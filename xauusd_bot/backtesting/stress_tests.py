"""
Stress testing (architecture doc §10 — "stress testing... where practical").

Rather than reimplementing the engine, these are thin WRAPPER
implementations of the existing MarketDataFeed and SlippageModel
interfaces that exaggerate one cost variable at a time. Point a
BacktestEngine at a wrapped feed/slippage model instead of the plain
ones, run it, and compare the resulting PerformanceReport against the
baseline run — same engine, same strategy, same risk_gate, only the
simulated market friction changes.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterator, Optional

from backtesting.slippage_model import SlippageModel
from core.models import Candle
from market_data.feed_interface import MarketDataFeed


class SpreadMultipliedFeed(MarketDataFeed):
    """Wraps a base feed, multiplying every candle's spread by a fixed
    factor — simulates a spread-blowout scenario (e.g. around major
    news) without needing separate stressed historical data."""

    def __init__(self, base_feed: MarketDataFeed, multiplier: float):
        if multiplier < 1.0:
            raise ValueError("a stress multiplier below 1.0 isn't a stress scenario")
        self.base_feed = base_feed
        self.multiplier = multiplier

    def candles(self) -> Iterator[Candle]:
        for c in self.base_feed.candles():
            stressed_spread = (
                c.spread_points * self.multiplier if c.spread_points is not None else None
            )
            yield Candle(
                timestamp=c.timestamp,
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
                spread_points=stressed_spread,
            )


class ScaledSlippageModel(SlippageModel):
    """Wraps a base slippage model, multiplying its output — simulates a
    slippage-shock scenario (thin liquidity, fast markets)."""

    def __init__(self, base_model: SlippageModel, multiplier: float):
        if multiplier < 1.0:
            raise ValueError("a stress multiplier below 1.0 isn't a stress scenario")
        self.base_model = base_model
        self.multiplier = multiplier

    def get_slippage_price(self, candle: Candle) -> float:
        return self.base_model.get_slippage_price(candle) * self.multiplier


class GapRiskSlippageModel(SlippageModel):
    """Wraps a base slippage model, occasionally injecting a large extra
    slippage amount — simulates a stop-loss gapping through its price on
    a news spike rather than filling near the requested level. This is
    intentionally probabilistic and seeded for reproducible tests."""

    def __init__(
        self,
        base_model: SlippageModel,
        *,
        gap_probability: float,
        gap_size: float,
        seed: Optional[int] = None,
    ):
        if not (0.0 <= gap_probability <= 1.0):
            raise ValueError("gap_probability must be between 0 and 1")
        if gap_size < 0:
            raise ValueError("gap_size cannot be negative")
        self.base_model = base_model
        self.gap_probability = gap_probability
        self.gap_size = gap_size
        self._rng = random.Random(seed)

    def get_slippage_price(self, candle: Candle) -> float:
        base = self.base_model.get_slippage_price(candle)
        if self._rng.random() < self.gap_probability:
            return base + self.gap_size
        return base


@dataclass
class StressScenario:
    name: str
    spread_multiplier: float = 1.0
    slippage_multiplier: float = 1.0
    gap_probability: float = 0.0
    gap_size: float = 0.0


SPREAD_BLOWOUT = StressScenario(name="spread_blowout", spread_multiplier=5.0)
SLIPPAGE_SHOCK = StressScenario(name="slippage_shock", slippage_multiplier=10.0)
GAP_THROUGH_STOP = StressScenario(
    name="gap_through_stop", gap_probability=0.05, gap_size=15.0
)
COMBINED_STRESS = StressScenario(
    name="combined_stress",
    spread_multiplier=3.0,
    slippage_multiplier=5.0,
    gap_probability=0.02,
    gap_size=10.0,
)


def apply_stress_scenario(
    base_feed: MarketDataFeed,
    base_slippage: SlippageModel,
    scenario: StressScenario,
    *,
    seed: Optional[int] = None,
) -> tuple[MarketDataFeed, SlippageModel]:
    """Build the (feed, slippage_model) pair for a given scenario. Pass
    these straight into a BacktestEngine in place of the baseline ones."""
    feed = base_feed
    if scenario.spread_multiplier > 1.0:
        feed = SpreadMultipliedFeed(base_feed, scenario.spread_multiplier)

    slippage: SlippageModel = base_slippage
    if scenario.slippage_multiplier > 1.0:
        slippage = ScaledSlippageModel(slippage, scenario.slippage_multiplier)
    if scenario.gap_probability > 0.0:
        slippage = GapRiskSlippageModel(
            slippage,
            gap_probability=scenario.gap_probability,
            gap_size=scenario.gap_size,
            seed=seed,
        )

    return feed, slippage
