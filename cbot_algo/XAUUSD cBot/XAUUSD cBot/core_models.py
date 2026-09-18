"""
Core data models shared across the bot (architecture doc §4).

These are plain dataclasses, not pydantic models: they represent internal
runtime state passed between modules on a hot path, not external input that
needs parsing/validation (that's what config/config_schema.py is for).
Validation that IS needed here (e.g. "stop_loss is mandatory") is enforced
in __post_init__, so an invalid object simply cannot be constructed —
there is no "valid=False" state floating around the system.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

from core_enums import Direction, ExitReason, RejectReason


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    spread_points: Optional[float] = None

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise ValueError(f"Candle high ({self.high}) < low ({self.low})")
        if not (self.low <= self.open <= self.high):
            raise ValueError(
                f"Candle open ({self.open}) outside [low={self.low}, high={self.high}]"
            )
        if not (self.low <= self.close <= self.high):
            raise ValueError(
                f"Candle close ({self.close}) outside [low={self.low}, high={self.high}]"
            )


@dataclass
class Signal:
    """A strategy's proposed trade idea. Stop-loss is mandatory — a Signal
    without one cannot be constructed, per the "mandatory stop-loss" safety
    requirement. This is enforced at the type level, not by convention."""

    symbol: str
    direction: Direction
    entry_price: float
    stop_loss: float
    strategy_name: str
    take_profit: Optional[float] = None
    confidence: Optional[float] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=_new_id)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.stop_loss is None or self.stop_loss <= 0:
            raise ValueError("Signal.stop_loss is mandatory and must be > 0")
        if self.entry_price <= 0:
            raise ValueError("Signal.entry_price must be > 0")
        if self.direction == Direction.LONG and self.stop_loss >= self.entry_price:
            raise ValueError(
                "Long signal stop_loss must be below entry_price "
                f"(entry={self.entry_price}, sl={self.stop_loss})"
            )
        if self.direction == Direction.SHORT and self.stop_loss <= self.entry_price:
            raise ValueError(
                "Short signal stop_loss must be above entry_price "
                f"(entry={self.entry_price}, sl={self.stop_loss})"
            )
        if self.take_profit is not None:
            if self.direction == Direction.LONG and self.take_profit <= self.entry_price:
                raise ValueError("Long signal take_profit must be above entry_price")
            if self.direction == Direction.SHORT and self.take_profit >= self.entry_price:
                raise ValueError("Short signal take_profit must be below entry_price")

    @property
    def stop_loss_distance(self) -> float:
        return abs(self.entry_price - self.stop_loss)


@dataclass
class TradeRequest:
    """Output of the risk engine for an APPROVED signal. Only this object,
    never a raw Signal, may reach the execution module."""

    signal: Signal
    lot_size: float
    risk_amount: float
    risk_percent: float
    required_margin: float
    approved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    id: str = field(default_factory=_new_id)

    def __post_init__(self) -> None:
        if self.lot_size <= 0:
            raise ValueError("TradeRequest.lot_size must be > 0")
        if self.risk_percent <= 0:
            raise ValueError("TradeRequest.risk_percent must be > 0")


@dataclass
class RejectedSignal:
    signal: Signal
    reason: RejectReason
    detail: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    id: str = field(default_factory=_new_id)


@dataclass
class Position:
    symbol: str
    direction: Direction
    lot_size: float
    entry_price: float
    stop_loss: float
    take_profit: Optional[float]
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    broker_ticket: Optional[str] = None
    id: str = field(default_factory=_new_id)

    def __post_init__(self) -> None:
        if self.stop_loss is None or self.stop_loss <= 0:
            raise ValueError("Position.stop_loss is mandatory and must be > 0")


@dataclass
class Trade:
    """A closed position — the unit of analysis for backtest reporting
    and live trade history."""

    symbol: str
    direction: Direction
    lot_size: float
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: Optional[float]
    opened_at: datetime
    closed_at: datetime
    pnl: float
    pnl_percent: float
    exit_reason: ExitReason
    commission: float
    slippage_points: float
    id: str = field(default_factory=_new_id)

    def __post_init__(self) -> None:
        if self.closed_at < self.opened_at:
            raise ValueError("Trade.closed_at cannot be before opened_at")

    @property
    def is_win(self) -> bool:
        return self.pnl > 0


@dataclass
class AccountState:
    """Normalized account snapshot. IMPORTANT: by the time this object is
    constructed, all monetary values must already be in the bot's internal
    base unit (see risk_engine.normalize_to_base_currency, architecture §5.0)
    — never mix raw broker units (e.g. USC/cents) with base-unit values here."""

    equity: float
    balance: float
    free_margin: float
    used_margin: float
    open_positions_count: int
    daily_start_equity: float
    peak_equity: float
    consecutive_losses: int
    trading_day: date

    def __post_init__(self) -> None:
        if self.equity < 0 or self.balance < 0:
            raise ValueError("AccountState.equity/balance cannot be negative")
        if self.open_positions_count < 0:
            raise ValueError("AccountState.open_positions_count cannot be negative")
        if self.consecutive_losses < 0:
            raise ValueError("AccountState.consecutive_losses cannot be negative")

    @property
    def drawdown_percent(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)

    @property
    def daily_loss_percent(self) -> float:
        if self.daily_start_equity <= 0:
            return 0.0
        return max(0.0, (self.daily_start_equity - self.equity) / self.daily_start_equity)


# ---------------------------------------------------------------------
# cTrader-bridge time helpers (added in the cBot port)
#
# cTrader exposes .NET System.DateTime objects (bar times, server time,
# position entry times). Everything in this bot works in Python
# timezone-aware datetimes (UTC). These two helpers are the ONLY place
# that converts between the two; cTrader server/bar time is treated as
# UTC-based, matching the original xauusd_bot feed convention.
# ---------------------------------------------------------------------

def net_dt_to_utc(dt) -> "datetime":
    """Convert a cTrader (.NET) DateTime — or an already-converted Python
    datetime — to a timezone-aware UTC datetime."""
    to_unix = getattr(dt, "ToUnixTimeSeconds", None)
    if callable(to_unix):
        try:
            return datetime.fromtimestamp(int(to_unix()), tz=timezone.utc)
        except Exception:
            pass
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    raise TypeError(f"cannot convert {type(dt)!r} to a UTC datetime")


def now_utc() -> "datetime":
    return datetime.now(timezone.utc)
