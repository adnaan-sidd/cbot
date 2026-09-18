"""
Position sizing (architecture doc §5.1).

This module's ONLY job is: given equity, a risk %, an entry price, and a
stop-loss price, produce a lot size that never risks more than intended —
or refuse to produce one at all. It has no knowledge of strategy, margin,
or account-level halts (daily loss/drawdown/consecutive-loss) — those are
separate, composed together in risk_gate.py.

All monetary inputs (equity) must already be normalized to the bot's base
currency (see normalization.py) — this module does not know about cents
accounts, USC, or unit_scale_factor.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Optional

from config.config_schema import BrokerConfig, RiskConfig
from core.enums import RejectReason


def floor_to_step(value: float, step: float) -> float:
    """Floor `value` to the nearest multiple of `step`, using Decimal to
    avoid binary-float rounding artifacts (e.g. 0.1 + 0.2 != 0.3 territory).
    Flooring — never rounding up — is deliberate: rounding up would silently
    increase risk beyond what was calculated, which this system never does."""
    if step <= 0:
        raise ValueError("step must be > 0")
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    floored_units = (d_value / d_step).to_integral_value(rounding=ROUND_DOWN)
    return float(floored_units * d_step)


@dataclass
class SizingResult:
    """Either a valid lot size with its actual (post-floor) risk figures,
    or a rejection with a specific reason. Exactly one of the two shapes
    is populated — callers must check `approved` before reading lot_size."""

    approved: bool
    lot_size: Optional[float] = None
    risk_amount: Optional[float] = None      # actual $ at risk, post-floor
    risk_percent_actual: Optional[float] = None
    reason: Optional[RejectReason] = None
    detail: str = ""


def calculate_lot_size(
    *,
    equity: float,
    risk_percent: float,
    entry_price: float,
    stop_loss_price: float,
    broker: BrokerConfig,
    risk: RiskConfig,
) -> SizingResult:
    """Core formula (architecture §5.1):

        risk_amount    = equity * risk_percent
        sl_distance    = |entry_price - stop_loss_price|
        raw_lot        = risk_amount / (sl_distance * contract_size)
        lot_size       = floor(raw_lot, lot_step)

    If the floored lot is below the broker's minimum, the trade is
    rejected UNLESS the minimum lot's risk is within `min_lot_risk_tolerance`
    of the intended risk_percent (default tolerance is 0 — strict reject).
    The lot size is never rounded up to reach the minimum at the cost of
    additional risk.
    """
    if equity <= 0:
        return SizingResult(
            approved=False,
            reason=RejectReason.INVALID_SIGNAL,
            detail=f"equity must be > 0, got {equity}",
        )

    if risk_percent <= 0:
        return SizingResult(
            approved=False,
            reason=RejectReason.INVALID_SIGNAL,
            detail=f"risk_percent must be > 0, got {risk_percent}",
        )

    if risk_percent > risk.max_risk_percent:
        return SizingResult(
            approved=False,
            reason=RejectReason.RISK_PERCENT_EXCEEDS_MAX,
            detail=(
                f"requested risk_percent {risk_percent} exceeds configured "
                f"max_risk_percent {risk.max_risk_percent}"
            ),
        )

    sl_distance = abs(entry_price - stop_loss_price)
    if sl_distance <= 0:
        return SizingResult(
            approved=False,
            reason=RejectReason.INVALID_SIGNAL,
            detail="stop_loss distance is zero — cannot size a position with no stop",
        )

    risk_amount = equity * risk_percent
    value_per_price_unit_per_lot = broker.contract_size  # $ per 1.0 price move per lot

    raw_lot = risk_amount / (sl_distance * value_per_price_unit_per_lot)
    lot_size = floor_to_step(raw_lot, broker.lot_step)

    if lot_size < broker.min_lot:
        min_lot_risk_amount = broker.min_lot * sl_distance * value_per_price_unit_per_lot
        min_lot_risk_percent = min_lot_risk_amount / equity

        if min_lot_risk_percent > risk_percent + risk.min_lot_risk_tolerance:
            return SizingResult(
                approved=False,
                reason=RejectReason.MIN_LOT_EXCEEDS_RISK,
                detail=(
                    f"broker min_lot ({broker.min_lot}) would risk "
                    f"{min_lot_risk_percent:.4%} of equity, exceeding intended "
                    f"risk_percent {risk_percent:.4%} "
                    f"(+tolerance {risk.min_lot_risk_tolerance:.4%})"
                ),
            )
        # Within tolerance — min_lot is acceptable even though it's
        # technically above the raw calculated size.
        lot_size = broker.min_lot

    # Defensive cap — should essentially never trigger given the formula
    # above, but a broker's max_lot is a hard ceiling regardless.
    lot_size = min(lot_size, broker.max_lot)

    actual_risk_amount = lot_size * sl_distance * value_per_price_unit_per_lot
    actual_risk_percent = actual_risk_amount / equity

    return SizingResult(
        approved=True,
        lot_size=lot_size,
        risk_amount=actual_risk_amount,
        risk_percent_actual=actual_risk_percent,
    )
