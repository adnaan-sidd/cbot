"""
Config schema (architecture doc §8). Every risk-relevant number in the
system is validated HERE, at boot, using pydantic's field constraints.

Design rule: a bad config must fail to start, never silently clamp.
This is why bounds below use pydantic Field(ge=..., le=...) rather than
being "corrected" in application code — an invalid BotConfig object
cannot be constructed at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator


class RiskConfig(BaseModel):
    # Brief: default risk 0.25-0.5%, absolute max 1%.
    default_risk_percent: float = Field(0.0035, ge=0.0025, le=0.005)
    max_risk_percent: float = Field(0.01, le=0.01)

    # Phase-1 hard cap. Raising this later is a deliberate architecture
    # change, not a config tweak — hence le=1 with no higher option exposed.
    max_open_positions: int = Field(1, ge=1, le=1)

    daily_loss_limit_percent: float = Field(0.02, gt=0, le=0.05)
    max_drawdown_percent: float = Field(0.10, gt=0, le=0.25)
    max_consecutive_losses: int = Field(3, ge=1, le=10)
    consecutive_loss_cooldown_hours: int = Field(24, ge=1)

    # Cap on how much of free margin a single trade may use, independent
    # of and in addition to the risk-% based position sizing.
    margin_utilization_cap: float = Field(0.5, gt=0, le=1.0)

    # If a broker's minimum lot risks more than intended, reject by default
    # (tolerance=0). A nonzero tolerance must be an explicit, deliberate
    # config choice — never a silent default.
    min_lot_risk_tolerance: float = Field(0.0, ge=0.0, le=0.002)

    daily_loss_reset: Literal["utc_midnight"] = "utc_midnight"

    @model_validator(mode="after")
    def _default_must_not_exceed_max(self) -> "RiskConfig":
        if self.default_risk_percent > self.max_risk_percent:
            raise ValueError(
                f"default_risk_percent ({self.default_risk_percent}) cannot exceed "
                f"max_risk_percent ({self.max_risk_percent})"
            )
        return self


class FilterConfig(BaseModel):
    max_spread_points: float = Field(..., gt=0)
    duplicate_order_debounce_seconds: int = Field(5, ge=1)
    allowed_sessions: Optional[list[str]] = None


class BrokerConfig(BaseModel):
    """XM, cTrader, Raw Spread standard account (not cents). Values below
    are taken from cTrader's Symbol Info panel for XAUUSD.

    Two things are NOT hardcoded assumptions, unlike the previous broker:
      - Commission here is priced per $1M of notional USD volume traded,
        NOT per lot — a fundamentally different structure from a flat
        per-lot commission, so it needs its own formula (see
        backtesting/cost_model.py), not just a different number.
      - Swap is NOT assumed to be free. The account's swap-free/Islamic
        status was unconfirmed at setup time, so `swap_enabled` defaults
        to False (no swap cost is charged) while the real swap point
        values from the broker are still stored below, ready to switch
        on with a one-line config change once confirmed — rather than
        silently omitting swap the way the previous (confirmed-Islamic)
        broker's config did.
    """

    symbol: str = "XAUUSD"
    leverage: int = Field(100, ge=1, le=100)
    contract_size: float = Field(100.0, gt=0)   # 100 oz/lot (cTrader "Lot size")
    digits: int = Field(2, ge=0)                 # "Pip position": 2 -> point/pip = 0.01
    lot_step: float = Field(0.01, gt=0)          # not explicitly listed in Symbol Info; standard cTrader default — confirm if precision issues appear
    min_lot: float = Field(0.01, gt=0)
    max_lot: float = Field(100.0, gt=0)
    margin_rate: float = Field(1.0, gt=0)        # no explicit "margin rate" field shown for this symbol; 1.0 = standard notional/leverage formula

    # Commission: $30.00 USD per $1,000,000 of notional USD volume, charged
    # per side (assumed — cTrader's Symbol Info doesn't state round-trip
    # vs per-side explicitly; this matches the "in/out deals" convention
    # confirmed for the previous broker and is the more conservative
    # assumption if wrong in the other direction, but should be verified
    # against an actual filled order's commission line).
    commission_per_million_usd: float = Field(30.0, ge=0)

    account_currency_unit: Literal["cents", "usd"] = "usd"
    unit_scale_factor: float = Field(1.0, gt=0)

    # Swap — NOT assumed free. See class docstring.
    swap_enabled: bool = False
    swap_long_points: float = -58.6   # cTrader "Swap (long)", in the same points/pips as `digits`
    swap_short_points: float = 40.9   # cTrader "Swap (short)"
    triple_swap_weekday: int = Field(2, ge=0, le=6)  # cTrader "3-day swaps: Wednesday" -> 0=Mon..6=Sun, Wed=2
    weekend_swap_disabled: bool = True

    @model_validator(mode="after")
    def _min_lot_not_above_max(self) -> "BrokerConfig":
        if self.min_lot > self.max_lot:
            raise ValueError("min_lot cannot exceed max_lot")
        return self

    @model_validator(mode="after")
    def _cents_requires_scale_factor(self) -> "BrokerConfig":
        if self.account_currency_unit == "cents" and self.unit_scale_factor <= 1.0:
            raise ValueError(
                "account_currency_unit is 'cents' but unit_scale_factor "
                f"is {self.unit_scale_factor} — this looks misconfigured"
            )
        return self

    @property
    def point_size(self) -> float:
        return 10 ** (-self.digits)


class KillSwitchConfig(BaseModel):
    # Automatic only, per decision log — no manual CLI/Telegram trigger
    # in this version. Field exists so a future manual trigger is an
    # explicit config change, not a silent capability.
    manual_trigger_enabled: bool = False
    manual_rearm_required: bool = True
    trigger_on_broker_disconnect: bool = True
    trigger_on_max_drawdown: bool = True


class BotConfig(BaseModel):
    environment: Literal["backtest", "paper", "live"]
    risk: RiskConfig
    filters: FilterConfig
    broker: BrokerConfig
    kill_switch: KillSwitchConfig = KillSwitchConfig()

    model_config = {"extra": "forbid"}  # unknown keys in YAML fail loudly, not silently ignored

    @model_validator(mode="after")
    def _live_requires_no_placeholder_values(self) -> "BotConfig":
        # Guard rail: catch someone flipping environment to "live" while
        # still pointed at an obviously-unconfigured spread filter, etc.
        if self.environment == "live" and self.filters.max_spread_points <= 0:
            raise ValueError("live environment requires a real max_spread_points value")
        return self


def load_config(path: str | Path) -> BotConfig:
    """Load and validate a YAML config file. Raises on any invalid or
    unrecognized field — never returns a partially-valid config."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if raw is None:
        raise ValueError(f"Config file {path} is empty")
    return BotConfig(**raw)
