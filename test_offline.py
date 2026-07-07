"""离线集成测试：验证班次代码/优普解析、按星期的顺位优先级、精确匹配、
每天各抢一个（顺位 fallback）、并发抢、满员/失效处理、超时重试、auto 日期展开。
不访问任何真实服务器，全部用 Fake 模拟。
"""
import datetime as dt
import types

import requests

from grabber.client import Shift, GrabResult, AuthError, Grab12348Client
from grabber.targeting import TargetSpec
from grabber.grabber import Grabber, GrabberConfig
from grabber import scheduler, config as cfgmod

# 测试静音：抢班成功的用例会走真实通知代码（响铃+弹窗+语音），跑测试时不要吵
scheduler.notify = lambda *a, **k: None


def _raw(sid, date, name, cap, scheduled, start="08:15", end="13:15"):
    return {"schedulingId": sid, "postName": "省热线平台法服岗(2026)",
            "schedulingDate": f"{date} 00:00:00", "shiftName": name,
            "startTime": start, "endTime": end,
            "schedulingNumOfPeople": cap, "scheduledNumOfPeople": scheduled,
            "grabFlag": "0"}


# 参考星期：2026-07-06 周一, 07 周二, 08 周三, 09 周四, 10 周五, 11 周六, 12 周日, 13 周一
ORDER = ["C", "B", "D", "E", "F", "C3", "C1", "R", "G"]
ORDER_FRI = ["C", "B", "D"]


class FakeClient:
    """模拟客户端：可控制 grab 成功/满员/失效，并记录抢的顺序。"""
    def __init__(self, shifts, grab_behavior):
        self._shifts = shifts
        self._behavior = grab_behavior  # dict: id -> "ok"/"full"/"auth"
        self.grab_calls = []

    def check_login(self):
        return {"nickName": "测试用户", "phoneNumber": "177****6745"}

    def get_grab_list(self, date):
        return [s for s in self._shifts if s.date == date]

    def grab(self, scheduling_id):
        self.grab_calls.append(scheduling_id)
        b = self._behavior.get(scheduling_id, "full")
        if b == "ok":
            return GrabResult(True, 200, "操作成功", scheduling_id, 12.3)
        if b == "auth":
            return GrabResult(False, 401, "登录失效", scheduling_id, 5.0)
        return GrabResult(False, 500, "抢班人数已超过限制！", scheduling_id, 8.0)


