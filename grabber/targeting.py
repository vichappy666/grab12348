"""目标班次筛选与优先级排序（按星期分套的顺位表）。

规则：
  - 每天有一张「顺位表」（从高到低排的班次代码），按星期切换：
      周一/二/三/四（及默认周六/日）→ order
      周五                          → order_friday
      order 为空的星期 = 那天不抢（如把周六/日的表设空）
  - 同一个班次分「优 / 普」两种场次，prefer 决定同代码里先抢哪种（默认「优」）。
  - 精确匹配班次代码：order 里写 "C" 只命中 "C（…）"，不会误匹配 "C1"/"C3"。

排序键（越小越优先）：(顺位下标, 优普次序, 开始时间, 日期)。
每天各抢一个时：按该天顺位表从高到低抢，上一顺位满了自动落到下一顺位。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .client import Shift

FRIDAY = 4              # date.weekday(): 周一=0 … 周五=4 … 周日=6
WEEKEND = (5, 6)        # 周六、周日：默认不抢


@dataclass
class TargetSpec:
    dates: list[str]                                       # 要抢的日期，形如 ["2026-07-10"]
    order: list[str] = field(default_factory=list)         # 周一~周四（及周六日）顺位，从高到低
    order_friday: list[str] = field(default_factory=list)  # 周五顺位，从高到低
    prefer: str = "优"                                      # 同代码里优先抢「优」还是「普」

    def order_for(self, date_str: str) -> list[str]:
        """某天适用的顺位表；周五用 order_friday，周六/日不抢（空表）。"""
        wd = dt.date.fromisoformat(date_str[:10]).weekday()
        if wd == FRIDAY:
            return self.order_friday
        if wd in WEEKEND:
            return []
        return self.order

    def matches(self, s: Shift) -> bool:
        if s.date not in self.dates:
            return False
        return s.code in self.order_for(s.date)

    def _tier_rank(self, tier: str) -> int:
        """优/普次序：命中 prefer 的排 0，另一种排 1，无优普（高峰班等）排 2。"""
        if not tier:
            return 2
        return 0 if tier == self.prefer else 1

    def priority(self, s: Shift) -> tuple:
        """返回排序键，越小越优先。"""
        order = self.order_for(s.date)
        try:
            rank = order.index(s.code)
        except ValueError:
            rank = len(order)
        return (rank, self._tier_rank(s.tier), s.start_time, s.date)

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
