"""离线集成测试：用抓包记录里的真实班次结构，验证筛选、并发抢、满员/失效处理。
不访问任何真实服务器，全部用 FakeClient 模拟。
"""
import datetime as dt

from grabber.client import Shift, GrabResult, AuthError
from grabber.targeting import TargetSpec
from grabber.grabber import Grabber, GrabberConfig

# ---- 来自抓包记录的真实班次数据（2026-05-22）----
RAW = [
    {"schedulingId": "b-full", "postName": "省热线平台法服岗(2026)", "schedulingDate": "2026-05-22 00:00:00",
     "shiftName": "B（2026-普）", "startTime": "08:00", "endTime": "17:00",
     "schedulingNumOfPeople": 1, "scheduledNumOfPeople": 1, "grabFlag": "0",
     "languageCategory": "普通话,粤语,潮汕话,客家话"},
    {"schedulingId": "c1-open", "postName": "省热线平台法服岗(2026)", "schedulingDate": "2026-05-22 00:00:00",
     "shiftName": "C1（2026-普）", "startTime": "08:15", "endTime": "13:15",
     "schedulingNumOfPeople": 8, "scheduledNumOfPeople": 7, "grabFlag": "0",
     "languageCategory": "普通话,粤语,潮汕话,客家话"},
    {"schedulingId": "c-open", "postName": "省热线平台法服岗(2026)", "schedulingDate": "2026-05-22 00:00:00",
     "shiftName": "C（2026-普）", "startTime": "09:00", "endTime": "18:30",
     "schedulingNumOfPeople": 5, "scheduledNumOfPeople": 0, "grabFlag": "0",
     "languageCategory": "普通话,粤语,潮汕话,客家话"},
    {"schedulingId": "e-open", "postName": "省热线平台法服岗(2026)", "schedulingDate": "2026-05-22 00:00:00",
     "shiftName": "E（2026-普）", "startTime": "14:00", "endTime": "21:30",
     "schedulingNumOfPeople": 3, "scheduledNumOfPeople": 1, "grabFlag": "0",
     "languageCategory": "普通话,粤语,潮汕话,客家话"},
]


def test_shift_parse():
    s = Shift.from_json(RAW[0])
    assert s.scheduling_id == "b-full"
    assert s.date == "2026-05-22"
    assert s.capacity == 1 and s.scheduled == 1
    assert s.has_room is False
    s2 = Shift.from_json(RAW[1])
    assert s2.has_room is True and s2.remaining == 1
    print("[OK] Shift 解析 / 名额判断")


def test_targeting_priority():
    shifts = [Shift.from_json(r) for r in RAW]
    # 偏好 C1 优先，其次 B；只看有名额的
    t = TargetSpec(dates=["2026-05-22"], shift_names=["C1", "B"])
    picks = t.pick(shifts, only_with_room=True)
    # B 满了被排除，C1 有名额排第一
    assert picks[0].scheduling_id == "c1-open", [p.shift_name for p in picks]
    assert all(p.has_room for p in picks)
    print("[OK] 目标筛选：满员排除 + 优先级排序 ->", [p.shift_name for p in picks])

    # 时间过滤：只要 09:00 及以后
    t2 = TargetSpec(dates=["2026-05-22"], time_after="09:00")
    picks2 = t2.pick(shifts, only_with_room=True)
    names = {p.shift_name for p in picks2}
    assert "C（2026-普）" in names and "E（2026-普）" in names
    assert not any("08:" in p.start_time for p in picks2)
    print("[OK] 时间区间过滤 ->", sorted(names))

    # 留空 shift_names = 全部有名额的
    t3 = TargetSpec(dates=["2026-05-22"])
    picks3 = t3.pick(shifts, only_with_room=True)
    assert len(picks3) == 3  # b 满，其余 3 个有名额
    print("[OK] 不填偏好 = 抢任意有名额 ->", len(picks3), "个候选")


class FakeClient:
    """模拟客户端：可控制 grab 成功/满员/失效。"""
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


def test_grab_success():
    shifts = [Shift.from_json(r) for r in RAW]
    # C1 满员失败，但 C 能抢成功 -> 引擎应抢到 C
    behavior = {"c1-open": "full", "c-open": "ok"}
    client = FakeClient(shifts, behavior)
    target = TargetSpec(dates=["2026-05-22"], shift_names=["C1", "C", "E"])
    cfg = GrabberConfig(poll_interval=0.0, duration=3, parallel=3, stop_after=1)
    logs = []
    g = Grabber(client, target, cfg, logger=logs.append)
    stats = g.run(start_at=None)
    assert len(stats.successes) == 1
    assert stats.successes[0].scheduling_id == "c-open"
    print("[OK] 并发抢：C1满员 -> 成功抢到 C，发起", stats.attempts, "次请求")


def test_grab_auth_error():
    shifts = [Shift.from_json(r) for r in RAW]
    behavior = {"c1-open": "auth"}
    client = FakeClient(shifts, behavior)
    target = TargetSpec(dates=["2026-05-22"], shift_names=["C1"])
    cfg = GrabberConfig(poll_interval=0.0, duration=3, parallel=1, stop_after=1)
    logs = []
    g = Grabber(client, target, cfg, logger=logs.append)
    stats = g.run(start_at=None)
    # token 失效应立即停止，没有成功
    assert len(stats.successes) == 0
    assert any("失效" in l for l in logs)
    print("[OK] token 失效：立即停止并提示")


if __name__ == "__main__":
    test_shift_parse()
    test_targeting_priority()
    test_grab_success()
    test_grab_auth_error()
    print("\n全部测试通过 ✅")
