"""
FOR ENGINE VALIDATION ONLY — this is not, and is not meant to become, a
real trading strategy. Its only purpose is to produce fully deterministic,
hand-predictable signals so backtest_engine.py's mechanics (fills, cost
model, SL/TP handling, performance reporting) can be tested against
numbers computed by hand, independent of any real strategy's quality.

Real strategy development is Phase 4 — a separate, scratch-built module
that will plug into the same Strategy interface without any change to
the engine, risk_gate, or filter_chain built in Phases 1-3.
"""

from __future__ import annotations

from typing import Optional

from core.enums import Direction
from core.models import Signal
from strategy.strategy_interface import MarketState, Strategy


class AlternatingIntervalStrategy(Strategy):
    """Emits a signal every `interval` candles, alternating long/short,
    with a fixed stop-loss and take-profit distance from the candle's
    close. Fully deterministic given a candle sequence — no indicators,
    no randomness, no lookback beyond the current candle."""

    def __init__(
        self,
        *,
        interval: int = 10,
        stop_distance: float = 10.0,
        take_profit_distance: Optional[float] = 20.0,
        name: str = "placeholder_alternating_TEST_ONLY",
    ):
        if interval <= 0:
            raise ValueError("interval must be > 0")
        if stop_distance <= 0:
            raise ValueError("stop_distance must be > 0")
        self.interval = interval
        self.stop_distance = stop_distance
        self.take_profit_distance = take_profit_distance
        self.name = name
        self._candle_count = 0

    def generate_signal(self, market_state: MarketState) -> Optional[Signal]:
        self._candle_count += 1
        if self._candle_count % self.interval != 0:
            return None

        candle = market_state.current
        signal_number = self._candle_count // self.interval
        direction = Direction.LONG if signal_number % 2 == 1 else Direction.SHORT
        entry_price = candle.close

        if direction == Direction.LONG:
            stop_loss = entry_price - self.stop_distance
            take_profit = (
                entry_price + self.take_profit_distance
                if self.take_profit_distance is not None
                else None
            )
        else:
            stop_loss = entry_price + self.stop_distance
            take_profit = (
                entry_price - self.take_profit_distance
                if self.take_profit_distance is not None
                else None
            )

        return Signal(
            symbol=market_state.symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strategy_name=self.name,
        )
