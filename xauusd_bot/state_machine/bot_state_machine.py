"""
Bot state machine (architecture doc §6).

This is the runtime loop for paper/live trading — the counterpart to
backtest_engine.py, but built for a real-time-arriving feed and routing
every order through an ExecutionInterface instead of simulating fills
inline. It reuses, unmodified: strategy.generate_signal(),
trade_filters.run_filter_chain(), risk_engine.evaluate_signal(), and
position_monitor.exit_logic.check_stop_or_target() — the same functions
backtest_engine.py uses, per the architecture's core rule that backtest
and live never diverge in decision logic.

States (architecture §6): BOOT, IDLE, SCANNING, RISK_CHECK, ORDER_PENDING,
ORDER_FAILED, POSITION_OPEN, MONITORING, EMERGENCY_HALT, KILL_SWITCH_ACTIVE.
KILL_SWITCH_ACTIVE is checked FIRST on every tick, from any prior state —
it is a global interrupt, not a branch reached only from certain states.

Error handling: ExecutionError from place_order/close_position is caught,
logged as ORDER_FAILED, and the state machine returns to IDLE — it never
retries automatically. An error the state machine doesn't know how to
handle safely propagates up rather than being swallowed.
"""

from __future__ import annotations

from typing import Optional

from config.config_schema import BotConfig
from core.enums import EventType, SystemState
from core.events import BotEvent
from core.models import Candle, Position, RejectedSignal, Signal, Trade
from execution.execution_interface import ExecutionError, ExecutionInterface
from persistence.logger import EventLogger
from position_monitor.account_tracker import AccountTracker
from position_monitor.exit_logic import check_stop_or_target
from position_monitor.kill_switch import KillSwitch
from risk_engine.risk_gate import evaluate_signal
from risk_engine.risk_limits import ConsecutiveLossTracker
from strategy.strategy_interface import MarketState, Strategy
from trade_filters.duplicate_order_guard import DuplicateOrderGuard
from trade_filters.filter_chain import run_filter_chain


