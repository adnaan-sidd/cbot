"""
Slippage models (architecture doc §10 — "realistic slippage assumptions").

Returns slippage as a price amount (USD for XAUUSD), not points — the
backtest engine works directly in price units throughout, so this avoids
a point/price conversion at every call site.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.models import Candle


class SlippageModel(ABC):
    @abstractmethod
    def get_slippage_price(self, candle: Candle) -> float:
        """Return a non-negative price-unit slippage amount for a fill
        happening during this candle."""
        raise NotImplementedError


class FixedSlippageModel(SlippageModel):
    """Constant slippage regardless of market conditions — the simplest
    possible assumption, useful as a baseline before adding volatility
    sensitivity."""

    def __init__(self, price_amount: float):
        if price_amount < 0:
            raise ValueError("price_amount cannot be negative")
        self.price_amount = price_amount

    def get_slippage_price(self, candle: Candle) -> float:
        return self.price_amount


class VolatilitySlippageModel(SlippageModel):
    """Slippage scales with the candle's own high-low range, as a cheap
    proxy for realized volatility (a true ATR would need lookback across
    several candles — this is a deliberately simple stand-in for Phase 3).
    Wider candles (more volatile conditions) produce proportionally more
    slippage, which is generally more realistic than a fixed constant."""

    def __init__(self, range_fraction: float = 0.05):
        if range_fraction < 0:
            raise ValueError("range_fraction cannot be negative")
        self.range_fraction = range_fraction

    def get_slippage_price(self, candle: Candle) -> float:
        candle_range = candle.high - candle.low
        return candle_range * self.range_fraction
