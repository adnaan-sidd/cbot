"""
TrendPullbackStrategy tests. Indicator math itself is already hand-verified
in test_indicators.py and test_resample.py; these tests instead verify the
strategy's DECISION LOGIC: it only signals with the H1 trend, only on a
confirmed pullback, respects the RSI filter, and places stop/target on the
correct side using ATR — using small, engineered candle sequences with
tiny indicator periods (so tests don't need thousands of candles to reach
a real 50/200-period EMA's warm-up).
"""

from datetime import datetime, timedelta

import pytest

from core_enums import Direction
from core_models import Candle
from strategy_interface import MarketState
from strategies import TrendPullbackStrategy


def _build_candles(closes: list[float], *, minutes: int = 15) -> list[Candle]:
    """Builds candles with realistic OHLC continuity: each candle's open
    is the previous candle's close. This matters a lot here — a candle
    helper that defaults open=close makes the strategy's bullish/bearish
    confirmation check (close vs. open) permanently impossible to
    satisfy, silently breaking every entry-side test."""
    candles = []
    prev_close = closes[0] - 1.0
    for i, c in enumerate(closes):
        o = prev_close
        h = max(o, c) + 0.3
        l = min(o, c) - 0.3
        ts = datetime(2026, 1, 5) + timedelta(minutes=minutes * i)  # Monday 00:00 start
        candles.append(Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=10))
        prev_close = c
    return candles


def _zigzag_uptrend(n: int, start: float = 100.0) -> list[Candle]:
    """Net-rising price path with periodic 1-candle pullbacks — an
    actual pullback needs a real dip relative to the fast EMA, which a
    perfectly monotonic move never produces (price is always above an
    EMA that's lagging behind a straight climb)."""
    closes = []
    price = start
    for i in range(n):
        if i % 4 == 3:
            price -= 1.5
        else:
            price += 1.0
        closes.append(price)
    return _build_candles(closes)


def _zigzag_downtrend(n: int, start: float = 200.0) -> list[Candle]:
    closes = []
    price = start
    for i in range(n):
        if i % 4 == 3:
            price += 1.5
        else:
            price -= 1.0
        closes.append(price)
    return _build_candles(closes)


def _flat_candles(n: int, price: float = 100.0) -> list[Candle]:
    return _build_candles([price] * n)


def _small_strategy(**overrides) -> TrendPullbackStrategy:
    """Tiny periods so tests don't need enormous synthetic histories to
    exercise the real decision logic."""
    kwargs = dict(
        htf_minutes=60,       # 4 M15 candles per H1 bucket
        htf_fast_ema_period=2,
        htf_slow_ema_period=3,
        ltf_fast_ema_period=2,
        atr_period=2,
        atr_stop_multiplier=1.5,
        reward_risk_ratio=2.0,
        rsi_period=2,
        rsi_long_min=0.0,
        rsi_long_max=100.0,   # disable RSI filtering by default in most tests
        rsi_short_min=0.0,
        rsi_short_max=100.0,
    )
    kwargs.update(overrides)
    return TrendPullbackStrategy(**kwargs)


def _run(strat: TrendPullbackStrategy, candles: list[Candle]) -> list:
    history: list[Candle] = []
    signals = []
    for c in candles:
        history.append(c)
        s = strat.generate_signal(MarketState(symbol="XAUUSD", history=history))
        if s:
            signals.append(s)
    return signals


class TestNoSignalBeforeEnoughHistory:
    def test_too_little_history_returns_none(self):
        strat = _small_strategy()
        history = _zigzag_uptrend(3)
        signal = strat.generate_signal(MarketState(symbol="XAUUSD", history=history))
        assert signal is None

    def test_not_enough_htf_candles_returns_none(self):
        # htf_slow_ema_period=3 needs at least 3 CLOSED H1 candles ->
        # at least 4*3 + 1 = 13 M15 candles just to resample enough,
        # plus warm-up for the LTF indicators on top of that.
        strat = _small_strategy()
        history = _zigzag_uptrend(8)  # not enough for 3 closed H1 bars yet
        signal = strat.generate_signal(MarketState(symbol="XAUUSD", history=history))
        assert signal is None


class TestTrendDetection:
    def test_zigzag_uptrend_produces_at_least_one_long_signal_never_short(self):
        strat = _small_strategy()
        signals = _run(strat, _zigzag_uptrend(60))
        assert len(signals) > 0  # confirms the pattern actually exercises entry logic
        assert all(s.direction == Direction.LONG for s in signals)

    def test_zigzag_downtrend_produces_at_least_one_short_signal_never_long(self):
        strat = _small_strategy()
        signals = _run(strat, _zigzag_downtrend(60))
        assert len(signals) > 0
        assert all(s.direction == Direction.SHORT for s in signals)


