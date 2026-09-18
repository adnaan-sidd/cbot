"""
Live/paper account tracking (architecture doc, position_monitor module
table — new in Phase 5).

Phase 3's backtest engine tracked equity by updating it only on trade
close (documented there as a known simplification: no floating/unrealized
P&L, so a drawdown breach could only block NEW trades, never react to
one already open). This module removes that limitation for live/paper
running: `get_live_account_state()` marks the open position to market on
every call, so the kill switch can see a real-time equity figure that
includes the open position's current floating P&L — not just realized
balance.

Also computes free/used margin PROPERLY now (Phase 3 approximated
free_margin=equity while flat, since only one position is ever allowed
open at a time anyway) — Phase 5 tracks the actual required margin of
the currently open position, since that number is now meaningfully used
by more than just a single at-entry check.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from config.config_schema import BrokerConfig
from core.models import AccountState, Position, Trade
from position_monitor.exit_logic import compute_unrealized_pnl
from risk_engine.margin_calculator import calculate_required_margin


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
