#!/usr/bin/env python3
"""12348 抢班次 —— 命令行入口。

用法：
  python main.py check                 # 校验 token，看当前用户 / 已排班 / 已满日期
  python main.py list 2026-05-22       # 看某天可抢的班次（含名额）
  python main.py run                   # 按 config.yaml 跑抢班（含定时）
  python main.py run --now             # 忽略定时，立即开抢
  python main.py run --dry-run         # 只演练：刷新+显示会抢哪些，但不真的抢

配置见 config.yaml（从 config.example.yaml 复制修改）。
"""
from __future__ import annotations

import argparse
import sys

import requests

from grabber import config as cfgmod
from grabber import scheduler
from grabber.client import AuthError
from grabber.grabber import Grabber


def cmd_check(args) -> int:
    cfg = cfgmod.load_config(args.config)
    client = cfgmod.build_client(cfg)
    try:
        user = client.check_login()
    except AuthError as e:
        print(f"[X] token 无效：{e}")
        return 1
    print("token 有效 ✓")
    print(f"  姓名：{user.get('nickName')}")
    print(f"  手机：{user.get('phoneNumber')}")
    # 顺带看看目标日期 + 本月已排班 / 已满的日期
    dates: list[str] = []
    try:
        target = cfgmod.build_target(cfg)
        if any(d.lower() == "auto" for d in target.dates):
            nr = scheduler.next_release()
            target = cfgmod.resolve_auto_dates(target, nr)
            print(f"  下次放班：{nr:%Y-%m-%d %H:%M}（{scheduler.weekday_cn(nr)}）")
        dates = target.dates
        print(f"  目标日期：{'、'.join(f'{d}（{scheduler.weekday_cn(d)}）' for d in dates)}")
    except ValueError as e:
        print(f"  （目标日期未配置好：{e}）")
    if dates:
        month = dates[0][:7]
        try:
            arranged = client.get_arranged_dates(month)
            full = client.get_full_dates(month)
            print(f"  {month} 我已排班：{arranged}")
            print(f"  {month} 已抢满：{full}")
        except Exception as e:
            print(f"  （查询排班概况失败：{e}）")
    return 0


def cmd_list(args) -> int:
    cfg = cfgmod.load_config(args.config)
    client = cfgmod.build_client(cfg)
    try:
        shifts = client.get_grab_list(args.date)
    except AuthError as e:
        print(f"[X] token 无效：{e}")
        return 1
    if not shifts:
        print(f"{args.date} 暂无可抢班次（可能还没放班）。")
        return 0
    print(f"{args.date} 可抢班次（共 {len(shifts)} 个）：")
    print(f"{'班次':<16}{'时间':<14}{'名额':<12}{'schedulingId'}")
    for s in sorted(shifts, key=lambda x: x.start_time):
        room = f"剩{s.remaining}/{s.capacity}" if s.has_room else f"满({s.capacity})"
        t = f"{s.start_time}-{s.end_time}"
        print(f"{s.shift_name:<16}{t:<14}{room:<12}{s.scheduling_id}")
    return 0


def cmd_run(args) -> int:
    cfg = cfgmod.load_config(args.config)
    client = cfgmod.build_client(cfg)
    gcfg = cfgmod.build_grabber_config(cfg)

    # 定时：start_at 支持 "auto" = 下一个放班时刻（周一/周四 13:00）
    start_at = None
    if not args.now:
        raw = str(cfg.get("grab", {}).get("start_at") or "").strip()
        if raw.lower() == "auto":
            start_at = scheduler.next_release()
            print(f"自动定时：下一个放班时刻 {start_at:%Y-%m-%d %H:%M:%S}（{scheduler.weekday_cn(start_at)}）")
            print("（想立刻抢当前窗口的剩余名额，用 python main.py run --now）")
        else:
            start_at = scheduler.parse_start_time(raw)

    # 日期：dates 支持 "auto" = 按开抢时刻所属放班场次展开（--now 取最近一场）
    target = cfgmod.build_target(cfg)
    target = cfgmod.resolve_auto_dates(target, start_at or scheduler.last_release())
    print(f"目标日期：{'、'.join(f'{d}（{scheduler.weekday_cn(d)}）' for d in target.dates)}")

    grabber = Grabber(client, target, gcfg)

    if args.dry_run:
        # 演练：只校验 + 刷新 + 显示候选，不真的抢
        print("== 演练模式（不会真的抢）==")
        try:
            user = client.check_login()
            print(f"登录有效：{user.get('nickName')}")
            cand = grabber.refresh(only_with_room=False)
            grabber._print_candidates(cand)
        except AuthError as e:
            print(f"[X] token 无效：{e}")
            return 1
        return 0

    try:
        stats = grabber.run(start_at=start_at)
    except AuthError as e:
        print(f"[X] token 无效：{e}")
        return 1
    except KeyboardInterrupt:
        print("\n已手动中断。")
        return 130
    return 0 if stats.successes else 2


def main() -> int:
    # -c 作为各子命令的公共参数，这样 `run -c xxx`（放在子命令后）也能用
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default="config.yaml", help="配置文件路径")

    p = argparse.ArgumentParser(description="12348 抢班次工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", parents=[common], help="校验 token / 看排班概况")

    pl = sub.add_parser("list", parents=[common], help="查看某天可抢班次")
    pl.add_argument("date", help="日期，如 2026-05-22")

    pr = sub.add_parser("run", parents=[common], help="跑抢班")
    pr.add_argument("--now", action="store_true", help="忽略定时，立即开抢")
    pr.add_argument("--dry-run", action="store_true", help="只演练，不真的抢")

    args = p.parse_args()
    try:
        if args.cmd == "check":
            return cmd_check(args)
        if args.cmd == "list":
            return cmd_list(args)
        if args.cmd == "run":
            return cmd_run(args)
    except FileNotFoundError as e:
        print(f"[X] {e}")
        return 1
    except ValueError as e:
        print(f"[X] 配置有误：{e}")
        return 1
    except requests.RequestException as e:
        print(f"[X] 网络请求失败：{e}")
        print("    平台服务器可能较慢或网络不稳，稍后重试即可。")
        return 1
    return 1

# python main.py check               # 验 token + 看本月已排班/已满日期
# python main.py list 2026-05-22     # 看某天有哪些班、各剩几个名额、对应ID
# python main.py run --dry-run       # 演练：刷新+显示会抢哪些，但不真抢
# python main.py run --now           # 立即开抢（忽略配置里的定时）
# python main.py run                 # 正式跑（按 config.yaml 里 start_at 定时卡点）

# list 后面的日期是必填的,得自己换成你要查的那天,比如 python main.py list 2026-05-23。其他三个命令不用带日期,它们从 config.yaml 的 target.dates 读。
# run 那三种区别在于什么时候开抢:--dry-run 只看不抢(先确认筛选条件能命中你要的班,强烈建议正式抢前先跑这个);--now 立刻开抢;不带参数就等到 config.yaml 里 start_at 那个时刻再开抢——这是真正抢班那天用的。
# 还有个可选参数 -c(放在子命令后面),可以指定别的配置文件,比如为不同日期建多份配置:python main.py run --dry-run -c config_0709.yaml。一般用不上,默认就读 config.yaml。
# 想看完整帮助,直接敲 python main.py -h,或某个子命令的帮助 python main.py run -h,argparse 会把参数都列出来。
# 实战顺序就是从上往下:check 验环境 → list 看班 → run --dry-run 演练 → 抢班那天 run(配好 start_at)或 run --now(手动卡点)。


if __name__ == "__main__":
    sys.exit(main())
