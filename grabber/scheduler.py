"""定时等待 + 抢到后的提醒（macOS 友好），以及平台放班规则的日期计算。"""
from __future__ import annotations

import datetime as dt
import platform
import subprocess
import sys
import time

# ==== 平台放班规则 ====
# 每周放班两次，放班时刻 13:00（2026-07 起，原为 18:00）：
#   周一 13:00 放 周二/周三/周四 的班（未来 3 天）
#   周四 13:00 放 周五/周六/周日/下周一 的班（未来 4 天）
RELEASE_TIME = dt.time(13, 0)
RELEASE_WINDOWS = {0: 3, 3: 4}  # weekday -> 放未来几天（0=周一，3=周四）

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def weekday_cn(d: dt.date | dt.datetime | str) -> str:
    if isinstance(d, str):
        d = dt.date.fromisoformat(d[:10])
    return _WEEKDAY_CN[d.weekday()]


def next_release(now: dt.datetime | None = None) -> dt.datetime:
    """下一个放班时刻（严格晚于 now）。"""
    now = now or dt.datetime.now()
    for i in range(8):
        day = now.date() + dt.timedelta(days=i)
        if day.weekday() in RELEASE_WINDOWS:
            t = dt.datetime.combine(day, RELEASE_TIME)
            if t > now:
                return t
    raise RuntimeError("找不到下一个放班时刻（不应发生）")


def last_release(now: dt.datetime | None = None) -> dt.datetime:
    """最近一次已到来的放班时刻（不晚于 now）。"""
    now = now or dt.datetime.now()
    for i in range(8):
        day = now.date() - dt.timedelta(days=i)
        if day.weekday() in RELEASE_WINDOWS:
            t = dt.datetime.combine(day, RELEASE_TIME)
            if t <= now:
                return t
    raise RuntimeError("找不到上一个放班时刻（不应发生）")


def release_dates(ref: dt.datetime) -> list[str]:
    """ref 时刻对应的可抢日期窗口。

    ref 是放班日（周一/周四）则直接给该场窗口；
    否则回退到 ref 之前最近一次放班的窗口（抢剩余名额场景）。
    """
    if ref.weekday() not in RELEASE_WINDOWS:
        ref = last_release(ref)
    days = RELEASE_WINDOWS[ref.weekday()]
    return [(ref.date() + dt.timedelta(days=i)).isoformat() for i in range(1, days + 1)]


def parse_start_time(value: str) -> dt.datetime | None:
    """把配置里的 start_at 解析成本地时间的 datetime。

    支持两种写法：
      - "2026-05-20 00:00:00"  绝对时间
      - "00:00:00" 或 "00:00"   今天的某个时刻（若已过则顺延到明天）
    返回 None 表示不定时、立即开抢。
    """
    if not value:
        return None
    value = value.strip()
    now = dt.datetime.now()
    # 绝对时间
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            pass
    # 只有时刻
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = dt.datetime.strptime(value, fmt).time()
            target = now.replace(hour=t.hour, minute=t.minute,
                                 second=t.second, microsecond=0)
            if target <= now:
                target += dt.timedelta(days=1)
            return target
        except ValueError:
            pass
    raise ValueError(f"无法解析 start_at: {value!r}")


def wait_until(target: dt.datetime, lead_ms: int = 300) -> None:
    """阻塞等待到 target 之前 lead_ms 毫秒。

    提前一点点返回，是为了把“开始轮询/开抢”的动作卡在放班时刻前夜，
    让第一发请求尽量贴着 0 点落地。最后阶段用忙等保证精度。
    """
    fire_at = target - dt.timedelta(milliseconds=lead_ms)
    while True:
        remain = (fire_at - dt.datetime.now()).total_seconds()
        if remain <= 0:
            return
        if remain > 2:
            # 还早，粗睡，每秒刷新一次倒计时
            print(f"\r距开抢还有 {remain:8.1f}s ...", end="", flush=True)
            time.sleep(0.5)
        else:
            # 临近，细睡，逼近精度
            time.sleep(0.002)


def notify(title: str, message: str) -> None:
    """桌面通知 + 终端响铃。macOS 用 osascript，其它平台尽量降级。"""
    # 终端响铃（多响几声）
    sys.stdout.write("\a\a\a")
    sys.stdout.flush()

    system = platform.system()
    try:
        if system == "Darwin":
            safe_msg = message.replace('"', "'")
            safe_title = title.replace('"', "'")
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{safe_msg}" with title "{safe_title}" sound name "Glass"'],
                check=False, timeout=5,
            )
            # 再用语音播报一下，确保你能听到
            subprocess.run(["say", "抢到班次了"], check=False, timeout=5)
        elif system == "Linux":
            subprocess.run(["notify-send", title, message], check=False, timeout=5)
        # Windows 不强依赖第三方库，靠响铃 + 控制台
    except Exception:
        pass  # 通知失败不影响主流程
