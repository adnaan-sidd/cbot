"""
Trend-Pullback Strategy — the real Phase 4 strategy, scratch-built.

Design rationale (this is the "why", not just the "what"):

  - TREND FILTER (H1, resampled from the M15 feed): only trade WITH the
    higher-timeframe trend (EMA50 vs EMA200). Gold trends hard during
    macro/rate moves, and fighting that trend is where most retail
    losses come from. This is the single highest-leverage risk-reduction
    decision in the whole strategy, and it costs nothing to enforce.

  - PULLBACK ENTRY (M15): rather than chasing a breakout (buying strength
    at its worst average entry price), wait for price to dip to the M15
    EMA20 against the trend, then enter on a confirmed bounce back
    through it. This structurally improves the average entry price
    relative to a breakout entry, without needing to predict the pullback
    depth in advance.

  - RSI FILTER: reject entries at momentum extremes in the trade's own
    direction (e.g. don't buy a pullback bounce if RSI is already
    overbought) — this avoids the worst-quality subset of pullback
    entries, where the "bounce" is actually exhaustion.

  - ATR-ADAPTIVE STOP/TARGET: stop-loss = 1.5x ATR(14), take-profit at a
    fixed 2:1 reward:risk multiple of that same ATR-based distance. Using
    ATR instead of a fixed dollar/point distance means the stop
    automatically widens in high-volatility conditions (where a fixed
    stop gets hit by noise) and tightens in quiet ones (where a fixed
    stop risks too much for the actual move available) — this is the
    single change that most improves a stop-loss's real-world quality
    over a flat distance.

None of this touches position sizing, margin, or account-level risk
limits — those remain exclusively in risk_engine, reached only through
risk_gate.evaluate_signal(), exactly as designed in Phase 1.

Performance note (added after real-data validation surfaced this as a
genuine problem, not just a theoretical one): an earlier version of this
strategy recomputed the full H1 resample + EMA/ATR/RSI series from the
ENTIRE candle history on every call. That's O(n) per call, O(n^2) over a
full backtest — a ~230,000-candle real XAUUSD run was estimated at over
14 HOURS before this was fixed. This wasn't just a backtest
inconvenience: a live bot running for months would face the same
unbounded, ever-growing per-tick cost. The fix (`max_history_candles`)
windows the input to a bounded, recent slice before any computation,
making per-call cost constant regardless of how long the bot has been
running — which live trading requires regardless of backtest speed. The
window is sized generously relative to the largest indicator period in
use, so the EMA/ATR/RSI values it produces are numerically
indistinguishable from a full-history calculation: a windowed EMA's
dependence on data outside the window decays exponentially, so a window
a few multiples of the period long makes the truncation's effect
negligible — this is standard practice for bounded-window indicator
calculation in live trading systems generally, not a shortcut specific
to this codebase.
"""

from __future__ import annotations

from typing import Optional

from core.enums import Direction
from core.models import Signal
from strategy.indicators import atr, ema, rsi
from strategy.resample import resample_to_higher_timeframe
from strategy.strategy_interface import MarketState, Strategy


class TrendPullbackStrategy(Strategy):
    def __init__(
        self,
        *,
        htf_minutes: int = 60,
        htf_fast_ema_period: int = 50,
        htf_slow_ema_period: int = 200,
        ltf_fast_ema_period: int = 20,
        atr_period: int = 14,
        atr_stop_multiplier: float = 1.5,
        reward_risk_ratio: float = 2.0,
        rsi_period: int = 14,
        rsi_long_min: float = 40.0,
        rsi_long_max: float = 75.0,
        rsi_short_min: float = 25.0,
        rsi_short_max: float = 60.0,
        min_atr_price: float = 0.0,  # 0.0 = no minimum-volatility filter
        max_history_candles: Optional[int] = None,
        name: str = "trend_pullback_h1_m15",
    ):
        self.htf_minutes = htf_minutes
        self.htf_fast_ema_period = htf_fast_ema_period
        self.htf_slow_ema_period = htf_slow_ema_period
        self.ltf_fast_ema_period = ltf_fast_ema_period
        self.atr_period = atr_period
        self.atr_stop_multiplier = atr_stop_multiplier
        self.reward_risk_ratio = reward_risk_ratio
        self.rsi_period = rsi_period
        self.rsi_long_min = rsi_long_min
        self.rsi_long_max = rsi_long_max
        self.rsi_short_min = rsi_short_min
        self.rsi_short_max = rsi_short_max
        self.min_atr_price = min_atr_price
        # Default: enough M15 candles for ~4x the HTF slow EMA period's
        # worth of H1 bars, plus a fixed buffer for LTF warm-up. Generous
        # on purpose -- see module docstring's performance note.
        self.max_history_candles = max_history_candles or (
            4 * htf_slow_ema_period * (htf_minutes // 15 or 1) + 500
        )
        self.name = name

    def generate_signal(self, market_state: MarketState) -> Optional[Signal]:
        history = market_state.history
        if len(history) < 2:
            return None
        if len(history) > self.max_history_candles:
            history = history[-self.max_history_candles :]

        trend = self._htf_trend(history)
        if trend is None:
            return None

        return self._ltf_entry(market_state.symbol, history, trend)

    # -- higher-timeframe trend filter --------------------------------

    def _htf_trend(self, history: list) -> Optional[Direction]:
        htf_candles = resample_to_higher_timeframe(history, self.htf_minutes)
        if len(htf_candles) < self.htf_slow_ema_period:
            return None

        htf_closes = [c.close for c in htf_candles]
        fast = ema(htf_closes, self.htf_fast_ema_period)[-1]
        slow = ema(htf_closes, self.htf_slow_ema_period)[-1]
        if fast is None or slow is None or fast == slow:
            return None
        return Direction.LONG if fast > slow else Direction.SHORT

    # -- lower-timeframe pullback entry --------------------------------

    def _ltf_entry(self, symbol: str, history: list, trend: Direction) -> Optional[Signal]:
        closes = [c.close for c in history]

        fast_ema_series = ema(closes, self.ltf_fast_ema_period)
        if len(fast_ema_series) < 2 or fast_ema_series[-1] is None or fast_ema_series[-2] is None:
            return None

        current = history[-1]
        previous = history[-2]
        current_ema = fast_ema_series[-1]
        previous_ema = fast_ema_series[-2]

        if trend == Direction.LONG:
            pulled_back = previous.close <= previous_ema
            confirmed = current.close > current_ema and current.close > current.open
            if not (pulled_back and confirmed):
                return None
        else:
            pulled_back = previous.close >= previous_ema
            confirmed = current.close < current_ema and current.close < current.open
            if not (pulled_back and confirmed):
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
        if current_atr < self.min_atr_price:
            return None

        entry_price = current.close
        stop_distance = current_atr * self.atr_stop_multiplier
        target_distance = stop_distance * self.reward_risk_ratio

        if trend == Direction.LONG:
            stop_loss = entry_price - stop_distance
            take_profit = entry_price + target_distance
        else:
            stop_loss = entry_price + stop_distance
            take_profit = entry_price - target_distance

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
                "ltf_fast_ema": current_ema,
            },
        )
