"""Strategy module — cBot port of xauusd_bot/strategy/.

All four strategies (placeholder, trend-pullback v1, trend-pullback
breakout v2, mean reversion v1) are ported verbatim — same indicator
math, same entry/exit construction, same bounded-window performance
guards — plus a factory that maps the cBot's "Strategy" parameter onto
them.
"""

from __future__ import annotations

from typing import Optional

from core_enums import Direction
from core_models import Candle, Signal
from indicators import atr, ema, rsi
from resample import resample_to_higher_timeframe
from strategy_interface import MarketState, Strategy


# ================= AlternatingIntervalStrategy (placeholder) =================
class AlternatingIntervalStrategy(Strategy):
    """Emits a signal every `interval` candles, alternating long/short,
    with a fixed stop-loss and take-profit distance from the candle's
    close. Fully deterministic given a candle sequence — no indicators,
    no randomness, no lookback beyond the current candle."""

    def __init__(
        self,
        *,
        interval: int = 10,
        stop_distance: float = 10.0,
        take_profit_distance: Optional[float] = 20.0,
        name: str = "placeholder_alternating_TEST_ONLY",
    ):
        if interval <= 0:
            raise ValueError("interval must be > 0")
        if stop_distance <= 0:
            raise ValueError("stop_distance must be > 0")
        self.interval = interval
        self.stop_distance = stop_distance
        self.take_profit_distance = take_profit_distance
        self.name = name
        self._candle_count = 0

    def generate_signal(self, market_state: MarketState) -> Optional[Signal]:
        self._candle_count += 1
        if self._candle_count % self.interval != 0:
            return None

        candle = market_state.current
        signal_number = self._candle_count // self.interval
        direction = Direction.LONG if signal_number % 2 == 1 else Direction.SHORT
        entry_price = candle.close

        if direction == Direction.LONG:
            stop_loss = entry_price - self.stop_distance
            take_profit = (
                entry_price + self.take_profit_distance
                if self.take_profit_distance is not None
                else None
            )
        else:
            stop_loss = entry_price + self.stop_distance
            take_profit = (
                entry_price - self.take_profit_distance
                if self.take_profit_distance is not None
                else None
            )

        return Signal(
            symbol=market_state.symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strategy_name=self.name,
        )



# ================= TrendPullbackStrategy (v1) =================
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



# ================= TrendPullbackBreakoutStrategy (v2) =================
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



# ================= MeanReversionStrategy =================
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




# ---------------------------------------------------------------------
# Strategy factory — maps the cBot's "Strategy" parameter to the
# corresponding xauusd_bot strategy class, with that strategy's
# parameters passed straight through. The strategy code itself is
# unchanged from xauusd_bot/strategy/.
# ---------------------------------------------------------------------

STRATEGY_NAMES = (
    "trend_pullback_breakout_v2",
    "trend_pullback_h1_m15",
    "mean_reversion_v1",
    "placeholder_alternating_TEST_ONLY",
)


def build_strategy(name, params):
    """`params` is a dict of ALL cBot parameter values (as read from the
    C# [Parameter] properties). Each strategy only reads the keys it
    knows about; unknown keys are ignored, matching how a YAML section
    applies only to the section that parses it."""
    if name == "trend_pullback_breakout_v2":
        return TrendPullbackBreakoutStrategy(
            htf_minutes=int(params["T2HtfMinutes"]),
            htf_fast_ema_period=int(params["T2HtfFastEma"]),
            htf_slow_ema_period=int(params["T2HtfSlowEma"]),
            htf_atr_period=int(params["T2HtfAtrPeriod"]),
            min_trend_strength_atr_multiples=float(params["T2MinTrendStrengthAtr"]),
            ltf_fast_ema_period=int(params["T2LtfFastEma"]),
            min_pullback_bars=int(params["T2MinPullbackBars"]),
            max_pullback_bars=int(params["T2MaxPullbackBars"]),
            atr_period=int(params["T2AtrPeriod"]),
            stop_buffer_atr_multiplier=float(params["T2StopBufferAtrMult"]),
            reward_risk_ratio=float(params["T2RewardRisk"]),
            rsi_period=int(params["T2RsiPeriod"]),
            rsi_long_min=float(params["T2RsiLongMin"]),
            rsi_long_max=float(params["T2RsiLongMax"]),
            rsi_short_min=float(params["T2RsiShortMin"]),
            rsi_short_max=float(params["T2RsiShortMax"]),
            use_trailing_stop=bool(params["T2UseTrailingStop"]),
            name="trend_pullback_breakout_v2",
        )
    if name == "trend_pullback_h1_m15":
        return TrendPullbackStrategy(
            htf_minutes=int(params["TpHtfMinutes"]),
            htf_fast_ema_period=int(params["TpHtfFastEma"]),
            htf_slow_ema_period=int(params["TpHtfSlowEma"]),
            ltf_fast_ema_period=int(params["TpLtfFastEma"]),
            atr_period=int(params["TpAtrPeriod"]),
            atr_stop_multiplier=float(params["TpAtrStopMult"]),
            reward_risk_ratio=float(params["TpRewardRisk"]),
            rsi_period=int(params["TpRsiPeriod"]),
            rsi_long_min=float(params["TpRsiLongMin"]),
            rsi_long_max=float(params["TpRsiLongMax"]),
            rsi_short_min=float(params["TpRsiShortMin"]),
            rsi_short_max=float(params["TpRsiShortMax"]),
            min_atr_price=float(params["TpMinAtrPrice"]),
            name="trend_pullback_h1_m15",
        )
    if name == "mean_reversion_v1":
        htf_minutes = int(params["MrHtfMinutes"])
        return MeanReversionStrategy(
            ema_period=int(params["MrEmaPeriod"]),
            atr_period=int(params["MrAtrPeriod"]),
            entry_threshold_atr=float(params["MrEntryThresholdAtr"]),
            stop_atr_multiplier=float(params["MrStopAtrMult"]),
            htf_minutes=htf_minutes if htf_minutes > 0 else None,
            htf_fast_ema_period=int(params["MrHtfFastEma"]),
            htf_slow_ema_period=int(params["MrHtfSlowEma"]),
            htf_atr_period=int(params["MrHtfAtrPeriod"]),
            max_htf_trend_strength_atr_multiples=(
                float(params["MrMaxHtfTrendStrength"])
                if float(params["MrMaxHtfTrendStrength"]) > 0
                else None
            ),
            name="mean_reversion_v1",
        )
    if name == "placeholder_alternating_TEST_ONLY":
        tp_dist = float(params["PhTakeProfitDistance"])
        return AlternatingIntervalStrategy(
            interval=int(params["PhInterval"]),
            stop_distance=float(params["PhStopDistance"]),
            take_profit_distance=tp_dist if tp_dist > 0 else None,
            name="placeholder_alternating_TEST_ONLY",
        )
    raise ValueError(
        f"unknown strategy {name!r}; expected one of {', '.join(STRATEGY_NAMES)}"
    )
