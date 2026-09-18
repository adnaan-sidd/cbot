"""Position monitoring — cBot port of xauusd_bot/position_monitor/
(4 modules, 1 file): mark-to-market account tracking, kill switch,
ATR trailing stop, and shared exit/SL-TP logic.

Unchanged behavior, including the hard rules: the trailing stop only
ever moves in the favorable direction; the kill switch stays tripped
until an operator explicitly restarts the cBot (rearm is never called
automatically); account equity is marked to market on every call so the
kill switch sees real-time floating P&L, not just realized balance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from config_schema import BrokerConfig, KillSwitchConfig, RiskConfig
from core_enums import Direction, ExitReason
from core_models import AccountState, Candle, Position, Trade
from risk_engine import calculate_required_margin

# =====================================================================
# exit_logic (xauusd_bot/position_monitor/exit_logic.py)
# =====================================================================

def check_stop_or_target(position: Position, candle: Candle) -> Optional[tuple[ExitReason, float]]:
    """Returns (exit_reason, exit_price_before_slippage) if this candle's
    range touches the position's stop-loss or take-profit, else None.

    Worst-case assumption when a single candle's range touches BOTH: the
    stop-loss is assumed hit first (not the take-profit) — same
    documented assumption as the Phase 3 backtest engine."""
    if position.direction == Direction.LONG:
        hit_sl = candle.low <= position.stop_loss
        hit_tp = position.take_profit is not None and candle.high >= position.take_profit
    else:
        hit_sl = candle.high >= position.stop_loss
        hit_tp = position.take_profit is not None and candle.low <= position.take_profit

    if hit_sl:
        return ExitReason.STOP_LOSS, position.stop_loss
    if hit_tp:
        return ExitReason.TAKE_PROFIT, position.take_profit
    return None


def compute_unrealized_pnl(position: Position, current_price: float, contract_size: float) -> float:
    """Mark-to-market P&L for an OPEN position at `current_price`, before
    commission (commission is only realized on close). This is what
    Phase 3's backtest engine explicitly did NOT compute (equity only
    updated on trade close there) — Phase 5 adds real intrabar/live
    floating P&L so the kill switch can react to a breach WHILE a
    position is open, not only block new ones."""
    price_diff = (
        current_price - position.entry_price
        if position.direction == Direction.LONG
        else position.entry_price - current_price
    )
    return price_diff * position.lot_size * contract_size


# =====================================================================
# account_tracker (xauusd_bot/position_monitor/account_tracker.py)
# =====================================================================

class AccountTracker:
    def __init__(self, starting_balance: float):
        if starting_balance <= 0:
            raise ValueError("starting_balance must be > 0")
        self.balance = starting_balance       # realized equity — no open position's floating P&L
        self.peak_equity = starting_balance
        self.daily_start_equity = starting_balance
        self.current_trading_day: Optional[date] = None

    def roll_daily_if_needed(self, timestamp: datetime) -> None:
        day = timestamp.date()
        if self.current_trading_day is None or day != self.current_trading_day:
            self.current_trading_day = day
            self.daily_start_equity = self.balance

    def record_realized_trade(self, trade: Trade) -> None:
        self.balance += trade.pnl
        self.peak_equity = max(self.peak_equity, self.balance)

    def get_live_account_state(
        self,
        *,
        open_position: Optional[Position],
        current_price: Optional[float],
        broker: BrokerConfig,
        consecutive_losses: int,
    ) -> AccountState:
        """Marks any open position to market using `current_price`. If
        there's an open position but no current_price is available yet
        (e.g. right at boot before the first tick arrives), floating
        P&L is treated as 0 — the position is valued at its own entry
        price, never assumed to already be winning or losing."""
        floating_pnl = 0.0
        used_margin = 0.0
        if open_position is not None:
            if current_price is not None:
                floating_pnl = compute_unrealized_pnl(open_position, current_price, broker.contract_size)
            used_margin = calculate_required_margin(
                lot_size=open_position.lot_size, entry_price=open_position.entry_price, broker=broker
            )

        equity = self.balance + floating_pnl
        self.peak_equity = max(self.peak_equity, equity)
        free_margin = equity - used_margin

        if self.current_trading_day is None:
            raise RuntimeError("roll_daily_if_needed() must be called before get_live_account_state()")

        return AccountState(
            equity=equity,
            balance=self.balance,
            free_margin=free_margin,
            used_margin=used_margin,
            open_positions_count=1 if open_position is not None else 0,
            daily_start_equity=self.daily_start_equity,
            peak_equity=self.peak_equity,
            consecutive_losses=consecutive_losses,
            trading_day=self.current_trading_day,
        )


# =====================================================================
# kill_switch (xauusd_bot/position_monitor/kill_switch.py)
# =====================================================================

@dataclass
class KillSwitch:
    risk: RiskConfig
    config: KillSwitchConfig
    armed: bool = field(default=True)
    triggered_reason: Optional[str] = field(default=None)

    def check(self, account: AccountState, *, broker_connected: bool) -> bool:
        """Returns True if trading should be halted RIGHT NOW — either
        because this call just tripped the switch, or because it was
        already tripped and remains so. Callers should treat any True
        return as "no new trades, and if a position is open, close it,"
        regardless of which case produced it."""
        if not self.armed:
            return True

        if self.config.trigger_on_max_drawdown and account.drawdown_percent >= self.risk.max_drawdown_percent:
            self._trigger(
                f"max drawdown breached: {account.drawdown_percent:.2%} >= "
                f"{self.risk.max_drawdown_percent:.2%} (equity={account.equity:.2f}, peak={account.peak_equity:.2f})"
            )
            return True

        if self.config.trigger_on_broker_disconnect and not broker_connected:
            self._trigger("broker connection lost")
            return True

        return False

    def _trigger(self, reason: str) -> None:
        self.armed = False
        self.triggered_reason = reason

    def rearm(self) -> None:
        """Manual operator action — never called automatically. Clears
        the triggered state so trading can resume. Does not undo
        whatever position-closing already happened as a result of the
        trigger; it only re-permits new trading going forward."""
        self.armed = True
        self.triggered_reason = None


# =====================================================================
# trailing_stop (xauusd_bot/position_monitor/trailing_stop.py)
# =====================================================================

@dataclass
class TrailingStopState:
    """Tracks the most favorable price extreme seen since entry — the
    reference point the trailing stop is computed from. One instance
    per open position; discarded when the position closes."""

    favorable_extreme: float

    @classmethod
    def initial(cls, position: Position) -> "TrailingStopState":
        return cls(favorable_extreme=position.entry_price)

    def update_extreme(self, position: Position, candle: Candle) -> None:
        if position.direction == Direction.LONG:
            self.favorable_extreme = max(self.favorable_extreme, candle.high)
        else:
            self.favorable_extreme = min(self.favorable_extreme, candle.low)


def compute_trailing_stop_candidate(
    position: Position, state: TrailingStopState, current_atr: float, atr_multiplier: float
) -> float:
    if current_atr <= 0:
        raise ValueError("current_atr must be > 0")
    if atr_multiplier <= 0:
        raise ValueError("atr_multiplier must be > 0")
    distance = current_atr * atr_multiplier
    if position.direction == Direction.LONG:
        return state.favorable_extreme - distance
    return state.favorable_extreme + distance


def maybe_update_trailing_stop(
    position: Position, state: TrailingStopState, candle: Candle, current_atr: float, atr_multiplier: float
) -> Optional[float]:
    """Returns a new stop-loss level if the trail should move (i.e. the
    candidate is more favorable than the current stop), else None. Does
    NOT mutate `position` — the caller decides how/whether to apply it,
    matching the read-only style used elsewhere in this codebase (e.g.
    DuplicateOrderGuard.check vs. .record)."""
    state.update_extreme(position, candle)
    candidate = compute_trailing_stop_candidate(position, state, current_atr, atr_multiplier)

    if position.direction == Direction.LONG:
        return candidate if candidate > position.stop_loss else None
    return candidate if candidate < position.stop_loss else None

