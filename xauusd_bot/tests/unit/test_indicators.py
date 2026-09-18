"""
Indicator tests. EMA/ATR/RSI values below are computed by hand for short
sequences (not just trusted to whatever the code outputs) so a formula
bug — wrong smoothing constant, off-by-one warm-up, wrong seed — would
break an exact-number assertion.
"""

from datetime import datetime, timedelta

import pytest

from core.models import Candle
from strategy.indicators import atr, ema, rsi


def _candle(i: int, high: float, low: float, close: float) -> Candle:
    ts = datetime(2026, 1, 1) + timedelta(minutes=15 * i)
    return Candle(timestamp=ts, open=close, high=high, low=low, close=close, volume=10)


class TestEMA:
    def test_insufficient_data_is_none(self):
        result = ema([1, 2], period=5)
        assert result == [None, None]

    def test_seed_is_simple_average_of_first_period(self):
        # period=3, first 3 values [1,2,3] -> seed = 2.0
        result = ema([1, 2, 3], period=3)
        assert result == [None, None, pytest.approx(2.0)]

    def test_hand_computed_sequence(self):
        """values = [1,2,3,4,5], period=3
        seed (index2) = (1+2+3)/3 = 2.0
        multiplier = 2/(3+1) = 0.5
        index3: (4-2.0)*0.5+2.0 = 3.0
        index4: (5-3.0)*0.5+3.0 = 4.0
        """
        result = ema([1, 2, 3, 4, 5], period=3)
        assert result[2] == pytest.approx(2.0)
        assert result[3] == pytest.approx(3.0)
        assert result[4] == pytest.approx(4.0)

    def test_invalid_period_rejected(self):
        with pytest.raises(ValueError):
            ema([1, 2, 3], period=0)


class TestATR:
    def test_insufficient_data_is_none(self):
        candles = [_candle(0, 105, 95, 100), _candle(1, 106, 96, 101)]
        result = atr(candles, period=5)
        assert result == [None, None]

    def test_hand_computed_sequence(self):
        """3 candles, period=2:
        TR[0] = high-low = 105-95 = 10 (no previous close)
        TR[1]: high=110,low=100,prev_close=100 -> max(10, |110-100|=10, |100-100|=0) = 10
        TR[2]: high=115,low=108,prev_close=105 -> max(7, |115-105|=10, |108-105|=3) = 10
        seed (index1) = (TR[0]+TR[1])/2 = (10+10)/2 = 10.0
        index2 (Wilder): (10.0*(2-1) + 10)/2 = (10+10)/2 = 10.0
        """
        candles = [
            _candle(0, 105, 95, 100),
            _candle(1, 110, 100, 105),
            _candle(2, 115, 108, 112),
        ]
        result = atr(candles, period=2)
        assert result[0] is None
        assert result[1] == pytest.approx(10.0)
        assert result[2] == pytest.approx(10.0)

    def test_wilder_smoothing_differs_from_simple_average_after_seed(self):
        """A TR spike after the seed period should pull ATR toward it
        gradually (Wilder smoothing), not average it in equally with
        older values the way a simple moving average would."""
        candles = [
            _candle(0, 101, 99, 100),   # TR=2
            _candle(1, 102, 100, 101),  # TR=2
            _candle(2, 103, 101, 102),  # TR=2
            _candle(3, 150, 100, 125),  # TR spike = 50
        ]
        result = atr(candles, period=3)
        seed = (2 + 2 + 2) / 3  # = 2.0 at index2
        assert result[2] == pytest.approx(seed)
        expected_index3 = (seed * 2 + 50) / 3  # Wilder smoothing formula
        assert result[3] == pytest.approx(expected_index3)
        assert result[3] < 50  # smoothed, not equal to the raw spike

    def test_invalid_period_rejected(self):
        with pytest.raises(ValueError):
            atr([_candle(0, 105, 95, 100)], period=0)

    def test_empty_candles_returns_empty(self):
        assert atr([], period=5) == []


class TestRSI:
    def test_insufficient_data_is_none(self):
        result = rsi([1, 2, 3], period=5)
        assert result == [None, None, None]

    def test_hand_computed_sequence(self):
        """values = [100, 102, 101, 103, 104, 103, 105], period=3
        changes:      +2,  -1,  +2,  +1,  -1,  +2
        gains:         2,   0,   2,   1,   0,   2
        losses:        0,   1,   0,   0,   1,   0

        avg_gain(seed, first 3 changes) = (2+0+2)/3 = 1.3333
        avg_loss(seed, first 3 changes) = (0+1+0)/3 = 0.3333
        RSI[index3] = 100 - 100/(1+ (1.3333/0.3333)) = 100 - 100/5 = 80.0

        index4 (Wilder update with change index4 = +1 -> gain=1, loss=0):
        avg_gain = (1.3333*2 + 1)/3 = 1.2222
        avg_loss = (0.3333*2 + 0)/3 = 0.2222
        RSI = 100 - 100/(1 + 1.2222/0.2222) = 100 - 100/6.5 = 84.615...
        """
        values = [100, 102, 101, 103, 104, 103, 105]
        result = rsi(values, period=3)
        assert result[0] is None
        assert result[1] is None
        assert result[2] is None
        assert result[3] == pytest.approx(80.0, abs=1e-2)
        assert result[4] == pytest.approx(84.615, abs=1e-2)

    def test_pure_uptrend_returns_100_not_division_error(self):
        values = [100, 101, 102, 103, 104, 105]
        result = rsi(values, period=3)
        assert result[3] == pytest.approx(100.0)

    def test_pure_downtrend_approaches_zero(self):
        values = [105, 104, 103, 102, 101, 100]
        result = rsi(values, period=3)
        assert result[3] == pytest.approx(0.0)

    def test_invalid_period_rejected(self):
        with pytest.raises(ValueError):
            rsi([1, 2, 3], period=0)
