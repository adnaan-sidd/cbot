"""
TrendPullbackBreakoutStrategy (v2) tests. As with v1's tests, indicator
math itself is already hand-verified elsewhere (test_indicators.py,
test_resample.py) — these tests verify the NEW decision logic specific
to v2: the trend-strength filter, multi-bar pullback bar-count bounds,
and the structural breakout-of-pullback-extreme trigger (as opposed to
v1's bare EMA-cross trigger). Every scenario here was verified against
the actual implementation via an interactive debugging script before
being written as an assertion, following the same discipline used for
v1 (whose first test-writing pass had a silent candle-construction bug
that made every entry-side test vacuously fail).
"""

from datetime import datetime, timedelta

import pytest

from core_enums import Direction
from core_models import Candle
from strategy_interface import MarketState
from strategies import TrendPullbackBreakoutStrategy


def _build_candles(closes: list[float], *, minutes: int = 15) -> list[Candle]:
    """Realistic OHLC continuity: each candle's open is the previous
    candle's close — see v1's test suite for why this matters (a
    close==open default silently breaks any close-vs-open confirmation
    check)."""
    candles = []
    prev_close = closes[0] - 1.0
    for i, c in enumerate(closes):
        o = prev_close
        h = max(o, c) + 0.3
        l = min(o, c) - 0.3
        ts = datetime(2026, 1, 5) + timedelta(minutes=minutes * i)
        candles.append(Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=10))
        prev_close = c
    return candles


def _uptrend_pullback_scenario(pullback_bars: int, *, breakout_jump: float = 3.0) -> list[Candle]:
    closes = []
    price = 100.0
    for _ in range(200):
        price += 0.8
        closes.append(price)
    for _ in range(pullback_bars):
        price -= 1.2
        closes.append(price)
    price += breakout_jump
    closes.append(price)
    for _ in range(5):
        price += 0.5
        closes.append(price)
    return _build_candles(closes)


def _downtrend_pullback_scenario(pullback_bars: int, *, breakout_jump: float = 3.0) -> list[Candle]:
    closes = []
    price = 300.0
    for _ in range(200):
        price -= 0.8
        closes.append(price)
    for _ in range(pullback_bars):
        price += 1.2
        closes.append(price)
    price -= breakout_jump
    closes.append(price)
    for _ in range(5):
        price -= 0.5
        closes.append(price)
    return _build_candles(closes)


def _weak_trend_scenario() -> list[Candle]:
    closes = []
    price = 100.0
    for _ in range(200):
        price += 0.01
        closes.append(price)
    for _ in range(2):
        price -= 0.02
        closes.append(price)
    price += 0.1
    closes.append(price)
    for _ in range(5):
        price += 0.01
        closes.append(price)
    return _build_candles(closes)


def _small_strategy(**overrides) -> TrendPullbackBreakoutStrategy:
    kwargs = dict(
        htf_minutes=60, htf_fast_ema_period=2, htf_slow_ema_period=3, htf_atr_period=2,
        min_trend_strength_atr_multiples=0.01,
        ltf_fast_ema_period=5, min_pullback_bars=2, max_pullback_bars=5,
        atr_period=2, stop_buffer_atr_multiplier=0.2, reward_risk_ratio=2.0, rsi_period=3,
        rsi_long_min=0.0, rsi_long_max=100.0, rsi_short_min=0.0, rsi_short_max=100.0,
    )
    kwargs.update(overrides)
    return TrendPullbackBreakoutStrategy(**kwargs)


def _run(strat: TrendPullbackBreakoutStrategy, candles: list[Candle]):
    history: list[Candle] = []
    for c in candles:
        history.append(c)
        s = strat.generate_signal(MarketState(symbol="XAUUSD", history=history))
        if s:
            return s
    return None


class TestConstructionValidation:
    def test_min_pullback_bars_must_be_at_least_one(self):
        with pytest.raises(ValueError):
            TrendPullbackBreakoutStrategy(min_pullback_bars=0)

    def test_max_must_be_at_least_min(self):
        with pytest.raises(ValueError):
            TrendPullbackBreakoutStrategy(min_pullback_bars=5, max_pullback_bars=3)


class TestTrendStrengthFilter:
    def test_weak_trend_produces_no_signal(self):
        strat = _small_strategy(min_trend_strength_atr_multiples=1.0)
        signal = _run(strat, _weak_trend_scenario())
        assert signal is None

    def test_strong_trend_with_valid_pullback_produces_a_signal(self):
        strat = _small_strategy()
        signal = _run(strat, _uptrend_pullback_scenario(pullback_bars=3))
        assert signal is not None


