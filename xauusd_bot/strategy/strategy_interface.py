"""
Strategy interface (architecture doc, strategy module table).

A Strategy's ONLY job is to look at market history and optionally
produce a Signal. It has zero knowledge of account equity, risk %,
lot sizing, or margin — those live exclusively in risk_engine, reached
only through risk_gate.evaluate_signal(). There is no import path from
this module to risk_engine, which is a deliberate architectural
boundary, not an oversight.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from core.models import Candle, Signal


@dataclass
class MarketState:
    """Everything a strategy is allowed to see: the symbol and the full
    candle history observed so far (most recent candle last)."""

    symbol: str
    history: list[Candle]

    @property
    def current(self) -> Candle:
        if not self.history:
            raise ValueError("MarketState has no candle history yet")
        return self.history[-1]


class Strategy(ABC):
    name: str

    @abstractmethod
    def generate_signal(self, market_state: MarketState) -> Optional[Signal]:
        raise NotImplementedError
