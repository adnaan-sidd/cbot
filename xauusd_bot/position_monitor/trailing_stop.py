"""
Trailing stop (added while testing whether a "let winners run" exit
beats a fixed reward:risk target — see trend_pullback_breakout_strategy.py's
`use_trailing_stop` option). Kept in position_monitor/, alongside
exit_logic.py, so backtest and live/paper trading use the exact same
trailing calculation — the architecture's core rule applied here too.

Only activates for a position whose `take_profit` is None — a strategy
opts into trailing by omitting a fixed target, rather than this module
overriding a strategy's explicit take-profit choice.

Hard rule: the stop only ever moves in the FAVORABLE direction (up for
a long, down for a short), never loosens. This is enforced structurally
(`update_trailing_stop` compares against the current stop and returns
None if the candidate wouldn't tighten it), not left to caller discipline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.enums import Direction
from core.models import Candle, Position


@dataclass
class TrailingStopState:
    """Tracks the most favorable price extreme seen since entry — the
    reference point the trailing stop is computed from. One instance
    per open position; discarded when the position closes."""

    favorable_extreme: float

    @classmethod
    def initial(cls, position: Position) -> "TrailingStopState":
        return cls(favorable_extreme=position.entry_price)

    def update_extreme(self, position: Position, candle: Candle) -> None:
        if position.direction == Direction.LONG:
            self.favorable_extreme = max(self.favorable_extreme, candle.high)
        else:
            self.favorable_extreme = min(self.favorable_extreme, candle.low)


def compute_trailing_stop_candidate(
    position: Position, state: TrailingStopState, current_atr: float, atr_multiplier: float
) -> float:
    if current_atr <= 0:
        raise ValueError("current_atr must be > 0")
    if atr_multiplier <= 0:
        raise ValueError("atr_multiplier must be > 0")
    distance = current_atr * atr_multiplier
    if position.direction == Direction.LONG:
        return state.favorable_extreme - distance
    return state.favorable_extreme + distance


def maybe_update_trailing_stop(
    position: Position, state: TrailingStopState, candle: Candle, current_atr: float, atr_multiplier: float
) -> Optional[float]:
    """Returns a new stop-loss level if the trail should move (i.e. the
    candidate is more favorable than the current stop), else None. Does
    NOT mutate `position` — the caller decides how/whether to apply it,
    matching the read-only style used elsewhere in this codebase (e.g.
    DuplicateOrderGuard.check vs. .record)."""
    state.update_extreme(position, candle)
    candidate = compute_trailing_stop_candidate(position, state, current_atr, atr_multiplier)

    if position.direction == Direction.LONG:
        return candidate if candidate > position.stop_loss else None
    return candidate if candidate < position.stop_loss else None
