"""
Mean-Reversion strategy — a genuinely different paradigm, built after
extensive real-data testing (see project iteration notes) showed FIVE
different trend-following entry/exit combinations all failing on both a
choppy period AND a strongly trending real period. That pattern (losing
even when a strong, obvious trend was present to ride) pointed away
from "wrong parameters" and toward "wrong hypothesis" — trend-following
may simply not suit XAUUSD M15 with these simple technical mechanics.

The hypothesis here is different in kind, not degree: short-timeframe
intraday price action in a liquid instrument like gold is often driven
more by order flow / liquidity effects that REVERT than by genuine
sustained trend continuation — real trend-following edges typically
need longer swing/position timeframes (H4/D1+) to overcome timeframe
noise. This strategy fades stretched moves back toward a short-term
mean instead of trying to ride them.

Mechanics:
  - distance = (close - EMA) / ATR -- a volatility-normalized measure
    of how far price has stretched from its short-term mean.
  - SHORT when distance >= entry_threshold_atr AND the current candle
    shows a reversal (bearish close) -- price stretched too far above
    the mean and is turning down.
  - LONG mirrored below the mean.
  - Take-profit is the EMA's value AT ENTRY (a concrete reversion
    target, fixed at signal time like every other strategy's target —
    it doesn't chase the EMA as it keeps moving after entry).
  - Stop-loss is a fixed ATR multiple beyond entry, in the adverse
    direction (price stretching even further before reverting).
  - Optional HTF trend filter (`max_htf_trend_strength_atr_multiples`)
    can SKIP reversion trades against a strong prevailing trend, since
    fading a genuine strong trend is a materially different risk than
    fading noise in a range — left permissive by default so the pure
    hypothesis can be tested in isolation first.
"""

from __future__ import annotations

from typing import Optional

from core.enums import Direction
from core.models import Signal
from strategy.indicators import atr, ema
from strategy.resample import resample_to_higher_timeframe
from strategy.strategy_interface import MarketState, Strategy


class MeanReversionStrategy(Strategy):
    def __init__(
        self,
        *,
        ema_period: int = 20,
        atr_period: int = 14,
        entry_threshold_atr: float = 2.0,
        stop_atr_multiplier: float = 1.5,
        htf_minutes: Optional[int] = None,
        htf_fast_ema_period: int = 50,
        htf_slow_ema_period: int = 200,
        htf_atr_period: int = 14,
        max_htf_trend_strength_atr_multiples: Optional[float] = None,
        max_history_candles: Optional[int] = None,
        name: str = "mean_reversion_v1",
    ):
        if entry_threshold_atr <= 0:
            raise ValueError("entry_threshold_atr must be > 0")
        if stop_atr_multiplier <= 0:
            raise ValueError("stop_atr_multiplier must be > 0")

        self.ema_period = ema_period
        self.atr_period = atr_period
        self.entry_threshold_atr = entry_threshold_atr
        self.stop_atr_multiplier = stop_atr_multiplier
        self.htf_minutes = htf_minutes
        self.htf_fast_ema_period = htf_fast_ema_period
        self.htf_slow_ema_period = htf_slow_ema_period
        self.htf_atr_period = htf_atr_period
        self.max_htf_trend_strength_atr_multiples = max_htf_trend_strength_atr_multiples
        self.max_history_candles = max_history_candles or (
            4 * htf_slow_ema_period * ((htf_minutes or 60) // 15 or 1) + 500
        )
        self.name = name

    def generate_signal(self, market_state: MarketState) -> Optional[Signal]:
        history = market_state.history
        if len(history) < max(self.ema_period, self.atr_period) + 1:
            return None
        if len(history) > self.max_history_candles:
            history = history[-self.max_history_candles :]

        closes = [c.close for c in history]
        ema_series = ema(closes, self.ema_period)
        atr_series = atr(history, self.atr_period)

        current = history[-1]
        previous = history[-2]
        prev_ema = ema_series[-2]
        prev_atr = atr_series[-2]
        if prev_ema is None or prev_atr is None or prev_atr <= 0:
            return None

        # Stretch is measured on the PREVIOUS candle, confirmation on the
        # CURRENT one -- measuring both on the same candle means ATR has
        # already expanded from the very move being measured by the time
        # a reversal shows up, deflating the distance below threshold
        # right when it should be triggering.
        prev_distance = (previous.close - prev_ema) / prev_atr

        if prev_distance >= self.entry_threshold_atr and current.close < previous.close:
            direction = Direction.SHORT
        elif prev_distance <= -self.entry_threshold_atr and current.close > previous.close:
            direction = Direction.LONG
        else:
            return None

        current_ema = ema_series[-1]
        current_atr = atr_series[-1]
        if current_ema is None or current_atr is None or current_atr <= 0:
            return None

        if self.max_htf_trend_strength_atr_multiples is not None and self.htf_minutes is not None:
            if self._trend_too_strong_against(history, direction):
                return None

        entry_price = current.close
        stop_distance = current_atr * self.stop_atr_multiplier

        if direction == Direction.LONG:
            stop_loss = entry_price - stop_distance
            take_profit = current_ema
            if take_profit <= entry_price:
                return None  # mean is not actually above entry -- no valid reversion target
        else:
            stop_loss = entry_price + stop_distance
            take_profit = current_ema
            if take_profit >= entry_price:
                return None

        return Signal(
            symbol=market_state.symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strategy_name=self.name,
            metadata={"distance_atr": prev_distance, "atr": current_atr, "ema": current_ema},
        )

    def _trend_too_strong_against(self, history, direction: Direction) -> bool:
        """Returns True if the HTF trend is strong AND in the OPPOSITE
        direction of this reversion trade -- i.e. we'd be fading a real
        trend, not just noise in a range."""
        htf_candles = resample_to_higher_timeframe(history, self.htf_minutes)
        needed = max(self.htf_slow_ema_period, self.htf_atr_period) + 1
        if len(htf_candles) < needed:
            return False  # not enough data to judge -- don't block on an unknown trend

        htf_closes = [c.close for c in htf_candles]
        fast = ema(htf_closes, self.htf_fast_ema_period)[-1]
        slow = ema(htf_closes, self.htf_slow_ema_period)[-1]
        htf_atr = atr(htf_candles, self.htf_atr_period)[-1]
        if fast is None or slow is None or htf_atr is None or htf_atr <= 0:
            return False

        separation = fast - slow
        strength = abs(separation) / htf_atr
        if strength < self.max_htf_trend_strength_atr_multiples:
            return False  # trend isn't strong enough to worry about

        htf_direction = Direction.LONG if separation > 0 else Direction.SHORT
        # A SHORT reversion trade fights an UP trend; a LONG reversion fights a DOWN trend.
        opposing = (direction == Direction.SHORT and htf_direction == Direction.LONG) or (
            direction == Direction.LONG and htf_direction == Direction.SHORT
        )
        return opposing
