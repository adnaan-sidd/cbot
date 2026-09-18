from datetime import datetime

import pytest

from backtesting.slippage_model import FixedSlippageModel, VolatilitySlippageModel
from core.models import Candle


def _candle(high: float, low: float) -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 1), open=(high + low) / 2, high=high, low=low,
        close=(high + low) / 2, volume=10,
    )


class TestFixedSlippageModel:
    def test_returns_constant_regardless_of_candle(self):
        model = FixedSlippageModel(price_amount=0.3)
        assert model.get_slippage_price(_candle(3610, 3590)) == pytest.approx(0.3)
        assert model.get_slippage_price(_candle(3601, 3599)) == pytest.approx(0.3)

    def test_negative_amount_rejected(self):
        with pytest.raises(ValueError):
            FixedSlippageModel(price_amount=-1)


class TestVolatilitySlippageModel:
    def test_wider_range_produces_more_slippage(self):
        model = VolatilitySlippageModel(range_fraction=0.1)
        narrow = model.get_slippage_price(_candle(3601, 3599))  # range=2
        wide = model.get_slippage_price(_candle(3650, 3550))     # range=100
        assert wide > narrow
        assert narrow == pytest.approx(0.2)
        assert wide == pytest.approx(10.0)

    def test_zero_range_produces_zero_slippage(self):
        model = VolatilitySlippageModel(range_fraction=0.1)
        assert model.get_slippage_price(_candle(3600, 3600)) == pytest.approx(0.0)

    def test_negative_fraction_rejected(self):
        with pytest.raises(ValueError):
            VolatilitySlippageModel(range_fraction=-0.1)
