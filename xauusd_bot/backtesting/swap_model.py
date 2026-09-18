"""
Swap (overnight financing) cost model.

Not part of the original architecture doc's cost model, added when the
broker switched to XM/cTrader because the new account's swap-free status
is UNCONFIRMED — unlike the previous (confirmed-Islamic) broker, swap is
not simply omitted here. Real swap point values from cTrader's Symbol
Info are stored in BrokerConfig, and this module calculates the actual
cost from them, gated behind `BrokerConfig.swap_enabled` (default False).

Modeling convention, matching the broker's stated rules exactly
(cTrader Symbol Info: "3-day swaps: Wednesday", "Weekend swaps: Disabled"):
  - One swap charge per calendar night the position is held across a
    daily rollover.
  - The night landing on the configured `triple_swap_weekday` (Wednesday
    by default) is charged at 3x, to account for the weekend.
  - Nights landing on Saturday/Sunday are NOT charged when
    `weekend_swap_disabled` is True — the market is closed and the
    Wednesday charge already compensates for the weekend.
  - The exit day itself is not counted as a held night — swap only
    applies to nights the position remained open THROUGH the rollover,
    not the day it closes.

This is a simplified approximation of real broker rollover timing
(actual rollover happens at a specific server time, e.g. 21:00-22:00
platform time, not exactly UTC midnight) — stated explicitly as a
Phase-3-appropriate simplification, consistent with the daily-loss
reset already using UTC midnight as its rollover convention.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from config.config_schema import BrokerConfig
from core.enums import Direction


def _nights_held(opened_at: datetime, closed_at: datetime) -> list[date]:
    """Each calendar date whose midnight rollover the position was open
    through, i.e. every date from opened_at's date up to (but not
    including) closed_at's date."""
    start = opened_at.date()
    end = closed_at.date()
    nights = []
    current = start
    while current < end:
        nights.append(current)
        current += timedelta(days=1)
    return nights


def calculate_swap_cost(
    *,
    direction: Direction,
    lot_size: float,
    opened_at: datetime,
    closed_at: datetime,
    broker: BrokerConfig,
) -> float:
    """Total swap cost (negative) or credit (positive) in USD for the
    full holding period. Returns 0.0 if `broker.swap_enabled` is False —
    callers can call this unconditionally without checking the flag
    themselves."""
    if not broker.swap_enabled:
        return 0.0
    if lot_size <= 0:
        raise ValueError("lot_size must be > 0")
    if closed_at < opened_at:
        raise ValueError("closed_at cannot be before opened_at")

    swap_points = broker.swap_long_points if direction == Direction.LONG else broker.swap_short_points
    swap_price_per_lot_per_night = swap_points * broker.point_size * broker.contract_size

    total_multiplier = 0
    for night in _nights_held(opened_at, closed_at):
        weekday = night.weekday()  # 0=Monday .. 6=Sunday
        if broker.weekend_swap_disabled and weekday in (5, 6):  # Saturday, Sunday
            continue
        multiplier = 3 if weekday == broker.triple_swap_weekday else 1
        total_multiplier += multiplier

    return swap_price_per_lot_per_night * lot_size * total_multiplier
