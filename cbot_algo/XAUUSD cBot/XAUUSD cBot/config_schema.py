"""Config schema — cBot port of xauusd_bot/config/config_schema.py.

Porting note: the original schema is pydantic-based. The cTrader Python
runtime resolves third-party packages from requirements.txt, but a
Rust-compiled dependency (pydantic-core) is a needless build risk for
~40 lines of bounds checking. This port keeps the EXACT same class
names, field names, defaults, bounds and cross-field rules, and fails
loudly on violation (raises ConfigValidationError, a ValueError subclass)
— a bad config still cannot be constructed, just like the original.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional


class ConfigValidationError(ValueError):
    """Any config value out of bounds, or cross-field inconsistent.
    Boot treats this as fatal: the cBot prints the error and Stops —
    it never trades on a config it does not fully trust."""


def model_copy(obj, **updates):
    """pydantic-compatible helper: copy this config object with the given
    fields replaced (re-running validation, like the original schema)."""
    from dataclasses import fields, replace

    valid = {f.name for f in fields(obj)}
    unknown = set(updates) - valid
    if unknown:
        raise ConfigValidationError(f"unknown field(s) for model_copy: {sorted(unknown)}")
    return replace(obj, **updates)


def _check(name: str, value, *, ge=None, le=None, gt=None, lt=None) -> None:
    problems = []
    if value is None:
        problems.append(f"{name}: value is None")
        raise ConfigValidationError("; ".join(problems))
    try:
        ok = True
        if ge is not None and not value >= ge:
            ok = False
            problems.append(f"{name}={value!r} < required minimum {ge}")
        if le is not None and not value <= le:
            ok = False
            problems.append(f"{name}={value!r} > required maximum {le}")
        if gt is not None and not value > gt:
            ok = False
            problems.append(f"{name}={value!r} must be > {gt}")
        if lt is not None and not value < lt:
            ok = False
            problems.append(f"{name}={value!r} must be < {lt}")
    except TypeError as exc:  # non-comparable value types
        raise ConfigValidationError(f"{name}: invalid type {type(value)!r}") from exc
    if not ok:
        raise ConfigValidationError("; ".join(problems))


@dataclass
class RiskConfig:
    # Brief: default risk 0.25-0.5%, absolute max 1%.
    default_risk_percent: float = 0.0035
    max_risk_percent: float = 0.01
    # Phase-1 hard cap. Raising this later is a deliberate architecture
    # change, not a config tweak — hence le=1 with no higher option exposed.
    max_open_positions: int = 1
    daily_loss_limit_percent: float = 0.02
    max_drawdown_percent: float = 0.10
    max_consecutive_losses: int = 3
    consecutive_loss_cooldown_hours: int = 24
    # Cap on how much of free margin a single trade may use, independent
    # of and in addition to the risk-% based position sizing.
    margin_utilization_cap: float = 0.5
    # If a broker's minimum lot risks more than intended, reject by default
    # (tolerance=0). A nonzero tolerance must be an explicit, deliberate
    # config choice — never a silent default.
    min_lot_risk_tolerance: float = 0.0
    daily_loss_reset: Literal["utc_midnight"] = "utc_midnight"


    def model_copy(self, update=None):
        """pydantic-compatible: return a copy with `update` fields replaced."""
        return model_copy(self, **(update or {}))

    def __post_init__(self) -> None:
        _check("default_risk_percent", self.default_risk_percent, ge=0.0025, le=0.005)
        _check("max_risk_percent", self.max_risk_percent, le=0.01)
        _check("max_open_positions", self.max_open_positions, ge=1, le=1)
        _check("daily_loss_limit_percent", self.daily_loss_limit_percent, gt=0, le=0.05)
        _check("max_drawdown_percent", self.max_drawdown_percent, gt=0, le=0.25)
        _check("max_consecutive_losses", self.max_consecutive_losses, ge=1, le=10)
        _check("consecutive_loss_cooldown_hours", self.consecutive_loss_cooldown_hours, ge=1)
        _check("margin_utilization_cap", self.margin_utilization_cap, gt=0, le=1.0)
        _check("min_lot_risk_tolerance", self.min_lot_risk_tolerance, ge=0.0, le=0.002)
        if self.daily_loss_reset != "utc_midnight":
            raise ConfigValidationError(
                f"daily_loss_reset={self.daily_loss_reset!r} — only 'utc_midnight' is supported"
            )
        if self.default_risk_percent > self.max_risk_percent:
            raise ConfigValidationError(
                f"default_risk_percent ({self.default_risk_percent}) cannot exceed "
                f"max_risk_percent ({self.max_risk_percent})"
            )


@dataclass
class FilterConfig:
    max_spread_points: float
    duplicate_order_debounce_seconds: int = 5
    allowed_sessions: Optional[List[str]] = None


    def model_copy(self, update=None):
        """pydantic-compatible: return a copy with `update` fields replaced."""
        return model_copy(self, **(update or {}))

    def __post_init__(self) -> None:
        _check("max_spread_points", self.max_spread_points, gt=0)
        _check("duplicate_order_debounce_seconds", self.duplicate_order_debounce_seconds, ge=1)


@dataclass
class BrokerConfig:
    """XM, cTrader, Raw Spread standard account (not cents). Values below
    are taken from cTrader's Symbol Info panel for XAUUSD — see the
    original module docstring for the commission/swap caveats."""

    symbol: str = "XAUUSD"
    leverage: int = 100
    contract_size: float = 100.0   # 100 oz/lot (cTrader "Lot size")
    digits: int = 2                 # "Pip position": 2 -> point/pip = 0.01
    lot_step: float = 0.01
    min_lot: float = 0.01
    max_lot: float = 100.0
    margin_rate: float = 1.0
    commission_per_million_usd: float = 30.0
    account_currency_unit: Literal["cents", "usd"] = "usd"
    unit_scale_factor: float = 1.0
    swap_enabled: bool = False
    swap_long_points: float = -58.6
    swap_short_points: float = 40.9
    triple_swap_weekday: int = 2
    weekend_swap_disabled: bool = True


    def model_copy(self, update=None):
        """pydantic-compatible: return a copy with `update` fields replaced."""
        return model_copy(self, **(update or {}))

    def __post_init__(self) -> None:
        _check("leverage", self.leverage, ge=1, le=100)
        _check("contract_size", self.contract_size, gt=0)
        _check("digits", self.digits, ge=0, le=8)
        _check("lot_step", self.lot_step, gt=0)
        _check("min_lot", self.min_lot, gt=0)
        _check("max_lot", self.max_lot, gt=0)
        _check("margin_rate", self.margin_rate, gt=0)
        _check("commission_per_million_usd", self.commission_per_million_usd, ge=0)
        if self.account_currency_unit not in ("cents", "usd"):
            raise ConfigValidationError(
                f"account_currency_unit={self.account_currency_unit!r} — must be 'cents' or 'usd'"
            )
        _check("unit_scale_factor", self.unit_scale_factor, gt=0)
        _check("triple_swap_weekday", self.triple_swap_weekday, ge=0, le=6)
        if self.min_lot > self.max_lot:
            raise ConfigValidationError("min_lot cannot exceed max_lot")
        if self.account_currency_unit == "cents" and self.unit_scale_factor <= 1.0:
            raise ConfigValidationError(
                "account_currency_unit is 'cents' but unit_scale_factor "
                f"is {self.unit_scale_factor} — this looks misconfigured"
            )

    @property
    def point_size(self) -> float:
        return 10 ** (-self.digits)


@dataclass
class KillSwitchConfig:
    # Automatic only, per decision log — no manual CLI/Telegram trigger
    # in this version. Field exists so a future manual trigger is an
    # explicit config change, not a silent capability.
    manual_trigger_enabled: bool = False
    manual_rearm_required: bool = True
    trigger_on_broker_disconnect: bool = True
    trigger_on_max_drawdown: bool = True

    def model_copy(self, update=None):
        """pydantic-compatible: return a copy with `update` fields replaced."""
        return model_copy(self, **(update or {}))



@dataclass
class BotConfig:
    environment: Literal["backtest", "paper", "live"]
    risk: RiskConfig
    filters: FilterConfig
    broker: BrokerConfig
    kill_switch: KillSwitchConfig = field(default_factory=KillSwitchConfig)


    def model_copy(self, update=None):
        """pydantic-compatible: return a copy with `update` fields replaced."""
        return model_copy(self, **(update or {}))

    def __post_init__(self) -> None:
        if self.environment not in ("backtest", "paper", "live"):
            raise ConfigValidationError(
                f"environment={self.environment!r} — must be 'backtest', 'paper' or 'live'"
            )
        # Guard rail: catch someone flipping environment to "live" while
        # still pointed at an obviously-unconfigured spread filter.
        if self.environment == "live" and self.filters.max_spread_points <= 0:
            raise ConfigValidationError("live environment requires a real max_spread_points value")
