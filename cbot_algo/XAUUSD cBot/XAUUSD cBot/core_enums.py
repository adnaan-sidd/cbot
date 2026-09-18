"""
Enumerations shared across the bot. Kept in one place so every module
refers to the same set of values — no magic strings anywhere else.
"""

from enum import Enum


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


class SystemState(str, Enum):
    """States for the top-level bot state machine (see architecture doc §6)."""

    BOOT = "boot"
    IDLE = "idle"
    SCANNING = "scanning"
    RISK_CHECK = "risk_check"
    ORDER_PENDING = "order_pending"
    ORDER_FAILED = "order_failed"
    POSITION_OPEN = "position_open"
    MONITORING = "monitoring"
    EMERGENCY_HALT = "emergency_halt"
    KILL_SWITCH_ACTIVE = "kill_switch_active"


class RejectReason(str, Enum):
    """Every reason a signal can be rejected. Explicit and exhaustive —
    a rejection without a reason from this list is a bug, not a shortcut."""

    MIN_LOT_EXCEEDS_RISK = "min_lot_exceeds_risk"
    SPREAD_TOO_WIDE = "spread_too_wide"
    DAILY_LOSS_LIMIT_HIT = "daily_loss_limit_hit"
    MAX_DRAWDOWN_HIT = "max_drawdown_hit"
    POSITION_ALREADY_OPEN = "position_already_open"
    DUPLICATE_ORDER = "duplicate_order"
    CONSECUTIVE_LOSS_LIMIT = "consecutive_loss_limit"
    MARGIN_INSUFFICIENT = "margin_insufficient"
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    INVALID_SIGNAL = "invalid_signal"          # e.g. missing/zero stop-loss
    SESSION_CLOSED = "session_closed"
    MISSING_STOP_LOSS = "missing_stop_loss"
    RISK_PERCENT_EXCEEDS_MAX = "risk_percent_exceeds_max"
    CONSECUTIVE_LOSS_COOLDOWN_ACTIVE = "consecutive_loss_cooldown_active"


class ExitReason(str, Enum):
    STOP_LOSS = "sl"
    TAKE_PROFIT = "tp"
    MANUAL = "manual"
    KILL_SWITCH = "kill_switch"
    TRAILING_STOP = "trailing_stop"


class EventType(str, Enum):
    """Every event written to the events log/table. See persistence/logger.py."""

    SIGNAL_GENERATED = "signal_generated"
    SIGNAL_REJECTED = "signal_rejected"
    ORDER_PLACED = "order_placed"
    ORDER_FILLED = "order_filled"
    ORDER_FAILED = "order_failed"
    POSITION_CLOSED = "position_closed"
    SL_MODIFIED = "sl_modified"
    TP_MODIFIED = "tp_modified"
    DAILY_LOSS_LIMIT_HIT = "daily_loss_limit_hit"
    MAX_DRAWDOWN_HIT = "max_drawdown_hit"
    CONSECUTIVE_LOSS_LIMIT_HIT = "consecutive_loss_limit_hit"
    KILL_SWITCH_TRIGGERED = "kill_switch_triggered"
    STATE_TRANSITION = "state_transition"
    ERROR = "error"
    BOOT = "boot"
