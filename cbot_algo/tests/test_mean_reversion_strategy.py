from datetime import datetime, timedelta

import pytest

from core_enums import Direction
from core_models import Candle
from strategies import MeanReversionStrategy
from strategy_interface import MarketState


def _build_candles(closes: list[float], *, minutes: int = 15) -> list[Candle]:
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


def _stretch_and_reverse_up(*, flat_len=30, ramp_len=7, ramp_step=0.3, spike=3.0, pullback=1.0) -> list[Candle]:
    closes = [100.0] * flat_len
    closes += [100.0 + i * ramp_step for i in range(1, ramp_len + 1)]
    closes.append(closes[-1] + spike)
    closes.append(closes[-1] - pullback)
    return _build_candles(closes)


def _stretch_and_reverse_down(*, flat_len=30, ramp_len=7, ramp_step=0.3, spike=3.0, bounce=1.0) -> list[Candle]:
    closes = [200.0] * flat_len
    closes += [200.0 - i * ramp_step for i in range(1, ramp_len + 1)]
    closes.append(closes[-1] - spike)
    closes.append(closes[-1] + bounce)
    return _build_candles(closes)


def _small_strategy(**overrides) -> MeanReversionStrategy:
    kwargs = dict(ema_period=5, atr_period=3, entry_threshold_atr=1.0, stop_atr_multiplier=1.5)
    kwargs.update(overrides)
    return MeanReversionStrategy(**kwargs)


def _run(strat: MeanReversionStrategy, candles: list[Candle]):
    history: list[Candle] = []
    for c in candles:
        history.append(c)
        s = strat.generate_signal(MarketState(symbol="XAUUSD", history=history))
        if s:
            return s
    return None


class TestConstructionValidation:
    def test_zero_threshold_rejected(self):
        with pytest.raises(ValueError):
            MeanReversionStrategy(entry_threshold_atr=0)

    def test_zero_stop_multiplier_rejected(self):
        with pytest.raises(ValueError):
            MeanReversionStrategy(stop_atr_multiplier=0)


class TestEntryLogic:
    def test_stretch_up_then_reversal_produces_short(self):
        strat = _small_strategy()
        signal = _run(strat, _stretch_and_reverse_up())
        assert signal is not None
        assert signal.direction == Direction.SHORT

    def test_stretch_down_then_reversal_produces_long(self):
        strat = _small_strategy()
        signal = _run(strat, _stretch_and_reverse_down())
        assert signal is not None
        assert signal.direction == Direction.LONG

    def test_stretch_without_reversal_produces_no_signal(self):
        """The spike candle itself, continuing further in the same
        direction (no pullback), must not trigger -- confirmation is
        required, not just distance."""
        strat = _small_strategy()
        closes = [100.0] * 30 + [100.0 + i * 0.3 for i in range(1, 8)]
        closes.append(closes[-1] + 3.0)
        closes.append(closes[-1] + 0.5)  # continues UP, no reversal
        candles = _build_candles(closes)
        signal = _run(strat, candles)
        assert signal is None

    def test_insufficient_stretch_produces_no_signal(self):
        strat = _small_strategy(entry_threshold_atr=10.0)  # essentially unreachable
        signal = _run(strat, _stretch_and_reverse_up())
        assert signal is None

    def test_flat_market_produces_no_signal(self):
        strat = _small_strategy()
        candles = _build_candles([100.0] * 40)
        signal = _run(strat, candles)
        assert signal is None


class TestSignalStructure:
    def test_short_stop_above_entry_target_below(self):
        strat = _small_strategy()
        signal = _run(strat, _stretch_and_reverse_up())
        assert signal is not None
        assert signal.stop_loss > signal.entry_price
        assert signal.take_profit < signal.entry_price

    def test_long_stop_below_entry_target_above(self):
        strat = _small_strategy()
        signal = _run(strat, _stretch_and_reverse_down())
        assert signal is not None
        assert signal.stop_loss < signal.entry_price
        assert signal.take_profit > signal.entry_price

    def test_stop_distance_matches_atr_multiplier(self):
        strat = _small_strategy(stop_atr_multiplier=2.0)
        signal = _run(strat, _stretch_and_reverse_up())
        assert signal is not None
        risk = signal.stop_loss - signal.entry_price
        assert risk == pytest.approx(signal.metadata["atr"] * 2.0, rel=1e-6)

    def test_take_profit_equals_ema_at_entry(self):
        strat = _small_strategy()
        signal = _run(strat, _stretch_and_reverse_up())
        assert signal is not None
        assert signal.take_profit == pytest.approx(signal.metadata["ema"], rel=1e-9)

    def test_metadata_includes_distance(self):
        strat = _small_strategy()
        signal = _run(strat, _stretch_and_reverse_up())
        assert signal is not None
        assert signal.metadata["distance_atr"] >= 1.0


class TestHTFTrendFilter:
    def test_no_filter_by_default(self):
        strat = _small_strategy()  # htf_minutes=None -> filter inactive
        signal = _run(strat, _stretch_and_reverse_up())
        assert signal is not None

    def test_filter_inactive_without_enough_htf_history(self):
        """The filter should never block a trade just because there
        isn't enough HTF history to judge yet -- absence of trend
        information is not evidence of a strong opposing trend."""
        strat = _small_strategy(
            htf_minutes=60, htf_fast_ema_period=50, htf_slow_ema_period=200,
            max_htf_trend_strength_atr_multiples=0.5,
        )
        signal = _run(strat, _stretch_and_reverse_up())
        assert signal is not None  # scenario has far fewer than 200 H1 bars worth of data
