"""
Timeframe resampling — lets a strategy derive a higher-timeframe view
(e.g. H1 trend) from the single lower-timeframe feed (M15) the engine
actually provides, without needing a second data feed or any change to
the architecture built in Phase 3.

CRITICAL correctness property: the currently-forming higher-timeframe
bucket is NEVER included in the output. If it were, a strategy computing
an H1 EMA from "the last few M15 candles that happen to fall in the
current, still-open hour" would see a value that changes retroactively
as more M15 candles arrive within that same hour — a subtle form of
look-ahead bias that would make backtest results not achievable live.
Every test in test_resample.py exists specifically to catch this.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from core_models import Candle


def _bucket_start(timestamp: datetime, timeframe_minutes: int) -> datetime:
    epoch = datetime(1970, 1, 1)
    minutes_since_epoch = int((timestamp - epoch).total_seconds() // 60)
    bucket_index = minutes_since_epoch // timeframe_minutes
    return epoch + timedelta(minutes=bucket_index * timeframe_minutes)


def resample_to_higher_timeframe(candles: list[Candle], timeframe_minutes: int) -> list[Candle]:
    """Aggregate `candles` (assumed sorted, fixed lower timeframe) into
    `timeframe_minutes`-sized bars. The LAST bucket is always dropped
    even if it looks complete, because the caller's candle list is a
    live-growing history — there is no way to know the current bucket
    has finished receiving candles until a candle from the NEXT bucket
    arrives, and by construction we can't see that from inside this
    function. Callers get only fully-closed higher-timeframe candles."""
    if timeframe_minutes <= 0:
        raise ValueError("timeframe_minutes must be > 0")
    if not candles:
        return []

    buckets: dict[datetime, list[Candle]] = {}
    order: list[datetime] = []
    for c in candles:
        key = _bucket_start(c.timestamp, timeframe_minutes)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(c)

    # Drop the last bucket — see docstring.
    complete_keys = order[:-1]

    result: list[Candle] = []
    for key in complete_keys:
        group = buckets[key]
        result.append(
            Candle(
                timestamp=key,
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
                volume=sum(c.volume for c in group),
                spread_points=None,  # spread doesn't aggregate meaningfully across a bar
            )
        )
    return result
