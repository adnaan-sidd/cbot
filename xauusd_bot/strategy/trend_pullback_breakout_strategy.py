"""
Trend-Pullback-BREAKOUT strategy — v2, redesigned after real-data
validation showed the original TrendPullbackStrategy (v1) had negative
expectancy on BOTH a choppy real period (2019, profit factor 0.91) AND
one of gold's strongest real uptrends in decades (the 2020 COVID rally,
profit factor 0.83 — WORSE than the choppy period). That result ruled
out "bad test window" as the explanation and pointed at the entry
trigger itself: v1's "one candle closes back through a 20-EMA" pattern
fires on ordinary noise, and its "confirmation" (close > open) is close
to a coin flip on its own.

This version is a genuinely different, more selective trigger, not a
retuning of v1's parameters (parameter sweeps on v1 — wider stops,
tighter RSI, faster HTF EMA, an ATR floor, different R:R — were all
tried first and none fixed the negative expectancy; see the project's
iteration notes). Three structural changes:

  1. TREND STRENGTH FILTER: v1 only checked EMA50 > EMA200 direction,
     trading even when the two were barely separated (a weak, arguably
     sideways market). v2 additionally requires the EMA separation to
     be at least `min_trend_strength_atr_multiples` times the H1 ATR —
     filtering out exactly the kind of borderline "trend" that a
     crossover-only filter can't distinguish from noise.

  2. MULTI-BAR PULLBACK + STRUCTURAL BREAKOUT ENTRY: instead of a single
     candle crossing an EMA, v2 requires an actual multi-candle
     retracement (`min_pullback_bars` to `max_pullback_bars` consecutive
     M15 candles on the wrong side of the fast EMA, immediately preceded
     by candles confirming the prior in-trend context) and then a
     genuine BREAKOUT of that pullback's own high/low — a real
     structural event, not a a bare EMA cross that roughly half of all
     candles satisfy by chance.

  3. STRUCTURE-BASED STOP: v1 placed its stop at a fixed ATR multiple
     from entry, with no relationship to the setup's actual invalidation
     point. v2 places the stop just beyond the pullback's own extreme
     (the level whose breach genuinely invalidates the "resumption"
     thesis), with a small ATR buffer — a stop tied to market structure,
     not an arbitrary distance.

This is still fully decoupled from risk_engine/risk_gate — the only
thing this class produces is a Signal, exactly like v1.
"""

from __future__ import annotations

from typing import Optional

from core.enums import Direction
from core.models import Candle, Signal
from strategy.indicators import atr, ema, rsi
from strategy.resample import resample_to_higher_timeframe
from strategy.strategy_interface import MarketState, Strategy


