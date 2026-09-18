"""
Technical indicators (architecture doc, strategy module table).

Pure functions only — no state, no I/O, no knowledge of Signal/Strategy.
Each returns a list the same length as its input, with None for indices
that don't yet have enough history (the "warm-up" period) — this makes
"not enough data yet" an explicit, checkable value rather than a
silently wrong number (e.g. an EMA computed from only 3 of the 20
periods it needs).
"""

from __future__ import annotations

from typing import Optional

from core_models import Candle


def ema(values: list[float], period: int) -> list[Optional[float]]:
    """Exponential moving average. The first `period - 1` entries are
    None (insufficient history); index `period - 1` seeds with a simple
    average of the first `period` values, then standard EMA smoothing
    applies from there."""
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(values)
    result: list[Optional[float]] = [None] * n
    if n < period:
        return result

    seed = sum(values[:period]) / period
    result[period - 1] = seed
    multiplier = 2.0 / (period + 1)
    prev = seed
    for i in range(period, n):
        current = (values[i] - prev) * multiplier + prev
        result[i] = current
        prev = current
    return result


def atr(candles: list[Candle], period: int) -> list[Optional[float]]:
    """Average True Range, Wilder's smoothing. True range at index 0 has
    no previous close, so it's just high-low; from index 1 onward it's
    the max of the three standard TR components."""
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(candles)
    result: list[Optional[float]] = [None] * n
    if n == 0:
        return result

    true_ranges: list[float] = [candles[0].high - candles[0].low]
    for i in range(1, n):
        prev_close = candles[i - 1].close
        tr = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - prev_close),
            abs(candles[i].low - prev_close),
        )
        true_ranges.append(tr)

    if n < period:
        return result

    seed = sum(true_ranges[:period]) / period
    result[period - 1] = seed
    prev_atr = seed
    for i in range(period, n):
        current = (prev_atr * (period - 1) + true_ranges[i]) / period
        result[i] = current
        prev_atr = current
    return result


def rsi(values: list[float], period: int) -> list[Optional[float]]:
    """Relative Strength Index, Wilder's smoothing. Returns values in
    [0, 100]. A period with zero average loss (pure uptrend so far)
    returns 100.0 rather than dividing by zero."""
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(values)
    result: list[Optional[float]] = [None] * n
    if n < period + 1:
        return result

    gains = [0.0]
    losses = [0.0]
    for i in range(1, n):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period

    def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    result[period] = _rsi_from_averages(avg_gain, avg_loss)

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result[i] = _rsi_from_averages(avg_gain, avg_loss)

    return result