class TestPullbackBarCountBounds:
    def test_pullback_shorter_than_minimum_rejected(self):
        strat = _small_strategy(min_pullback_bars=2, max_pullback_bars=5)
        signal = _run(strat, _uptrend_pullback_scenario(pullback_bars=1))
        assert signal is None

    def test_pullback_longer_than_maximum_rejected(self):
        strat = _small_strategy(min_pullback_bars=2, max_pullback_bars=3)
        signal = _run(strat, _uptrend_pullback_scenario(pullback_bars=6, breakout_jump=8.0))
        assert signal is None

    def test_pullback_within_widened_maximum_accepted(self):
        strat = _small_strategy(min_pullback_bars=2, max_pullback_bars=8)
        signal = _run(strat, _uptrend_pullback_scenario(pullback_bars=6, breakout_jump=8.0))
        assert signal is not None


class TestBreakoutRequirement:
    def test_valid_setup_produces_long_signal(self):
        strat = _small_strategy()
        signal = _run(strat, _uptrend_pullback_scenario(pullback_bars=3))
        assert signal is not None
        assert signal.direction == Direction.LONG

    def test_insufficient_bounce_does_not_clear_the_pullback_high(self):
        """A weak bounce that doesn't actually break the pullback's own
        high must NOT trigger -- this is the core structural difference
        from v1's bare EMA-cross trigger."""
        strat = _small_strategy(max_pullback_bars=8)
        # Deep pullback (6 bars, ~7.2 points) with a bounce too small
        # (1.0) to break back above the pullback's high.
        signal = _run(strat, _uptrend_pullback_scenario(pullback_bars=6, breakout_jump=1.0))
        assert signal is None

    def test_downtrend_mirror_produces_short_signal(self):
        strat = _small_strategy()
        signal = _run(strat, _downtrend_pullback_scenario(pullback_bars=3))
        assert signal is not None
        assert signal.direction == Direction.SHORT


class TestStructuralStopPlacement:
    """The other core difference from v1: stop is placed beyond the
    pullback's actual extreme (plus a small ATR buffer), not at a fixed
    ATR multiple from entry."""

    def test_long_stop_matches_pullback_extreme_minus_buffer(self):
        strat = _small_strategy(stop_buffer_atr_multiplier=0.2)
        signal = _run(strat, _uptrend_pullback_scenario(pullback_bars=3))
        assert signal is not None
        expected_stop = signal.metadata["pullback_extreme"] - signal.metadata["atr"] * 0.2
        assert signal.stop_loss == pytest.approx(expected_stop, rel=1e-6)

    def test_short_stop_matches_pullback_extreme_plus_buffer(self):
        strat = _small_strategy(stop_buffer_atr_multiplier=0.2)
        signal = _run(strat, _downtrend_pullback_scenario(pullback_bars=3))
        assert signal is not None
        expected_stop = signal.metadata["pullback_extreme"] + signal.metadata["atr"] * 0.2
        assert signal.stop_loss == pytest.approx(expected_stop, rel=1e-6)

    def test_reward_risk_ratio_respected(self):
        strat = _small_strategy(reward_risk_ratio=3.0)
        signal = _run(strat, _uptrend_pullback_scenario(pullback_bars=3))
        assert signal is not None
        risk = signal.entry_price - signal.stop_loss
        reward = signal.take_profit - signal.entry_price
        assert reward == pytest.approx(risk * 3.0, rel=1e-6)


class TestRSIFilter:
    def test_narrow_band_blocks_a_signal_the_wide_band_allows(self):
        wide = _run(_small_strategy(), _uptrend_pullback_scenario(pullback_bars=3))
        narrow = _run(
            _small_strategy(rsi_long_min=0.0, rsi_long_max=10.0),
            _uptrend_pullback_scenario(pullback_bars=3),
        )
        assert wide is not None
        assert narrow is None


class TestMetadata:
    def test_signal_includes_pullback_diagnostics(self):
        strat = _small_strategy()
        signal = _run(strat, _uptrend_pullback_scenario(pullback_bars=3))
        assert signal is not None
        assert "pullback_bars" in signal.metadata
        assert "breakout_level" in signal.metadata
        assert "pullback_extreme" in signal.metadata
        assert signal.metadata["pullback_bars"] >= 2


class TestHistoryWindowing:
    def test_default_window_derived_from_htf_slow_ema_period(self):
        strat = TrendPullbackBreakoutStrategy(htf_minutes=60, htf_slow_ema_period=200)
        assert strat.max_history_candles == 4 * 200 * 4 + 500

    def test_insufficient_history_returns_none(self):
        strat = _small_strategy()
        short_history = _uptrend_pullback_scenario(pullback_bars=3)[:5]
        signal = strat.generate_signal(MarketState(symbol="XAUUSD", history=short_history))
        assert signal is None
