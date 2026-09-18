"""
Duplicate order guard (architecture doc §5.7 / trade_filters module table).

Prevents the bot from submitting the same trade idea twice in quick
succession — e.g. a strategy re-firing on the next tick before the first
order has confirmed, or a reconnect causing a signal to be reprocessed.

This is deliberately separate from `risk_limits.check_max_open_positions`:
that check looks at the ACCOUNT's current open-position count; this one
looks at what THIS bot instance has recently attempted to submit,
independent of whether any of those attempts succeeded. Both are needed —
a duplicate could be caught here before a position even opens.

State is explicit and instance-held (not a module-level global), so it's
trivially testable and there is exactly one guard instance per running
bot, injected wherever it's needed — the same shape as ConsecutiveLossTracker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from core.enums import Direction, RejectReason
from core.models import Signal


@dataclass
class _SubmittedRecord:
    symbol: str
    direction: Direction
    entry_price: float
    submitted_at: datetime


@dataclass
class DuplicateOrderGuard:
    debounce_seconds: int
    price_tolerance: float = 0.0  # in price units (e.g. USD for XAUUSD); 0 = exact match required
    _recent: list[_SubmittedRecord] = field(default_factory=list)

    def _prune(self, *, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.debounce_seconds)
        self._recent = [r for r in self._recent if r.submitted_at >= cutoff]

    def check(self, signal: Signal, *, now: datetime) -> Optional[RejectReason]:
        """Read-only check — does NOT record the signal. Call `record()`
        separately once the signal is actually submitted, so a signal
        that fails a later check (e.g. risk gate) doesn't block a
        legitimate retry of the same idea."""
        self._prune(now=now)
        for r in self._recent:
            if (
                r.symbol == signal.symbol
                and r.direction == signal.direction
                and abs(r.entry_price - signal.entry_price) <= self.price_tolerance
            ):
                return RejectReason.DUPLICATE_ORDER
        return None

    def record(self, signal: Signal, *, now: datetime) -> None:
        self._prune(now=now)
        self._recent.append(
            _SubmittedRecord(
                symbol=signal.symbol,
                direction=signal.direction,
                entry_price=signal.entry_price,
                submitted_at=now,
            )
        )
