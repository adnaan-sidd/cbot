"""
Risk gate (architecture doc §5, module table). This is the ONLY function
in the system allowed to turn a Signal into a TradeRequest. Strategy code
never calls position_sizer or margin_calculator directly — it produces a
Signal and hands it here.

Check order matters: account-level halts (position count, daily loss,
drawdown, consecutive-loss cooldown) are evaluated BEFORE any sizing or
margin math, so a halted account never even computes a hypothetical lot
size. Every path returns either a TradeRequest or a RejectedSignal with a
specific, logged-later reason — there is no third "silently do nothing"
outcome.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from config.config_schema import BotConfig
from core.enums import RejectReason
from core.models import AccountState, RejectedSignal, Signal, TradeRequest
from risk_engine.margin_calculator import calculate_required_margin, check_margin_sufficient
from risk_engine.position_sizer import calculate_lot_size
from risk_engine.risk_limits import (
    ConsecutiveLossTracker,
    check_consecutive_loss_cooldown,
    check_daily_loss_limit,
    check_max_drawdown,
    check_max_open_positions,
)


def evaluate_signal(
    signal: Signal,
    account: AccountState,
    config: BotConfig,
    consecutive_loss_tracker: ConsecutiveLossTracker,
    *,
    requested_risk_percent: Optional[float] = None,
    now: Optional[datetime] = None,
) -> TradeRequest | RejectedSignal:
    now = now or datetime.now(timezone.utc)
    risk = config.risk
    broker = config.broker

    def reject(reason: RejectReason, detail: str) -> RejectedSignal:
        return RejectedSignal(signal=signal, reason=reason, detail=detail, timestamp=now)

    # 1. Account-level halts — checked first, before any sizing math.
    reason = check_max_open_positions(account, risk)
    if reason:
        return reject(
            reason,
            f"open_positions_count={account.open_positions_count} >= "
            f"max_open_positions={risk.max_open_positions}",
        )

    reason = check_daily_loss_limit(account, risk)
    if reason:
        return reject(
            reason,
            f"daily_loss_percent={account.daily_loss_percent:.4%} >= "
            f"limit={risk.daily_loss_limit_percent:.4%}",
        )

    reason = check_max_drawdown(account, risk)
    if reason:
        return reject(
            reason,
            f"drawdown_percent={account.drawdown_percent:.4%} >= "
            f"limit={risk.max_drawdown_percent:.4%}",
        )

    reason = check_consecutive_loss_cooldown(consecutive_loss_tracker, now=now)
    if reason:
        return reject(
            reason,
            f"in cooldown until {consecutive_loss_tracker.cooldown_until} "
            f"after {consecutive_loss_tracker.consecutive_losses} consecutive losses",
        )

    # 2. Position sizing.
    risk_percent = requested_risk_percent if requested_risk_percent is not None else risk.default_risk_percent
    sizing = calculate_lot_size(
        equity=account.equity,
        risk_percent=risk_percent,
        entry_price=signal.entry_price,
        stop_loss_price=signal.stop_loss,
        broker=broker,
        risk=risk,
    )
    if not sizing.approved:
        return reject(sizing.reason, sizing.detail)

    # 3. Margin check — AFTER sizing, never influences lot size (§5.2).
    required_margin = calculate_required_margin(
        lot_size=sizing.lot_size, entry_price=signal.entry_price, broker=broker
    )
    margin_check = check_margin_sufficient(
        required_margin=required_margin, free_margin=account.free_margin, risk=risk
    )
    if not margin_check.sufficient:
        return reject(RejectReason.MARGIN_INSUFFICIENT, margin_check.detail)

    return TradeRequest(
        signal=signal,
        lot_size=sizing.lot_size,
        risk_amount=sizing.risk_amount,
        risk_percent=sizing.risk_percent_actual,
        required_margin=required_margin,
        approved_at=now,
    )