class TrendPullbackBreakoutStrategy(Strategy):
    def __init__(
        self,
        *,
        htf_minutes: int = 60,
        htf_fast_ema_period: int = 50,
        htf_slow_ema_period: int = 200,
        htf_atr_period: int = 14,
        min_trend_strength_atr_multiples: float = 1.0,
        ltf_fast_ema_period: int = 20,
        min_pullback_bars: int = 2,
        max_pullback_bars: int = 8,
        atr_period: int = 14,
        stop_buffer_atr_multiplier: float = 0.3,
        reward_risk_ratio: float = 2.0,
        rsi_period: int = 14,
        rsi_long_min: float = 30.0,
        rsi_long_max: float = 75.0,
        rsi_short_min: float = 25.0,
        rsi_short_max: float = 70.0,
        max_history_candles: Optional[int] = None,
        use_trailing_stop: bool = False,
        name: str = "trend_pullback_breakout_v2",
    ):
        if min_pullback_bars < 1:
            raise ValueError("min_pullback_bars must be >= 1")
        if max_pullback_bars < min_pullback_bars:
            raise ValueError("max_pullback_bars must be >= min_pullback_bars")

        self.htf_minutes = htf_minutes
        self.htf_fast_ema_period = htf_fast_ema_period
        self.htf_slow_ema_period = htf_slow_ema_period
        self.htf_atr_period = htf_atr_period
        self.min_trend_strength_atr_multiples = min_trend_strength_atr_multiples
        self.ltf_fast_ema_period = ltf_fast_ema_period
        self.min_pullback_bars = min_pullback_bars
        self.max_pullback_bars = max_pullback_bars
        self.atr_period = atr_period
        self.stop_buffer_atr_multiplier = stop_buffer_atr_multiplier
        self.reward_risk_ratio = reward_risk_ratio
        self.rsi_period = rsi_period
        self.rsi_long_min = rsi_long_min
        self.rsi_long_max = rsi_long_max
        self.rsi_short_min = rsi_short_min
        self.rsi_short_max = rsi_short_max
        self.max_history_candles = max_history_candles or (
            4 * htf_slow_ema_period * (htf_minutes // 15 or 1) + 500
        )
        self.use_trailing_stop = use_trailing_stop
        self.name = name

    def generate_signal(self, market_state: MarketState) -> Optional[Signal]:
        history = market_state.history
        if len(history) < max(self.max_pullback_bars + 2, 2):
            return None
        if len(history) > self.max_history_candles:
            history = history[-self.max_history_candles :]

        trend = self._htf_trend_with_strength(history)
        if trend is None:
            return None

        return self._ltf_breakout_entry(market_state.symbol, history, trend)

    # -- higher-timeframe trend filter, now with a strength requirement --

    def _htf_trend_with_strength(self, history: list[Candle]) -> Optional[Direction]:
        htf_candles = resample_to_higher_timeframe(history, self.htf_minutes)
        needed = max(self.htf_slow_ema_period, self.htf_atr_period) + 1
        if len(htf_candles) < needed:
            return None

        closes = [c.close for c in htf_candles]
        fast = ema(closes, self.htf_fast_ema_period)[-1]
        slow = ema(closes, self.htf_slow_ema_period)[-1]
        htf_atr = atr(htf_candles, self.htf_atr_period)[-1]
        if fast is None or slow is None or htf_atr is None or htf_atr <= 0:
            return None

        separation = fast - slow
        if abs(separation) < self.min_trend_strength_atr_multiples * htf_atr:
            return None  # trend too weak / effectively sideways

        return Direction.LONG if separation > 0 else Direction.SHORT

    # -- multi-bar pullback identification + structural breakout entry --

    def _ltf_breakout_entry(self, symbol: str, history: list[Candle], trend: Direction) -> Optional[Signal]:
        closes = [c.close for c in history]
        fast_ema_series = ema(closes, self.ltf_fast_ema_period)
        if fast_ema_series[-1] is None:
            return None

        n = len(history)
        idx_before_current = n - 2
        if idx_before_current < 1:
            return None

        def is_wrong_side(i: int) -> Optional[bool]:
            e = fast_ema_series[i]
            if e is None:
                return None
            c = history[i].close
            return c < e if trend == Direction.LONG else c > e

        # Walk backward from the bar just before the current one, counting
        # a contiguous run of "wrong side" bars -- this is the pullback.
        count = 0
        i = idx_before_current
        while i >= 0 and is_wrong_side(i) is True:
            count += 1
            i -= 1

        if not (self.min_pullback_bars <= count <= self.max_pullback_bars):
            return None

        # The bar immediately before the pullback run must be explicitly
        # on the RIGHT side -- proving there was a real in-trend context
        # to pull back FROM, not just an ambiguous/undefined EMA warm-up.
        if i < 0 or is_wrong_side(i) is not False:
            return None

        pullback_start = i + 1
        pullback_end = idx_before_current
        pullback_candles = history[pullback_start : pullback_end + 1]

        if trend == Direction.LONG:
            breakout_level = max(c.high for c in pullback_candles)
            pullback_extreme = min(c.low for c in pullback_candles)
        else:
            breakout_level = min(c.low for c in pullback_candles)
            pullback_extreme = max(c.high for c in pullback_candles)

        current = history[-1]
        current_ema = fast_ema_series[-1]

        if trend == Direction.LONG:
            triggered = (
                current.close > breakout_level
                and current.close > current.open
                and current.close > current_ema
            )
        else:
            triggered = (
                current.close < breakout_level
                and current.close < current.open
                and current.close < current_ema
            )
        if not triggered:
            return None

        rsi_series = rsi(closes, self.rsi_period)
        current_rsi = rsi_series[-1]
        if current_rsi is None:
            return None
        if trend == Direction.LONG and not (self.rsi_long_min <= current_rsi <= self.rsi_long_max):
            return None
        if trend == Direction.SHORT and not (self.rsi_short_min <= current_rsi <= self.rsi_short_max):
            return None

        atr_series = atr(history, self.atr_period)
        current_atr = atr_series[-1]
        if current_atr is None or current_atr <= 0:
            return None

        entry_price = current.close
        buffer = current_atr * self.stop_buffer_atr_multiplier

        if trend == Direction.LONG:
            stop_loss = pullback_extreme - buffer
            risk = entry_price - stop_loss
            if risk <= 0:
                return None
            take_profit = None if self.use_trailing_stop else entry_price + risk * self.reward_risk_ratio
        else:
            stop_loss = pullback_extreme + buffer
            risk = stop_loss - entry_price
            if risk <= 0:
                return None
            take_profit = None if self.use_trailing_stop else entry_price - risk * self.reward_risk_ratio

        return Signal(
            symbol=symbol,
            direction=trend,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strategy_name=self.name,
            metadata={
                "atr": current_atr,
                "rsi": current_rsi,
                "pullback_bars": count,
                "breakout_level": breakout_level,
                "pullback_extreme": pullback_extreme,
            },
        )
