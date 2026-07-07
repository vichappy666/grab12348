"""核心抢班引擎。

策略概览：
  - 探查阶段（开抢前的等待期）：低频轮询列表，把目标班次的 schedulingId 先攒下来。
  - 开抢阶段（到点后）：高频「拉列表 → 对有名额的目标并发发起 grab」，
    谁先返回 code=200 谁赢，抢到即停。
  - 满员（超过限制）不致命，继续抢下一个 / 等下一次刷新；
    token 失效则立刻停下来提示重新登录。

频率默认温和（poll_interval ~0.25s）：你只是给自己抢一个班，不是黄牛，
对政府服务器客气点，够快就行。
"""
from __future__ import annotations

import datetime as dt
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .client import AuthError, Grab12348Client, GrabResult, Shift
from .targeting import TargetSpec
from . import scheduler


@dataclass
class GrabberConfig:
    poll_interval: float = 0.25     # 每轮拉列表的最小间隔（秒）
    duration: float = 120.0         # 开抢后最多持续多久（秒）
    parallel: int = 3               # 同时并发抢的候选数（取优先级最高的前 N 个）
    stop_after: int = 1             # 仅 per_day=False 时生效：抢到几个就停
    lead_ms: int = 300              # 定时模式下提前多少毫秒进入开抢循环
    aggressive: bool = False        # True=不等列表显示名额，到点直接对已知ID盲抢
    per_day: bool = True            # True=每个目标日期各抢一个班（抢到即锁定该天，不再抢这天其它班）


@dataclass
class GrabberStats:
    attempts: int = 0
    successes: list[GrabResult] = field(default_factory=list)
    started_at: dt.datetime | None = None


