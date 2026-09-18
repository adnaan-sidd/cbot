"""
Execution interface (architecture doc, execution module table).

Every concrete executor (paper, live cTrader) implements this same
interface, so the state machine never knows or cares which one it's
talking to. Hard rule from the brief: "handle broker/API failures
safely... never hide errors or silently retry dangerous orders" — this
is why every method here RAISES on failure rather than returning a
sentinel value. A caller that doesn't handle the exception will crash
loudly, which is the correct behavior for an unhandled trading error —
silently swallowing it and continuing is far more dangerous.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from core.enums import ExitReason
from core.models import Position, TradeRequest, Trade


class ExecutionError(Exception):
    """Base class for all execution failures. Never caught-and-ignored
    anywhere in this codebase — callers catch it to log and transition
    state (e.g. to ORDER_FAILED), never to retry automatically."""


class BrokerConnectionError(ExecutionError):
    """The broker/API connection is down or unreachable. Distinct from
    OrderRejectedError because a connection failure means we don't even
    know if an order attempt reached the broker — retrying blindly here
    risks a duplicate order, which is exactly what duplicate_order_guard
    exists to prevent at the signal level, not something execution
    should paper over with a retry."""


class OrderRejectedError(ExecutionError):
    """The broker was reached and explicitly rejected the order (e.g.
    invalid volume, market closed, insufficient margin at the broker's
    own check). The rejection reason from the broker is preserved in the
    exception message."""


class PositionNotFoundError(ExecutionError):
    """Attempted to close or modify a position that the executor has no
    record of — e.g. a reconciliation mismatch between the bot's local
    state and the broker's actual open positions."""


class ExecutionInterface(ABC):
    @abstractmethod
    def place_order(self, trade_request: TradeRequest) -> Position:
        """Submit an approved TradeRequest for execution. Returns the
        resulting Position on a confirmed fill. Raises ExecutionError
        (or a subclass) on any failure — never returns None or a
        partial/ambiguous result."""
        raise NotImplementedError

    @abstractmethod
    def close_position(self, position: Position, exit_reason: ExitReason, reference_price: float) -> Trade:
        """Close an open position. `reference_price` is the price the
        exit was triggered at (e.g. the stop-loss or take-profit level)
        before any execution slippage — the executor applies its own
        fill-price logic (simulated for paper, real market fill for
        live) on top of this."""
        raise NotImplementedError

    @abstractmethod
    def modify_sl_tp(
        self, position: Position, *, new_stop_loss: Optional[float] = None, new_take_profit: Optional[float] = None
    ) -> Position:
        """Modify an open position's stop-loss and/or take-profit.
        Passing None for either leaves that value unchanged."""
        raise NotImplementedError

    @abstractmethod
    def get_open_position(self, symbol: str) -> Optional[Position]:
        """Returns the currently tracked open position for `symbol`, or
        None. Used for reconciliation between the bot's local state and
        the broker's actual state."""
        raise NotImplementedError

    def is_connected(self) -> bool:
        """Default True (paper trading has no real connection to lose).
        Live executors override this with a real connectivity check —
        the kill switch's broker-disconnect trigger depends on it."""
        return True
