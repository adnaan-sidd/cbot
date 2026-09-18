"""
Account-currency normalization (architecture doc §5.0).

Some brokers report balance/equity/margin scaled relative to real USD
(a "cents account", where MT5/cTrader shows a number ~100x the real
deposit). The bot's CURRENT broker (XM, cTrader, Raw Spread standard
account) does NOT use this — account_currency_unit="usd",
unit_scale_factor=1.0, so to_base_currency() is a no-op for it. This
module is kept fully general (not deleted or specialized away) because
a broker/account switch is exactly the kind of change that has already
happened once in this project, and should require a config change here,
never a code change.

EVERY value that reaches the risk engine's formulas (position sizing,
margin, daily loss %, drawdown %) must already be in one consistent base
unit — here, real USD. This module is the ONLY place that divides by
unit_scale_factor. No other module should ever touch a raw broker number
directly; mixing a raw cents value into a formula expecting USD is exactly
the kind of silent unit bug that produces a wildly wrong lot size.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from config.config_schema import BrokerConfig
from core.models import AccountState


def to_base_currency(raw_value: float, broker: BrokerConfig) -> float:
    """Convert a single raw broker-reported monetary value into the bot's
    internal base currency (USD)."""
    if broker.account_currency_unit == "cents":
        return raw_value / broker.unit_scale_factor
    return raw_value


def commission_to_base_currency(commission_usc_per_lot: float) -> float:
    """Convert a flat per-lot commission quoted in USC (US cents) into
    USD. NOT used by the current broker (XM prices commission per $1M of
    notional volume, not per lot — see backtesting/cost_model.py's
    notional-based calculation instead). Kept here as a general utility
    in case a future broker reintroduces a flat per-lot-in-USC structure;
    USC-to-USD is a fixed real-world conversion (divide by 100), never
    dependent on any particular broker's unit_scale_factor, which is why
    this function takes no BrokerConfig."""
    return commission_usc_per_lot / 100.0


@dataclass
class RawAccountSnapshot:
    """Values exactly as reported by the broker/feed, before normalization."""

    equity: float
    balance: float
    free_margin: float
    used_margin: float
    open_positions_count: int
    daily_start_equity: float
    peak_equity: float
    consecutive_losses: int
    trading_day: date


def normalize_account_state(raw: RawAccountSnapshot, broker: BrokerConfig) -> AccountState:
    """Build a fully-normalized AccountState from raw broker values.
    This is the single conversion point — see module docstring."""
    return AccountState(
        equity=to_base_currency(raw.equity, broker),
        balance=to_base_currency(raw.balance, broker),
        free_margin=to_base_currency(raw.free_margin, broker),
        used_margin=to_base_currency(raw.used_margin, broker),
        open_positions_count=raw.open_positions_count,
        daily_start_equity=to_base_currency(raw.daily_start_equity, broker),
        peak_equity=to_base_currency(raw.peak_equity, broker),
        consecutive_losses=raw.consecutive_losses,
        trading_day=raw.trading_day,
    )