class Grabber:
    def __init__(self, client: Grab12348Client, target: TargetSpec,
                 cfg: GrabberConfig, logger=print):
        self.client = client
        self.target = target
        self.cfg = cfg
        self.log = logger
        self.stats = GrabberStats()
        self._known: dict[str, Shift] = {}   # schedulingId -> Shift（探查到的目标）
        self._grabbed: set[str] = set()      # 已抢成功的 id，避免重复
        self._grabbed_dates: set[str] = set()  # 已抢到班的日期（per_day 模式下每天限一个）

    # ---------- 探查 ----------
    def refresh(self, only_with_room: bool = False) -> list[Shift]:
        """拉取所有目标日期的班次，更新已知目标池，返回当前匹配的候选（已排序）。"""
        all_shifts: list[Shift] = []
        for date in self.target.dates:
            try:
                all_shifts.extend(self.client.get_grab_list(date))
            except AuthError:
                raise
            except Exception as e:  # 单个日期失败不影响其它
                self.log(f"  [警告] 拉取 {date} 列表失败: {e}")
        # 更新已知目标池（无论有没有名额都记下 id，方便 aggressive 盲抢）
        for s in all_shifts:
            if self.target.matches(s):
                self._known[s.scheduling_id] = s
        return self.target.pick(all_shifts, only_with_room=only_with_room)

    # ---------- 是否已达成目标 ----------
    def _done(self) -> bool:
        if self.cfg.per_day:
            # 每个目标日期都抢到一个班就收工
            return set(self.target.dates) <= self._grabbed_dates
        return len(self.stats.successes) >= self.cfg.stop_after

    # ---------- 每天限一个：某天串行试候选，抢到即停该天 ----------
    def _grab_one_day(self, shifts: list[Shift]) -> GrabResult | None:
        """串行抢某一天的候选（已按优先级排好），抢到一个就停这天。

        用于「每天各抢一个」：C1 抢时满了就退而抢同天的 B，
        抢到任意一个即锁定该天，不再占这天其它班次。
        """
        for s in shifts:
            res = self.client.grab(s.scheduling_id)
            self.stats.attempts += 1
            if res.ok:
                self.log(f"  ✅ 抢到！{s.label}  ({res.elapsed_ms:.0f}ms)")
                self._grabbed.add(s.scheduling_id)
                self._grabbed_dates.add(s.date)
                self.stats.successes.append(res)
                return res
            elif res.is_auth_error:
                raise AuthError(res.msg or "token 失效")
            elif res.is_full:
                self.log(f"  ✗ 满员 {s.shift_name}·{s.date} · {res.msg}  ({res.elapsed_ms:.0f}ms)")
            else:
                self.log(f"  ✗ {s.shift_name}·{s.date} · code={res.code} {res.msg}  ({res.elapsed_ms:.0f}ms)")
        return None

    def _grab_per_day(self, cand: list[Shift]) -> bool:
        """按天分组并发抢，每天最多抢到一个（跳过已抢到的天）。

        返回是否还有「未抢到、且当前有候选」的天可试；没有则调用方可歇一下。
        """
        days: dict[str, list[Shift]] = {}
        for s in cand:  # cand 已按优先级排序，dict 保序 -> 每天内部也按优先级
            if s.date in self._grabbed_dates:
                continue
            days.setdefault(s.date, []).append(s)
        if not days:
            return False
        day_lists = list(days.values())[: self.cfg.parallel]
        with ThreadPoolExecutor(max_workers=len(day_lists)) as pool:
            futs = [pool.submit(self._grab_one_day, shifts) for shifts in day_lists]
            for fut in as_completed(futs):
                fut.result()  # 让 AuthError 传播到主循环
        return True

    # ---------- 并发抢 ----------
    def _grab_many(self, shifts: list[Shift]) -> GrabResult | None:
        """并发抢若干候选，返回第一个成功的结果；都没成功返回 None。"""
        if not shifts:
            return None
        picks = shifts[: self.cfg.parallel]
        success: GrabResult | None = None
        with ThreadPoolExecutor(max_workers=len(picks)) as pool:
            futs = {pool.submit(self.client.grab, s.scheduling_id): s for s in picks}
            for fut in as_completed(futs):
                s = futs[fut]
                res = fut.result()
                self.stats.attempts += 1
                if res.ok:
                    self.log(f"  ✅ 抢到！{s.label}  ({res.elapsed_ms:.0f}ms)")
                    self._grabbed.add(s.scheduling_id)
                    self.stats.successes.append(res)
                    if success is None:
                        success = res
                elif res.is_auth_error:
                    raise AuthError(res.msg or "token 失效")
                elif res.is_full:
                    self.log(f"  ✗ 满员 {s.shift_name} · {res.msg}  ({res.elapsed_ms:.0f}ms)")
                else:
                    self.log(f"  ✗ {s.shift_name} · code={res.code} {res.msg}  ({res.elapsed_ms:.0f}ms)")
        return success

    # ---------- 主流程 ----------
    def run(self, start_at: dt.datetime | None = None) -> GrabberStats:
        # 1) 校验登录
        user = self.client.check_login()
        name = user.get("nickName") or user.get("userName") or "未知用户"
        self.log(f"登录有效：{name}")

        # 2) 探查一次，把目标 id 先攒下来，并展示现状
        self.log("探查目标班次 ...")
        try:
            cand_all = self.refresh(only_with_room=False)
        except AuthError as e:
            self.log(f"[错误] {e}")
            return self.stats
        self._print_candidates(cand_all)

        # 3) 定时等待
        if start_at:
            self.log(f"等待开抢时刻：{start_at:%Y-%m-%d %H:%M:%S}（提前 {self.cfg.lead_ms}ms 进入）")
            scheduler.wait_until(start_at, lead_ms=self.cfg.lead_ms)
            self.log("\n>>> 到点，开抢！")
        else:
            self.log(">>> 立即开抢！")

        # 4) 开抢循环
        self.stats.started_at = dt.datetime.now()
        deadline = time.monotonic() + self.cfg.duration
        last_fetch = 0.0

        while time.monotonic() < deadline:
            if self._done():
                break

            now = time.monotonic()
            try:
                if self.cfg.aggressive and self._known:
                    # 盲抢模式：到点直接对已知目标狂发，不等列表确认名额。
                    # 适合「明确知道要哪个班、且该班放出瞬间就会被秒光」的情况。
                    targets = [s for s in self._known.values()
                               if s.scheduling_id not in self._grabbed]
                    targets.sort(key=self.target.priority)
                    if self.cfg.per_day:
                        self._grab_per_day(targets)
                    else:
                        self._grab_many(targets)
                    # 间隔里也定期刷新一下列表，发现新名额
                    if now - last_fetch >= max(self.cfg.poll_interval, 0.5):
                        self.refresh(only_with_room=False)
                        last_fetch = now
                else:
                    # 标准模式：拉列表 → 对有名额的目标抢
                    if now - last_fetch >= self.cfg.poll_interval:
                        cand = self.refresh(only_with_room=True)
                        last_fetch = now
                        if self.cfg.per_day:
                            if not self._grab_per_day(cand):
                                time.sleep(0.05)  # 剩余的天暂无候选，歇一下等放班
                        else:
                            cand = [s for s in cand if s.scheduling_id not in self._grabbed]
                            if cand:
                                self._grab_many(cand)
                            else:
                                time.sleep(0.02)
                    else:
                        time.sleep(0.01)
            except AuthError as e:
                self.log(f"[错误] token 失效，停止：{e}")
                break

        self._print_summary()
        return self.stats

    # ---------- 输出 ----------
    def _print_candidates(self, cand: list[Shift]) -> None:
        if not cand:
            self.log("  当前没有匹配到目标班次（可能还没放班，或筛选条件太严）。")
            self.log("  —— 开抢时会持续刷新，放班后会自动发现。")
            return
        self.log(f"  匹配到 {len(cand)} 个目标班次（按优先级）：")
        for i, s in enumerate(cand, 1):
            room = f"剩{s.remaining}/{s.capacity}" if s.has_room else f"已满({s.capacity})"
            self.log(f"   {i:>2}. {s.label}  [{room}]  {s.scheduling_id}")

    def _print_summary(self) -> None:
        self.log("\n" + "=" * 48)
        if self.stats.successes:
            self.log(f"🎉 成功抢到 {len(self.stats.successes)} 个班次：")
            for r in self.stats.successes:
                s = self._known.get(r.scheduling_id)
                self.log(f"   - {s.label if s else r.scheduling_id}")
            # per_day 模式：提示哪些目标日期还没抢到
            if self.cfg.per_day:
                missing = [d for d in self.target.dates if d not in self._grabbed_dates]
                if missing:
                    self.log(f"⚠️  这些日期没抢到：{'、'.join(missing)}"
                             f"（可能还没放班 / 没有目标班次 / 名额已满）")
            # 抢到了就通知
            first = self.stats.successes[0]
            s = self._known.get(first.scheduling_id)
            scheduler.notify("抢班成功", s.label if s else "已抢到班次")
        else:
            self.log("没抢到。常见原因：放班瞬间名额被秒光 / 条件没匹配 / 持续时间太短。")
            self.log("可调：把 aggressive 设为 true、缩短 poll_interval、确认 start_at 与放班时刻一致。")
        self.log(f"共发起 grab 请求 {self.stats.attempts} 次。")
        self.log("=" * 48)
