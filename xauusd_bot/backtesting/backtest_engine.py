"""
Event-driven backtest engine (architecture doc §10 and §11 Phase 3).

Hard rule this engine exists to prove: strategy and risk/filter code are
IDENTICAL between backtest and live — this module never reimplements
sizing, margin, or filter logic. It only supplies simulated market data,
fills, and costs; every trading DECISION still goes through
risk_engine.risk_gate.evaluate_signal() and trade_filters.filter_chain.run_filter_chain()
exactly as Phases 1-2 built them.

Known Phase-3 simplifications (stated explicitly, not hidden):
  - Equity updates only on trade CLOSE, not intrabar (no floating/unrealized
    P&L tracking yet) — true mark-to-market equity tracking is part of
    position_monitor, built in Phase 5. This means a drawdown/kill-switch
    breach can only block NEW trades in backtest; it cannot force-close an
    already-open position, since there is no live floating P&L to react to
    yet. Given max_open_positions=1, the exposure window this leaves is
    bounded to a single trade's worst-case loss (its own stop-loss).
  - If a single candle's range touches both the stop-loss and take-profit,
    the stop-loss is assumed to have been hit first (worst case, not best).
  - Free margin while flat is approximated as full equity (used_margin=0);
    while a position is open, no new entries are evaluated anyway since
    max_open_positions=1, so this approximation never affects a sizing
    decision.
  - Swap/overnight financing cost is calculated (see swap_model.py) but
    only applied if config.broker.swap_enabled is True — it defaults to
    False since this account's swap-free/Islamic status is unconfirmed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from backtesting.cost_model import CostModel, apply_entry_spread, apply_slippage
from backtesting.slippage_model import SlippageModel
from backtesting.swap_model import calculate_swap_cost
from position_monitor.exit_logic import check_stop_or_target
from position_monitor.trailing_stop import TrailingStopState, maybe_update_trailing_stop
from strategy.indicators import atr as compute_atr_series
from config.config_schema import BotConfig
from core.enums import Direction, ExitReason
from core.models import AccountState, Position, RejectedSignal, Trade
from market_data.feed_interface import MarketDataFeed
from risk_engine.risk_gate import evaluate_signal
from risk_engine.risk_limits import ConsecutiveLossTracker
from strategy.strategy_interface import MarketState, Strategy
from trade_filters.duplicate_order_guard import DuplicateOrderGuard
from trade_filters.filter_chain import run_filter_chain


@dataclass
class BacktestResult:
    trades: list[Trade]
    rejected_signals: list[RejectedSignal]
    equity_curve: list[tuple[datetime, float]]
    starting_equity: float
    ending_equity: float


class BacktestEngine:
    def __init__(
        self,
        *,
        feed: MarketDataFeed,
        strategy: Strategy,
        config: BotConfig,
        cost_model: CostModel,
        slippage_model: SlippageModel,
        starting_equity: float,
        symbol: str,
        trailing_stop_atr_multiplier: Optional[float] = None,
        trailing_stop_atr_period: int = 14,
    ):
        self.feed = feed
        self.strategy = strategy
        self.config = config
        self.cost_model = cost_model
        self.slippage_model = slippage_model
        self.symbol = symbol
        self.trailing_stop_atr_multiplier = trailing_stop_atr_multiplier
        self.trailing_stop_atr_period = trailing_stop_atr_period

        self.equity = starting_equity
        self.starting_equity = starting_equity
        self.peak_equity = starting_equity
        self.daily_start_equity = starting_equity
        self.current_trading_day: Optional[date] = None

        self.open_position: Optional[Position] = None
        self._open_entry_commission: float = 0.0
        self._open_equity_at_entry: float = starting_equity
        self._trailing_stop_state: Optional[TrailingStopState] = None

        self.trades: list[Trade] = []
        self.rejected_signals: list[RejectedSignal] = []
        self.equity_curve: list[tuple[datetime, float]] = []
        self.history: list = []

        self.consecutive_loss_tracker = ConsecutiveLossTracker.from_config(config.risk)
        self.duplicate_guard = DuplicateOrderGuard(
            debounce_seconds=config.filters.duplicate_order_debounce_seconds
        )

    def run(self) -> BacktestResult:
        for candle in self.feed.candles():
            self._process_candle(candle)
        return BacktestResult(
            trades=self.trades,
            rejected_signals=self.rejected_signals,
            equity_curve=self.equity_curve,
            starting_equity=self.starting_equity,
            ending_equity=self.equity,
        )

    # -- internals ---------------------------------------------------

    def _process_candle(self, candle) -> None:
        self.history.append(candle)
        self._maybe_roll_daily(candle.timestamp)

        if self.open_position is not None:
            self._check_exit(candle)

        if self.open_position is None:
            self._maybe_enter(candle)

        self.peak_equity = max(self.peak_equity, self.equity)
        self.equity_curve.append((candle.timestamp, self.equity))

    def _maybe_roll_daily(self, timestamp: datetime) -> None:
        day = timestamp.date()
        if self.current_trading_day is None or day != self.current_trading_day:
            self.current_trading_day = day
            self.daily_start_equity = self.equity

    def _account_state(self) -> AccountState:
        # See module docstring: while flat (the only time this is called,
        # since max_open_positions=1), used_margin is always 0.
        return AccountState(
            equity=self.equity,
            balance=self.equity,
            free_margin=self.equity,
            used_margin=0.0,
            open_positions_count=0,
            daily_start_equity=self.daily_start_equity,
            peak_equity=self.peak_equity,
            consecutive_losses=self.consecutive_loss_tracker.consecutive_losses,
            trading_day=self.current_trading_day,
        )

    def _maybe_enter(self, candle) -> None:
        market_state = MarketState(symbol=self.symbol, history=self.history)
        signal = self.strategy.generate_signal(market_state)
        if signal is None:
            return

        spread_points = candle.spread_points if candle.spread_points is not None else 0.0

        filter_result = run_filter_chain(
            signal=signal,
            current_spread_points=spread_points,
            now=candle.timestamp,
            filters=self.config.filters,
            duplicate_guard=self.duplicate_guard,
        )
        if not filter_result.passed:
            self.rejected_signals.append(
                RejectedSignal(
                    signal=signal,
                    reason=filter_result.reason,
                    detail=filter_result.detail,
                    timestamp=candle.timestamp,
                )
            )
            return

        account = self._account_state()
        result = evaluate_signal(
            signal, account, self.config, self.consecutive_loss_tracker, now=candle.timestamp
        )
        if isinstance(result, RejectedSignal):
            self.rejected_signals.append(result)
            return

        # result is a TradeRequest — actually open the position.
        self.duplicate_guard.record(signal, now=candle.timestamp)

        entry_price = apply_entry_spread(
            signal.entry_price, signal.direction, spread_points, self.config.broker.point_size
        )
        slippage = self.slippage_model.get_slippage_price(candle)
        entry_price = apply_slippage(
            entry_price, signal.direction, is_entry=True, slippage_amount=slippage
        )

        self._open_entry_commission = self.cost_model.commission_for_notional(
            lot_size=result.lot_size, fill_price=entry_price, contract_size=self.config.broker.contract_size
        )
        self._open_equity_at_entry = self.equity

        self.open_position = Position(
            symbol=signal.symbol,
            direction=signal.direction,
            lot_size=result.lot_size,
            entry_price=entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            opened_at=candle.timestamp,
        )
        if self.trailing_stop_atr_multiplier is not None and signal.take_profit is None:
            self._trailing_stop_state = TrailingStopState.initial(self.open_position)
        else:
            self._trailing_stop_state = None

    def _check_exit(self, candle) -> None:
        pos = self.open_position
        assert pos is not None

        # IMPORTANT ORDERING: check the exit using the stop-loss as it
        # stood BEFORE this candle, not one tightened using this same
        # candle's own high/low. Updating the trail first would let a
        # candle's favorable move justify a tighter stop that the SAME
        # candle's adverse move then triggers — a real trailing stop
        # could never have been tightened to a level informed by data
        # from later in the same bar's formation. The trail is updated
        # AFTER this check, so it only affects subsequent candles.
        result = check_stop_or_target(pos, candle)
        if result is None:
            self._maybe_update_trailing_stop(candle)
            return
        exit_reason, exit_price = result

        slippage = self.slippage_model.get_slippage_price(candle)
        exit_price = apply_slippage(
            exit_price, pos.direction, is_entry=False, slippage_amount=slippage
        )
        exit_commission = self.cost_model.commission_for_notional(
            lot_size=pos.lot_size, fill_price=exit_price, contract_size=self.config.broker.contract_size
        )
        total_commission = self._open_entry_commission + exit_commission

        swap_cost = calculate_swap_cost(
            direction=pos.direction,
            lot_size=pos.lot_size,
            opened_at=pos.opened_at,
            closed_at=candle.timestamp,
            broker=self.config.broker,
        )

        gross_pnl = self._gross_pnl(pos, exit_price)
        net_pnl = gross_pnl - total_commission + swap_cost

        equity_before = self._open_equity_at_entry
        self.equity = equity_before + net_pnl

        trade = Trade(
            symbol=pos.symbol,
            direction=pos.direction,
            lot_size=pos.lot_size,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit,
            opened_at=pos.opened_at,
            closed_at=candle.timestamp,
            pnl=net_pnl,
            pnl_percent=net_pnl / equity_before if equity_before > 0 else 0.0,
            exit_reason=exit_reason,
            commission=total_commission,
            slippage_points=slippage,
        )
        self.trades.append(trade)
        self.consecutive_loss_tracker.record_result(is_win=net_pnl > 0, at=candle.timestamp)
        self.open_position = None
        self._open_entry_commission = 0.0
        self._trailing_stop_state = None

    def _maybe_update_trailing_stop(self, candle) -> None:
        if self.trailing_stop_atr_multiplier is None or self._trailing_stop_state is None:
            return
        pos = self.open_position
        assert pos is not None
        if pos.take_profit is not None:
            return  # trailing only applies to positions opened without a fixed target

        current_atr = compute_atr_series(self.history, self.trailing_stop_atr_period)[-1]
        if current_atr is None or current_atr <= 0:
            return
        new_stop = maybe_update_trailing_stop(
            pos, self._trailing_stop_state, candle, current_atr, self.trailing_stop_atr_multiplier
        )
        if new_stop is not None:
            pos.stop_loss = new_stop

    def _gross_pnl(self, pos: Position, exit_price: float) -> float:
        price_diff = (
            exit_price - pos.entry_price
            if pos.direction == Direction.LONG
            else pos.entry_price - exit_price
        )
        return price_diff * pos.lot_size * self.config.broker.contract_size