class TestSignalStructure:
    def _first_long_signal(self, strat=None):
        strat = strat or _small_strategy()
        signals = _run(strat, _zigzag_uptrend(60))
        return signals[0] if signals else None

    def test_long_signal_has_stop_below_entry_and_target_above(self):
        signal = self._first_long_signal()
        assert signal is not None
        assert signal.stop_loss < signal.entry_price
        assert signal.take_profit > signal.entry_price

    def test_long_signal_reward_risk_ratio_matches_config(self):
        strat = _small_strategy(reward_risk_ratio=2.0)
        signal = self._first_long_signal(strat)
        assert signal is not None
        risk = signal.entry_price - signal.stop_loss
        reward = signal.take_profit - signal.entry_price
        assert reward == pytest.approx(risk * 2.0, rel=1e-6)

    def test_different_reward_risk_ratio_is_respected(self):
        strat = _small_strategy(reward_risk_ratio=3.0)
        signal = self._first_long_signal(strat)
        assert signal is not None
        risk = signal.entry_price - signal.stop_loss
        reward = signal.take_profit - signal.entry_price
        assert reward == pytest.approx(risk * 3.0, rel=1e-6)

    def test_stop_distance_matches_atr_multiplier(self):
        strat = _small_strategy(atr_stop_multiplier=1.5)
        signal = self._first_long_signal(strat)
        assert signal is not None
        risk = signal.entry_price - signal.stop_loss
        assert risk == pytest.approx(signal.metadata["atr"] * 1.5, rel=1e-6)

    def test_signal_metadata_includes_indicator_values(self):
        signal = self._first_long_signal()
        assert signal is not None
        assert "atr" in signal.metadata
        assert "rsi" in signal.metadata
        assert signal.metadata["atr"] > 0

    def test_strategy_name_is_set(self):
        strat = _small_strategy(name="custom_test_name")
        signal = self._first_long_signal(strat)
        assert signal is not None
        assert signal.strategy_name == "custom_test_name"


class TestShortSignalStructure:
    def test_short_signal_has_stop_above_entry_and_target_below(self):
        strat = _small_strategy()
        signals = _run(strat, _zigzag_downtrend(60))
        assert len(signals) > 0
        signal = signals[0]
        assert signal.stop_loss > signal.entry_price
        assert signal.take_profit < signal.entry_price


class TestRSIFilter:
    def test_narrow_rsi_band_blocks_entries_the_wide_band_allows(self):
        """Same zigzag uptrend, but with an RSI band narrow enough to
        exclude every entry -- proves the RSI filter is load-bearing,
        by comparing directly against the permissive baseline that DOES
        produce signals on identical price data."""
        wide_signals = _run(_small_strategy(rsi_long_min=0.0, rsi_long_max=100.0), _zigzag_uptrend(60))
        narrow_signals = _run(_small_strategy(rsi_long_min=99.0, rsi_long_max=100.0), _zigzag_uptrend(60))
        assert len(wide_signals) > 0
        assert len(narrow_signals) == 0


class TestMinimumATRFilter:
    def test_impossibly_high_minimum_atr_blocks_every_entry(self):
        strat = _small_strategy(min_atr_price=10_000.0)
        signals = _run(strat, _zigzag_uptrend(60))
        assert signals == []

    def test_zero_minimum_atr_does_not_block_entries(self):
        strat = _small_strategy(min_atr_price=0.0)
        signals = _run(strat, _zigzag_uptrend(60))
        assert len(signals) > 0


class TestFlatMarketProducesNoSignal:
    def test_completely_flat_prices_produce_no_trend_and_no_signal(self):
        strat = _small_strategy()
        signals = _run(strat, _flat_candles(60))
        # fast EMA == slow EMA on perfectly flat data -> no trend -> no signal
        assert signals == []


class TestHistoryWindowing:
    """Covers the performance fix added after real-data validation
    testing showed unbounded full-history recomputation made a
    ~230,000-candle backtest infeasible (>14 hours estimated). See the
    module docstring's performance note."""

    def test_default_window_is_derived_from_htf_slow_ema_period(self):
        strat = TrendPullbackStrategy(htf_minutes=60, htf_slow_ema_period=200)
        # 4 * 200 * (60//15) + 500 = 4*200*4+500 = 3700
        assert strat.max_history_candles == 3700

    def test_explicit_window_overrides_the_default(self):
        strat = TrendPullbackStrategy(max_history_candles=500)
        assert strat.max_history_candles == 500

    def test_history_longer_than_window_does_not_crash_and_still_signals(self):
        """The core regression test: feeding far more history than the
        window size must not error, and must still produce signals --
        proving the strategy correctly operates on the tail slice."""
        strat = _small_strategy(max_history_candles=30)
        # zigzag pattern repeated well beyond the window size
        candles = _zigzag_uptrend(120)
        signals = _run(strat, candles)
        assert len(signals) > 0
        assert all(s.direction == Direction.LONG for s in signals)

    def test_per_call_cost_does_not_grow_with_total_history_length(self):
        """Direct proof of the O(n) vs O(n^2) fix: time the LAST call
        with a short windowed strategy against a very long history, and
        confirm it's not meaningfully slower than an early call -- if
        the window weren't applied, this call would be doing far more
        work than the early one."""
        import time

        strat = _small_strategy(max_history_candles=20)
        candles = _zigzag_uptrend(2000)

        history = []
        for c in candles[:25]:
            history.append(c)
        start = time.perf_counter()
        strat.generate_signal(MarketState(symbol="XAUUSD", history=history))
        early_call_time = time.perf_counter() - start

        history = list(candles)  # full 2000-candle history
        start = time.perf_counter()
        strat.generate_signal(MarketState(symbol="XAUUSD", history=history))
        late_call_time = time.perf_counter() - start

        # Generous tolerance -- the point isn't precise timing equality,
        # it's that late_call_time doesn't scale with total history size
        # (2000/25 = 80x more history) the way an unwindowed O(n) call would.
        assert late_call_time < early_call_time * 10
