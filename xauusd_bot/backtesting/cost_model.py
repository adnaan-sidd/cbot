"""
Cost model (architecture doc §10 — "spread, commission and realistic
slippage assumptions").

Commission structure is broker-specific in a way that isn't just "a
different number" — XM (current broker, cTrader Raw Spread account)
prices commission per $1,000,000 of notional USD volume traded, NOT as a
flat per-lot fee (the previous broker's model). Notional volume depends
on the actual fill price, so commission must be calculated per-fill,
not from lot size alone.

Modeling simplifications, stated explicitly rather than left implicit:
  - Spread cost is applied ONLY at entry (the fill is always worse than
    the candle's close by the full spread, in the adverse direction for
    the trade). Exit fills at the exact SL/TP price with no additional
    spread charge. This concentrates the full round-trip spread cost at
    entry rather than splitting it — a common, slightly conservative
    simplification for candle-based (not tick-based) backtesting.
  - Slippage is applied at BOTH entry and exit, always adverse.
  - Commission is charged once per side (entry AND exit) — ASSUMED from
    XM's "per million USD volume" wording, matching the "in/out deals"
    convention confirmed for the previous broker. This assumption should
    be verified against an actual filled order's commission line before
    relying on it for live/paper trading.
  - Swap is calculated separately (see swap_model.py) and only applied
    if `BrokerConfig.swap_enabled` is True — it defaults to False since
    this account's swap-free/Islamic status was unconfirmed at setup.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.enums import Direction


@dataclass
class CostModel:
    commission_per_million_usd: float  # e.g. 30.0 -> $30 per $1M notional, one side

    def commission_for_notional(self, *, lot_size: float, fill_price: float, contract_size: float) -> float:
        """One-way commission in USD for a fill of `lot_size` lots at
        `fill_price`, given the instrument's contract size. Callers
        charge this once at entry (using the entry fill price) and once
        at exit (using the exit fill price) — notional differs between
        the two fills whenever price has moved, so this must be computed
        per-fill, not doubled from a single calculation."""
        if lot_size <= 0:
            raise ValueError("lot_size must be > 0")
        if fill_price <= 0:
            raise ValueError("fill_price must be > 0")
        if contract_size <= 0:
            raise ValueError("contract_size must be > 0")
        notional_usd = lot_size * contract_size * fill_price
        return (notional_usd / 1_000_000.0) * self.commission_per_million_usd


def apply_entry_spread(
    price: float, direction: Direction, spread_points: float, point_size: float
) -> float:
    """Entry always fills worse than the candle's close by the full
    spread, regardless of direction — see module docstring."""
    if spread_points < 0:
        raise ValueError("spread_points cannot be negative")
    spread_price = spread_points * point_size
    if direction == Direction.LONG:
        return price + spread_price
    return price - spread_price


def apply_slippage(
    price: float, direction: Direction, *, is_entry: bool, slippage_amount: float
) -> float:
    """Slippage is always adverse: a fill is never better than expected.
    - Entry LONG / Exit SHORT (both are effectively "buying"): price moves up.
    - Entry SHORT / Exit LONG (both are effectively "selling"): price moves down.
    """
    if slippage_amount < 0:
        raise ValueError("slippage_amount cannot be negative")
    buying = (direction == Direction.LONG and is_entry) or (
        direction == Direction.SHORT and not is_entry
    )
    return price + slippage_amount if buying else price - slippage_amount
