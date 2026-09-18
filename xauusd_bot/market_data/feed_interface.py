"""
Market data feed interface (architecture doc, market_data module table).

Deliberately minimal: a feed's only job is to yield Candles in
chronological order. It has no concept of strategy, risk, or lookback
windows — the backtest engine accumulates its own candle history as it
consumes the feed. This keeps the same interface usable for both
historical replay (this phase) and a live cTrader feed (Phase 5) without
either implementation needing to know about the other.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator

from core.models import Candle


class MarketDataFeed(ABC):
    @abstractmethod
    def candles(self) -> Iterator[Candle]:
        """Yield Candles in chronological order. Implementations may be
        consumed exactly once (a live feed can't be 'replayed') — callers
        that need multiple passes should construct a fresh feed instance."""
        raise NotImplementedError
