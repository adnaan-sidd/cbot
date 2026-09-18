"""Risk engine — cBot port of xauusd_bot/risk_engine/ (5 modules, 1 file).

Porting note: cTrader Python projects are flat (in-memory modules, no
packages), so the five risk_engine modules are combined into this single
module. Function/class names and behavior are UNCHANGED from the
original: normalization (cents->base currency), floor-to-step position
sizing (risk can only ever be reduced, never increased), pass/fail
margin gate, daily-loss / drawdown / consecutive-loss limits, and
risk_gate.evaluate_signal() — the ONLY function allowed to turn a
Signal into a TradeRequest, with account-level halts checked BEFORE any
sizing math.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Optional

from config_schema import BrokerConfig, KillSwitchConfig, RiskConfig
from core_enums import RejectReason
from core_models import AccountState, RejectedSignal, Signal, TradeRequest

# =====================================================================
# normalization (xauusd_bot/risk_engine/normalization.py)
# =====================================================================

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


# =====================================================================
# position_sizer (xauusd_bot/risk_engine/position_sizer.py)
# =====================================================================

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


# =====================================================================
# margin_calculator (xauusd_bot/risk_engine/margin_calculator.py)
# =====================================================================

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


# =====================================================================
# risk_limits (xauusd_bot/risk_engine/risk_limits.py)
# =====================================================================

def check_max_open_positions(account: AccountState, risk: RiskConfig) -> Optional[RejectReason]:
    if account.open_positions_count >= risk.max_open_positions:
        return RejectReason.POSITION_ALREADY_OPEN
    return None


def check_daily_loss_limit(account: AccountState, risk: RiskConfig) -> Optional[RejectReason]:
    if account.daily_loss_percent >= risk.daily_loss_limit_percent:
        return RejectReason.DAILY_LOSS_LIMIT_HIT
    return None


def check_max_drawdown(account: AccountState, risk: RiskConfig) -> Optional[RejectReason]:
    if account.drawdown_percent >= risk.max_drawdown_percent:
        return RejectReason.MAX_DRAWDOWN_HIT
    return None


@dataclass
class ConsecutiveLossTracker:
    """Tracks consecutive losses and enforces a cooldown window once the
    configured limit is hit. A winning trade resets the streak to zero —
    this is explicitly NOT martingale/averaging logic; it only ever
    controls whether new trades are ALLOWED, never position size."""

    max_consecutive_losses: int
    cooldown_hours: int
    consecutive_losses: int = 0
    cooldown_until: Optional[datetime] = field(default=None)

    def record_result(self, *, is_win: bool, at: Optional[datetime] = None) -> None:
        at = at or datetime.now(timezone.utc)
        if is_win:
            self.consecutive_losses = 0
            self.cooldown_until = None
            return

        self.consecutive_losses += 1
        if self.consecutive_losses >= self.max_consecutive_losses:
            self.cooldown_until = at + timedelta(hours=self.cooldown_hours)

    def is_in_cooldown(self, *, now: Optional[datetime] = None) -> bool:
        if self.cooldown_until is None:
            return False
        now = now or datetime.now(timezone.utc)
        return now < self.cooldown_until

    def reset(self) -> None:
        """Manual reset — e.g. operator override after reviewing losses.
        Never called automatically by the bot itself."""
        self.consecutive_losses = 0
        self.cooldown_until = None

    @classmethod
    def from_config(cls, risk: RiskConfig) -> "ConsecutiveLossTracker":
        return cls(
            max_consecutive_losses=risk.max_consecutive_losses,
            cooldown_hours=risk.consecutive_loss_cooldown_hours,
        )


def check_consecutive_loss_cooldown(
    tracker: ConsecutiveLossTracker, *, now: Optional[datetime] = None
) -> Optional[RejectReason]:
    if tracker.is_in_cooldown(now=now):
        return RejectReason.CONSECUTIVE_LOSS_COOLDOWN_ACTIVE
    return None


# =====================================================================
# risk_gate (xauusd_bot/risk_engine/risk_gate.py)
# =====================================================================

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