class FlakySession:
    """模拟慢/不稳的服务器：前 fail_times 次超时，之后返回 resp。"""
    def __init__(self, fail_times, resp):
        self.calls = 0
        self.fail_times = fail_times
        self.resp = resp
        self.headers = {}
        self.cookies = types.SimpleNamespace(set=lambda *a, **k: None)

    def get(self, url, timeout=None, **kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise requests.exceptions.ReadTimeout("boom")
        return self.resp


# ---------------- 解析 ----------------
def test_shift_parse():
    s = Shift.from_json(_raw("b-full", "2026-07-06", "B（2026-普）", 1, 1, "08:00", "17:00"))
    assert s.date == "2026-07-06"
    assert s.capacity == 1 and s.scheduled == 1 and s.has_room is False
    assert s.code == "B" and s.tier == "普"
    s2 = Shift.from_json(_raw("c1-open", "2026-07-06", "C1（2026-优）", 20, 7))
    assert s2.has_room is True and s2.remaining == 13
    assert s2.code == "C1" and s2.tier == "优"
    print("[OK] Shift 解析 / 名额判断")


def test_shift_code_tier():
    cases = [
        ("C（2026-优）", "C", "优"),
        ("C（2026-普）", "C", "普"),
        ("C1（2026-普）", "C1", "普"),
        ("C3（2026-优）", "C3", "优"),
        ("早高峰", "早高峰", ""),      # 高峰班无括号、无优普
    ]
    for name, code, tier in cases:
        s = Shift.from_json(_raw("x", "2026-07-06", name, 10, 0, "09:00", "18:30"))
        assert s.code == code and s.tier == tier, (name, s.code, s.tier)
    print("[OK] 班次代码/优普解析（含高峰班无优普）")


# ---------------- 顺位 + 优普 + 精确匹配 ----------------
def test_targeting_order_and_tier():
    day = "2026-07-06"  # 周一，用全序
    shifts = [
        Shift.from_json(_raw("c-you", day, "C（2026-优）", 40, 0, "09:00", "18:30")),
        Shift.from_json(_raw("c-pu",  day, "C（2026-普）", 40, 0, "09:00", "18:30")),
        Shift.from_json(_raw("c1-you", day, "C1（2026-优）", 20, 0, "08:15", "13:15")),
        Shift.from_json(_raw("b-you", day, "B（2026-优）", 40, 0, "08:00", "17:00")),
    ]
    t = TargetSpec(dates=[day], order=ORDER, order_friday=ORDER_FRI, prefer="优")
    picks = [s.scheduling_id for s in t.pick(shifts, only_with_room=True)]
    # 顺位第一优先：C 在 B、C1 之前；同为 C，优 > 普；C1 顺位最靠后
    assert picks == ["c-you", "c-pu", "b-you", "c1-you"], picks
    print("[OK] 顺位优先 + 同班次优>普 排序 ->", picks)


def test_targeting_exact_match():
    day = "2026-07-06"
    shifts = [
        Shift.from_json(_raw("c", day, "C（2026-优）", 40, 0, "09:00", "18:30")),
        Shift.from_json(_raw("c1", day, "C1（2026-优）", 20, 0, "08:15", "13:15")),
        Shift.from_json(_raw("c3", day, "C3（2026-优）", 25, 0, "13:30", "19:30")),
    ]
    t = TargetSpec(dates=[day], order=["C"], prefer="优")   # 只要 C，不要 C1/C3
    picks = [s.scheduling_id for s in t.pick(shifts, only_with_room=True)]
    assert picks == ["c"], picks   # 精确匹配：C 不误匹配 C1/C3
    print("[OK] 精确匹配：order=['C'] 只命中 C，不碰 C1/C3")


def test_targeting_prefer_pu():
    day = "2026-07-06"
    shifts = [
        Shift.from_json(_raw("c-you", day, "C（2026-优）", 40, 0, "09:00", "18:30")),
        Shift.from_json(_raw("c-pu",  day, "C（2026-普）", 40, 0, "09:00", "18:30")),
    ]
    t = TargetSpec(dates=[day], order=["C"], prefer="普")   # 改成优先普
    picks = [s.scheduling_id for s in t.pick(shifts, only_with_room=True)]
    assert picks == ["c-pu", "c-you"], picks
    print("[OK] prefer=普 时 普 > 优")


# ---------------- 按星期分套 ----------------
def test_targeting_weekday_sets():
    dates = ["2026-07-10", "2026-07-11", "2026-07-12", "2026-07-13"]  # 五/六/日/一
    t = TargetSpec(dates=dates, order=ORDER, order_friday=ORDER_FRI, prefer="优")

    fri_c = Shift.from_json(_raw("fc", "2026-07-10", "C（2026-优）", 40, 0, "09:00", "18:30"))
    fri_e = Shift.from_json(_raw("fe", "2026-07-10", "E（2026-优）", 45, 0, "14:00", "21:30"))
    assert t.matches(fri_c) and not t.matches(fri_e)        # 周五短序：C 抢、E 不抢

    sat_c = Shift.from_json(_raw("sc", "2026-07-11", "C（2026-优）", 40, 0, "09:00", "18:30"))
    sun_c = Shift.from_json(_raw("uc", "2026-07-12", "C（2026-优）", 40, 0, "09:00", "18:30"))
    assert not t.matches(sat_c) and not t.matches(sun_c)    # 周六周日：一律不抢

    mon_e = Shift.from_json(_raw("me", "2026-07-13", "E（2026-优）", 45, 0, "14:00", "21:30"))
    assert t.matches(mon_e)                                  # 下周一全序：E 可抢
    print("[OK] 星期分套：周五短序(E不抢)/周六日不抢/周一全序(E可抢)")


# ---------------- 每天各抢一个 ----------------
def test_per_day_multi_dates():
    dates = ["2026-07-07", "2026-07-08", "2026-07-09"]  # 周二三四
    shifts = [Shift.from_json(_raw(f"c-{d}", d, "C（2026-优）", 40, 39, "09:00", "18:30")) for d in dates]
    client = FakeClient(shifts, {f"c-{d}": "ok" for d in dates})
    target = TargetSpec(dates=dates, order=ORDER, order_friday=ORDER_FRI, prefer="优")
    cfg = GrabberConfig(poll_interval=0.0, duration=3, parallel=3, per_day=True)
    g = Grabber(client, target, cfg, logger=lambda *a: None)
    stats = g.run(start_at=None)
    assert len(stats.successes) == 3, [s.scheduling_id for s in stats.successes]
    assert g._grabbed_dates == set(dates)
    print("[OK] 每天各抢一个：周二三四各抢到 1 个 C")


def test_per_day_one_per_day():
    day = "2026-07-06"  # 周一
    shifts = [Shift.from_json(_raw("c-you", day, "C（2026-优）", 40, 39, "09:00", "18:30")),
              Shift.from_json(_raw("b-you", day, "B（2026-优）", 40, 39, "08:00", "17:00"))]
    client = FakeClient(shifts, {"c-you": "ok", "b-you": "ok"})
    target = TargetSpec(dates=[day], order=ORDER, order_friday=ORDER_FRI, prefer="优")
    cfg = GrabberConfig(poll_interval=0.0, duration=3, parallel=3, per_day=True)
    g = Grabber(client, target, cfg, logger=lambda *a: None)
    stats = g.run(start_at=None)
    assert len(stats.successes) == 1
    assert stats.successes[0].scheduling_id == "c-you"    # C 顺位最高
    assert "b-you" not in client.grab_calls               # 抢到 C 就停，不占 B
    print("[OK] 每天限一个：抢到 C 即停，不重复占 B")


def test_per_day_fallback_order_and_tier():
    """顺位 + 优普 fallback：C优满 -> C普满 -> 抢到 B优。"""
    day = "2026-07-06"  # 周一
    shifts = [Shift.from_json(_raw("c-you", day, "C（2026-优）", 40, 39, "09:00", "18:30")),
              Shift.from_json(_raw("c-pu",  day, "C（2026-普）", 40, 39, "09:00", "18:30")),
              Shift.from_json(_raw("b-you", day, "B（2026-优）", 40, 39, "08:00", "17:00"))]
    client = FakeClient(shifts, {"c-you": "full", "c-pu": "full", "b-you": "ok"})
    target = TargetSpec(dates=[day], order=ORDER, order_friday=ORDER_FRI, prefer="优")
    cfg = GrabberConfig(poll_interval=0.0, duration=3, parallel=3, per_day=True)
    g = Grabber(client, target, cfg, logger=lambda *a: None)
    stats = g.run(start_at=None)
    assert len(stats.successes) == 1
    assert stats.successes[0].scheduling_id == "b-you"
    assert client.grab_calls[:3] == ["c-you", "c-pu", "b-you"]  # 按 C优->C普->B优 顺序
    print("[OK] 顺位+优普 fallback：C优满->C普满->抢到 B优")


def test_per_day_skips_weekend():
    """周四场：周六周日不抢，只抢周五 + 下周一。"""
    dates = ["2026-07-10", "2026-07-11", "2026-07-12", "2026-07-13"]  # 五/六/日/一
    shifts = [Shift.from_json(_raw(f"c-{d}", d, "C（2026-优）", 40, 39, "09:00", "18:30")) for d in dates]
    client = FakeClient(shifts, {f"c-{d}": "ok" for d in dates})
    # 目标日期已由 auto 展开排除周六日（这里直接给排除后的）
    target = TargetSpec(dates=["2026-07-10", "2026-07-13"], order=ORDER, order_friday=ORDER_FRI, prefer="优")
    cfg = GrabberConfig(poll_interval=0.0, duration=3, parallel=3, per_day=True)
    g = Grabber(client, target, cfg, logger=lambda *a: None)
    stats = g.run(start_at=None)
    assert g._grabbed_dates == {"2026-07-10", "2026-07-13"}
    assert "c-2026-07-11" not in client.grab_calls and "c-2026-07-12" not in client.grab_calls
    print("[OK] 周六周日不抢：只抢到周五 + 下周一")


# ---------------- 并发（per_day=False）与失效 ----------------
def test_grab_concurrent_fallback():
    day = "2026-07-06"
    shifts = [Shift.from_json(_raw("c-open", day, "C（2026-优）", 40, 39, "09:00", "18:30")),
              Shift.from_json(_raw("b-open", day, "B（2026-优）", 40, 39, "08:00", "17:00"))]
    client = FakeClient(shifts, {"c-open": "full", "b-open": "ok"})
    target = TargetSpec(dates=[day], order=ORDER, order_friday=ORDER_FRI, prefer="优")
    cfg = GrabberConfig(poll_interval=0.0, duration=3, parallel=3, stop_after=1, per_day=False)
    g = Grabber(client, target, cfg, logger=lambda *a: None)
    stats = g.run(start_at=None)
    assert len(stats.successes) == 1 and stats.successes[0].scheduling_id == "b-open"
    print("[OK] per_day=False 并发抢：C 满 -> 抢到 B")


def test_grab_auth_error():
    day = "2026-07-06"
    shifts = [Shift.from_json(_raw("c-open", day, "C（2026-优）", 40, 39, "09:00", "18:30"))]
    client = FakeClient(shifts, {"c-open": "auth"})
    target = TargetSpec(dates=[day], order=ORDER, order_friday=ORDER_FRI, prefer="优")
    cfg = GrabberConfig(poll_interval=0.0, duration=3, parallel=1, stop_after=1, per_day=False)
    logs = []
    g = Grabber(client, target, cfg, logger=logs.append)
    stats = g.run(start_at=None)
    assert len(stats.successes) == 0
    assert any("失效" in l for l in logs)
    print("[OK] token 失效：立即停止并提示")


# ---------------- 放班规则 / auto 展开 ----------------
def test_release_schedule():
    assert dt.date(2026, 7, 6).weekday() == 0  # 周一
    mon_am = dt.datetime(2026, 7, 6, 9, 0)
    nr = scheduler.next_release(mon_am)
    assert nr == dt.datetime(2026, 7, 6, 13, 0), nr
    assert scheduler.release_dates(nr) == ["2026-07-07", "2026-07-08", "2026-07-09"]

    mon_pm = dt.datetime(2026, 7, 6, 14, 0)
    nr2 = scheduler.next_release(mon_pm)
    assert nr2 == dt.datetime(2026, 7, 9, 13, 0), nr2
    assert scheduler.release_dates(nr2) == ["2026-07-10", "2026-07-11", "2026-07-12", "2026-07-13"]
    print("[OK] 放班规则：周一/周四 13:00 与对应日期窗口")


def test_resolve_auto_dates():
    ref_thu = dt.datetime(2026, 7, 9, 13, 0)   # 周四放班
    t = TargetSpec(dates=["auto"], order=ORDER, order_friday=ORDER_FRI, prefer="优")
    r = cfgmod.resolve_auto_dates(t, ref_thu)
    # 窗口 周五/六/日/下周一 -> 排除周六日 -> 只留周五(07-10) + 下周一(07-13)
    assert r.dates == ["2026-07-10", "2026-07-13"], r.dates

    ref_mon = dt.datetime(2026, 7, 6, 13, 0)   # 周一放班
    t2 = TargetSpec(dates=["auto"], order=ORDER, order_friday=ORDER_FRI, prefer="优")
    r2 = cfgmod.resolve_auto_dates(t2, ref_mon)
    assert r2.dates == ["2026-07-07", "2026-07-08", "2026-07-09"], r2.dates
    print("[OK] auto 展开：周四场排除周六日(留周五+下周一) / 周一场留周二三四")


# ---------------- 超时重试 ----------------
def test_get_retry_on_timeout():
    resp = types.SimpleNamespace(
        status_code=200,
        json=lambda: {"code": 200, "data": {"nickName": "焦军平", "phoneNumber": "1777"}},
    )
    c = Grab12348Client(token="x", retries=2, retry_backoff=0)
    c.session = FlakySession(fail_times=2, resp=resp)
    user = c.check_login()
    assert user["nickName"] == "焦军平"
    assert c.session.calls == 3
    print("[OK] GET 超时自动重试：前2次超时 -> 第3次成功")

    c2 = Grab12348Client(token="x", retries=2, retry_backoff=0)
    c2.session = FlakySession(fail_times=99, resp=resp)
    try:
        c2.get_grab_list("2026-07-10")
        assert False, "应抛超时异常"
    except requests.exceptions.ReadTimeout:
        assert c2.session.calls == 3
    print("[OK] 超时超过重试上限：如实抛出异常")


if __name__ == "__main__":
    test_shift_parse()
    test_shift_code_tier()
    test_targeting_order_and_tier()
    test_targeting_exact_match()
    test_targeting_prefer_pu()
    test_targeting_weekday_sets()
    test_per_day_multi_dates()
    test_per_day_one_per_day()
    test_per_day_fallback_order_and_tier()
    test_per_day_skips_weekend()
    test_grab_concurrent_fallback()
    test_grab_auth_error()
    test_release_schedule()
    test_resolve_auto_dates()
    test_get_retry_on_timeout()
    print("\n全部测试通过 ✅")
