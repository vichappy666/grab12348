"""定时等待 + 抢到后的提醒（macOS 友好）。"""
from __future__ import annotations

import datetime as dt
import platform
import subprocess
import sys
import time


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
