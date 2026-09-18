"""
Spread filter (architecture doc §5.6 / trade_filters module table).

Deliberately the simplest filter in the system: one comparison, no
state. Wide spreads are common around news/rollover and quietly erode
edge on every trade, so this is checked before anything more expensive.
"""

from __future__ import annotations

from typing import Optional

from config.config_schema import FilterConfig
from core.enums import RejectReason


def check_spread(current_spread_points: float, filters: FilterConfig) -> Optional[RejectReason]:
    if current_spread_points < 0:
        raise ValueError(f"current_spread_points cannot be negative, got {current_spread_points}")
    if current_spread_points > filters.max_spread_points:
        return RejectReason.SPREAD_TOO_WIDE
    return None
