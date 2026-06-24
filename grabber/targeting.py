"""目标班次筛选与优先级排序。

支持三种匹配条件（可组合）：
  1. shift_names：按班次名匹配（支持子串，比如填 "C1" 能匹配 "C1（2026-普）"）
  2. time_after / time_before：按开始时间过滤（"08:00" 这种）
  3. 不填任何条件 = 该日期下所有有名额的班次都要

排序规则：
  - 优先按 shift_names 里列出的先后顺序（越靠前优先级越高）
  - 其次按开始时间早的优先
你最想要哪个班，就把它写在 shift_names 第一个。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .client import Shift


@dataclass
class TargetSpec:
    dates: list[str]                          # 要抢的日期，形如 ["2026-05-22"]
    shift_names: list[str] = field(default_factory=list)  # 偏好班次名（按优先级）
    time_after: str = ""                      # 只要开始时间 >= 这个（含）
    time_before: str = ""                     # 只要开始时间 <= 这个（含）

    def matches(self, s: Shift) -> bool:
        if s.date not in self.dates:
            return False
        if self.shift_names:
            if not any(name in s.shift_name for name in self.shift_names):
                return False
        if self.time_after and s.start_time < self.time_after:
            return False
        if self.time_before and s.start_time > self.time_before:
            return False
        return True

    def priority(self, s: Shift) -> tuple:
        """返回排序键，越小越优先。"""
        # shift_names 里的次序：找到第一个命中的下标，没命中给一个大值
        name_rank = len(self.shift_names)
        for i, name in enumerate(self.shift_names):
            if name in s.shift_name:
                name_rank = i
                break
        return (name_rank, s.start_time, s.date)

    def pick(self, shifts: list[Shift], only_with_room: bool = True) -> list[Shift]:
        """从一批班次里挑出匹配的目标，按优先级排好序。

        only_with_room=True 时只保留还有名额的（真正去抢时用）。
        only_with_room=False 时保留全部匹配的（探查/演练时用，方便看到满员情况）。
        """
        cand = [s for s in shifts if self.matches(s)]
        if only_with_room:
            cand = [s for s in cand if s.has_room]
        cand.sort(key=self.priority)
        return cand
