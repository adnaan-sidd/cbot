from datetime import datetime, timedelta

import pytest

from core_enums import Direction
from core_models import Candle
from strategies import AlternatingIntervalStrategy
from strategy_interface import MarketState


def _candle(i: int, close: float = 3600.0) -> Candle:
    ts = datetime(2026, 1, 1) + timedelta(minutes=i)
    return Candle(timestamp=ts, open=close, high=close + 1, low=close - 1, close=close, volume=10)


class TestAlternatingIntervalStrategy:
    def test_no_signal_before_interval_reached(self):
        strat = AlternatingIntervalStrategy(interval=3, stop_distance=10.0)
        history = [_candle(0)]
        assert strat.generate_signal(MarketState(symbol="XAUUSD", history=history)) is None
        history.append(_candle(1))
        assert strat.generate_signal(MarketState(symbol="XAUUSD", history=history)) is None

    def test_signal_fires_at_interval(self):
        strat = AlternatingIntervalStrategy(interval=3, stop_distance=10.0, take_profit_distance=20.0)
        history = []
        signal = None
        for i in range(3):
            history.append(_candle(i))
            signal = strat.generate_signal(MarketState(symbol="XAUUSD", history=history))
        assert signal is not None
        assert signal.direction == Direction.LONG
        assert signal.stop_loss == pytest.approx(3590.0)
        assert signal.take_profit == pytest.approx(3620.0)

    def test_signals_alternate_direction(self):
        strat = AlternatingIntervalStrategy(interval=2, stop_distance=10.0)
        history = []
        signals = []
        for i in range(6):
            history.append(_candle(i))
            s = strat.generate_signal(MarketState(symbol="XAUUSD", history=history))
            if s:
                signals.append(s)
        assert len(signals) == 3
        assert [s.direction for s in signals] == [Direction.LONG, Direction.SHORT, Direction.LONG]

    def test_short_signal_stop_and_target_on_correct_side(self):
        strat = AlternatingIntervalStrategy(interval=1, stop_distance=10.0, take_profit_distance=20.0)
        history = [_candle(0)]
        first = strat.generate_signal(MarketState(symbol="XAUUSD", history=history))  # LONG
        history.append(_candle(1))
        second = strat.generate_signal(MarketState(symbol="XAUUSD", history=history))  # SHORT
        assert second.direction == Direction.SHORT
        assert second.stop_loss > second.entry_price
        assert second.take_profit < second.entry_price

    def test_none_take_profit_distance_produces_no_take_profit(self):
        strat = AlternatingIntervalStrategy(interval=1, stop_distance=10.0, take_profit_distance=None)
        history = [_candle(0)]
        signal = strat.generate_signal(MarketState(symbol="XAUUSD", history=history))
        assert signal.take_profit is None

    def test_invalid_interval_rejected(self):
        with pytest.raises(ValueError):
            AlternatingIntervalStrategy(interval=0)

    def test_invalid_stop_distance_rejected(self):
        with pytest.raises(ValueError):
            AlternatingIntervalStrategy(interval=1, stop_distance=0)