class BotStateMachine:
    def __init__(
        self,
        *,
        strategy: Strategy,
        config: BotConfig,
        execution: ExecutionInterface,
        starting_balance: float,
        symbol: str,
        event_logger: Optional[EventLogger] = None,
    ):
        self.strategy = strategy
        self.config = config
        self.execution = execution
        self.symbol = symbol
        self.event_logger = event_logger

        self.account_tracker = AccountTracker(starting_balance)
        self.consecutive_loss_tracker = ConsecutiveLossTracker.from_config(config.risk)
        self.duplicate_guard = DuplicateOrderGuard(
            debounce_seconds=config.filters.duplicate_order_debounce_seconds
        )
        self.kill_switch = KillSwitch(risk=config.risk, config=config.kill_switch)

        self.history: list[Candle] = []
        self.open_position: Optional[Position] = None
        self.trades: list[Trade] = []
        self.rejected_signals: list[RejectedSignal] = []
        self.state = SystemState.BOOT

        self._log(EventType.BOOT, f"State machine initialized for {symbol}")
        self.state = SystemState.IDLE

    def on_candle(self, candle: Candle) -> None:
        """Process one new candle/tick. This is the single entry point a
        live feed or a paper-mode replay loop calls for every update."""
        self.history.append(candle)
        self.account_tracker.roll_daily_if_needed(candle.timestamp)

        if hasattr(self.execution, "update_market_price"):
            self.execution.update_market_price(candle)

        broker_connected = self.execution.is_connected()
        account = self.account_tracker.get_live_account_state(
            open_position=self.open_position,
            current_price=candle.close,
            broker=self.config.broker,
            consecutive_losses=self.consecutive_loss_tracker.consecutive_losses,
        )

        if self.kill_switch.check(account, broker_connected=broker_connected):
            self._handle_kill_switch(candle)
            return

        if self.open_position is not None:
            self._check_exit(candle)

        if self.open_position is None:
            self._maybe_enter(candle, account)

    # -- kill switch ----------------------------------------------------

    def _handle_kill_switch(self, candle: Candle) -> None:
        if self.state == SystemState.KILL_SWITCH_ACTIVE:
            return  # already handled — nothing new to do this tick
        self.state = SystemState.KILL_SWITCH_ACTIVE
        if self.open_position is not None:
            try:
                trade = self.execution.close_position(self.open_position, self._kill_switch_exit_reason(), candle.close)
                self.trades.append(trade)
                self.account_tracker.record_realized_trade(trade)
            except ExecutionError as exc:
                # Even the emergency close can fail (e.g. broker
                # disconnected is often WHY we're here). Log loudly —
                # do not retry, do not pretend the position is closed.
                self._log(
                    EventType.ERROR,
                    f"kill switch triggered but position close FAILED: {exc}",
                    payload={"position_id": self.open_position.id},
                )
            else:
                self.open_position = None
        self._log(
            EventType.KILL_SWITCH_TRIGGERED,
            self.kill_switch.triggered_reason or "kill switch triggered",
        )

    def _kill_switch_exit_reason(self):
        from core.enums import ExitReason

        return ExitReason.KILL_SWITCH

    # -- normal trading flow ---------------------------------------------

    def _check_exit(self, candle: Candle) -> None:
        pos = self.open_position
        assert pos is not None
        result = check_stop_or_target(pos, candle)
        if result is None:
            return
        exit_reason, reference_price = result

        try:
            trade = self.execution.close_position(pos, exit_reason, reference_price)
        except ExecutionError as exc:
            self._log(EventType.ERROR, f"failed to close position on {exit_reason.value}: {exc}")
            return  # position remains open in our records; next tick will retry the check, not the close blindly

        self.trades.append(trade)
        self.account_tracker.record_realized_trade(trade)
        self.consecutive_loss_tracker.record_result(is_win=trade.pnl > 0, at=candle.timestamp)
        self.open_position = None
        self.state = SystemState.IDLE
        self._log(
            EventType.POSITION_CLOSED,
            f"{exit_reason.value} at {trade.exit_price}",
            payload={"pnl": trade.pnl},
            trade_id=trade.id,
        )

    def _maybe_enter(self, candle: Candle, account) -> None:
        self.state = SystemState.SCANNING
        market_state = MarketState(symbol=self.symbol, history=self.history)
        signal = self.strategy.generate_signal(market_state)
        if signal is None:
            self.state = SystemState.IDLE
            return

        self._log(
            EventType.SIGNAL_GENERATED,
            f"{signal.direction.value} signal from {signal.strategy_name}",
            signal_id=signal.id,
        )
        self.state = SystemState.RISK_CHECK

        spread_points = candle.spread_points if candle.spread_points is not None else 0.0
        filter_result = run_filter_chain(
            signal=signal,
            current_spread_points=spread_points,
            now=candle.timestamp,
            filters=self.config.filters,
            duplicate_guard=self.duplicate_guard,
        )
        if not filter_result.passed:
            self._reject(signal, filter_result.reason, filter_result.detail, candle.timestamp)
            return

        result = evaluate_signal(
            signal, account, self.config, self.consecutive_loss_tracker, now=candle.timestamp
        )
        if isinstance(result, RejectedSignal):
            self._reject(signal, result.reason, result.detail, candle.timestamp)
            return

        self.duplicate_guard.record(signal, now=candle.timestamp)
        self.state = SystemState.ORDER_PENDING
        try:
            position = self.execution.place_order(result)
        except ExecutionError as exc:
            self.state = SystemState.ORDER_FAILED
            self._log(EventType.ORDER_FAILED, str(exc), signal_id=signal.id)
            self.state = SystemState.IDLE
            return

        self.open_position = position
        self.state = SystemState.MONITORING
        self._log(
            EventType.ORDER_FILLED,
            f"filled {position.direction.value} {position.lot_size} lots @ {position.entry_price}",
            signal_id=signal.id,
        )

    def _reject(self, signal: Signal, reason, detail: str, timestamp) -> None:
        rejected = RejectedSignal(signal=signal, reason=reason, detail=detail, timestamp=timestamp)
        self.rejected_signals.append(rejected)
        self._log(EventType.SIGNAL_REJECTED, detail, signal_id=signal.id)
        self.state = SystemState.IDLE

    def _log(self, event_type: EventType, message: str, **kwargs) -> None:
        if self.event_logger is not None:
            self.event_logger.log(BotEvent(event_type=event_type, message=message, **kwargs))
