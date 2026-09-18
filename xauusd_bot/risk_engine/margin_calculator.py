"""
Margin calculation (architecture doc §5.2).

Hard rule this module exists to enforce: margin availability NEVER
increases lot size. Sizing is risk-% driven only (position_sizer.py);
this module is purely a pass/fail gate applied AFTER a lot size has
already been determined.
"""

from __future__ import annotations

from dataclasses import dataclass

from config.config_schema import BrokerConfig, RiskConfig


def calculate_required_margin(*, lot_size: float, entry_price: float, broker: BrokerConfig) -> float:
    """
        required_margin = (lot_size * contract_size * entry_price * margin_rate) / leverage

    Result is in the bot's base currency (USD), matching entry_price's unit —
    this function does not know about cents accounts; inputs must already
    be normalized (see normalization.py) before reaching here.
    """
    if lot_size <= 0:
        raise ValueError("lot_size must be > 0")
    if entry_price <= 0:
        raise ValueError("entry_price must be > 0")

    notional = lot_size * broker.contract_size * entry_price * broker.margin_rate
    return notional / broker.leverage


@dataclass
class MarginCheckResult:
    sufficient: bool
    required_margin: float
    available_margin_for_trade: float  # free_margin * margin_utilization_cap
    detail: str = ""


def check_margin_sufficient(
    *, required_margin: float, free_margin: float, risk: RiskConfig
) -> MarginCheckResult:
    """Required margin must fit within `margin_utilization_cap` of free
    margin — a deliberate buffer below 100% of free margin, so a single
    trade can never consume all available margin even if the broker would
    technically allow it."""
    available = free_margin * risk.margin_utilization_cap
    sufficient = required_margin <= available
    detail = (
        f"required={required_margin:.2f}, available={available:.2f} "
        f"(free_margin={free_margin:.2f} x cap={risk.margin_utilization_cap})"
    )
    return MarginCheckResult(
        sufficient=sufficient,
        required_margin=required_margin,
        available_margin_for_trade=available,
        detail=detail,
    )
