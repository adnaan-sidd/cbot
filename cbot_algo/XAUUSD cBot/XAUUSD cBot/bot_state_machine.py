"""Bot state machine — cBot port of xauusd_bot/state_machine/bot_state_machine.py.

The core class is a faithful port of the original BotStateMachine —
same states (BOOT, IDLE, SCANNING, RISK_CHECK, ORDER_PENDING,
ORDER_FAILED, POSITION_OPEN, MONITORING, EMERGENCY_HALT,
KILL_SWITCH_ACTIVE), same check order, same "no silent retries" error
handling, reusing the UNCHANGED strategy / filter chain / risk gate /
exit logic from the other ported modules.

Two additions, both required because a cTrader cBot gets data differently
than the external process (which consumed a candle feed on one thread):

  1. `on_monitor_tick()` — a monitoring-only path driven by ticks
     (mark-to-market equity -> kill switch, stop/target checks against
     the forming bar, trailing-stop updates). It never appends to
     history and never evaluates entries: entries happen only on
     CLOSED candles via `on_candle()`, so the strategy can never
     repaint or double-fire. The exit check still runs BEFORE the trail
     update in the same bar, matching the backtest engine's documented
     ordering rule (a trail tightened by bar N's favorable move must
     not be triggered by bar N's own adverse move).

  2. `handle_external_close()` / `reconcile_open_position()` — in
     cTrader the broker enforces SL/TP itself, so a closed position
     arrives via the Positions.Closed event rather than through our
     close_position() call. These reconcile that, mirroring the
     "positions table is reconciled against the broker" rule from
     xauusd_bot's persistence layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from config_schema import BotConfig
from core_enums import EventType, ExitReason, SystemState
from core_events import BotEvent
from core_models import Candle, Position, RejectedSignal, Signal, Trade, now_utc
from execution_interface import ExecutionError, ExecutionInterface
from indicators import atr as compute_atr_series
from position_monitor import (
    AccountTracker,
    KillSwitch,
    TrailingStopState,
    check_stop_or_target,
    maybe_update_trailing_stop,
)
from risk_engine import ConsecutiveLossTracker, evaluate_signal
from strategy_interface import MarketState, Strategy
from trade_filters import DuplicateOrderGuard, run_filter_chain


class BotStateMachine:
    def __init__(
        self,
        *,
        strategy: Strategy,
        config: BotConfig,
        execution: ExecutionInterface,
        starting_balance: float,
        symbol: str,
        event_logger=None,
        trailing_stop_atr_multiplier: Optional[float] = None,
        trailing_stop_atr_period: int = 14,
        hooks: Optional[dict] = None,
        broker_enforced_exits: bool = False,
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

        # Trailing stop (backtest-engine-style constructor params, since
        # the shared BotConfig deliberately has no trailing section).
        self.trailing_stop_atr_multiplier = trailing_stop_atr_multiplier
        self.trailing_stop_atr_period = trailing_stop_atr_period

        # Optional audit hooks (cBot port): called with the object at the
        # moment it happens, so the audit trail sees every lifecycle
        # object, not just events. Keys: signal, rejection, trade,
        # position_open, position_close. None values = no hook.
        self._hooks = hooks or {}

        # cBot port: when True, stop-loss/take-profit are enforced BY THE
        # BROKER (they are attached to the position at the broker), so
        # candle-range exit checks do NOT initiate closes — the broker's
        # close event is the authoritative exit path. This matters
        # specifically for trailing stops, which move mid-bar: a
        # candle-based check could otherwise close a position whose stop
        # was set AFTER the bar's low already occurred. The kill switch
        # (immediate market close) and broker reconciliation are UNAFFECTED
        # by this flag. The repo's external/paper path keeps the default
        # (False), where fills are simulated by the executor.
        self.broker_enforced_exits = broker_enforced_exits

        self.history: list[Candle] = []
        self.open_position: Optional[Position] = None
        self.trades: list[Trade] = []
        self.rejected_signals: list[RejectedSignal] = []
        self.state = SystemState.BOOT

        self._trailing_stop_state: Optional[TrailingStopState] = None
        self._equity_at_entry: float = starting_balance

        # Broker tickets for closes WE initiated (kill switch / exit
        # check). The broker's Closed event for those arrives either
        # synchronously inside close_position() or a little later; either
        # way our own path records the trade, so handle_external_close()
        # ignores these tickets to guarantee single recording.
        self._closing_tickets: set = set()

        self._log(EventType.BOOT, f"State machine initialized for {symbol}")
        self.state = SystemState.IDLE

    # ------------------------------------------------------------------
    # closed-candle path (entries happen HERE and only here)
    # ------------------------------------------------------------------

    def on_candle(self, candle: Candle) -> None:
        """Process one new CLOSED candle. Single entry point the cBot's
        on_bar_closed handler calls for every bar close."""
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

        if self.open_position is not None and not self.broker_enforced_exits:
            self._check_exit(candle)
            # Backtest-engine ordering: trail updates AFTER the exit
            # check, so this bar's own move cannot both tighten and
            # trigger the same stop.
            if self.open_position is not None:
                self._maybe_trail(candle)
        elif self.open_position is not None:
            # broker-enforced mode: SL/TP exit via the broker's close
            # event; we only manage the trailing stop here.
            self._maybe_trail(candle)

        if self.open_position is None:
            self._maybe_enter(candle, account)

    # ------------------------------------------------------------------
    # tick path (monitoring only — no history, no entries)
    # ------------------------------------------------------------------

    def on_monitor_tick(self, candle: Candle, *, broker_connected: bool = True) -> None:
        """Driven by the cBot's on_tick: mark-to-market, kill switch,
        exit checks against the FORMING bar's range, then trailing-stop
        update. Never generates entries and never appends to history —
        that separation is what keeps the closed-candle strategy
        semantics (no repainting, one evaluation per closed bar) intact
        while monitoring runs at tick frequency."""
        self.account_tracker.roll_daily_if_needed(candle.timestamp)

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
            if not self.broker_enforced_exits:
                self._check_exit(candle)
                if self.open_position is None:
                    return
            # Ordering rule from the backtest engine: trail updates happen
            # AFTER the exit check, so this bar's own move cannot both
            # tighten and trigger the same stop.
            self._maybe_trail(candle)

    # ------------------------------------------------------------------
    # broker reconciliation
    # ------------------------------------------------------------------

    def handle_external_close(
        self,
        *,
        broker_ticket: Optional[str],
        exit_price: float,
        exit_reason: ExitReason,
        pnl: float,
        commission: float,
        closed_at: datetime,
        detail: str = "",
    ) -> bool:
        """Record a position the BROKER closed (SL/TP hit, stop-out, or
        manual close in the terminal). Returns True if it consumed the
        event (i.e. it was the tracked position). Never double-records:
        if the position was already accounted for (bot-initiated close),
        open_position is None and this returns False."""
        pos = self.open_position
        if pos is None:
            return False
        if broker_ticket is not None:
            ticket = str(broker_ticket)
            if ticket in self._closing_tickets:
                # we initiated this close ourselves — our own path
                # records the trade; consume the ticket and stand down
                self._closing_tickets.discard(ticket)
                return False
            if pos.broker_ticket is not None and ticket != pos.broker_ticket:
                return False

        trade = Trade(
            symbol=pos.symbol,
            direction=pos.direction,
            lot_size=pos.lot_size,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit,
            opened_at=pos.opened_at,
            closed_at=closed_at,
            pnl=pnl,
            pnl_percent=(pnl / self._equity_at_entry) if self._equity_at_entry > 0 else 0.0,
            exit_reason=exit_reason,
            commission=commission,
            slippage_points=0.0,
        )
        self._record_trade(trade, closed_at)
        self.open_position = None
        self._trailing_stop_state = None
        self.state = SystemState.IDLE
        message = f"broker closed position ({exit_reason.value}) at {exit_price}"
        if detail:
            message += f" — {detail}"
        self._log(
            EventType.POSITION_CLOSED,
            message,
            payload={"pnl": pnl, "commission": commission},
            trade_id=trade.id,
        )
        return True

    def reconcile_open_position(self, current_price: float) -> None:
        """Tick-time safety net: if we track a position the broker no
        longer reports (close event missed, terminal-level close, etc.),
        record a trade from the best information available and clear the
        state. PnL is ESTIMATED from the last known tick price in this
        path — flagged in the audit log; the broker event path always
        prefers the broker's own reported numbers."""
        pos = self.open_position
        if pos is None:
            return
        if self.execution.get_open_position(self.symbol) is not None:
            return
        broker = self.config.broker
        if pos.direction.value == "long":
            pnl = (current_price - pos.entry_price) * pos.lot_size * broker.contract_size
        else:
            pnl = (pos.entry_price - current_price) * pos.lot_size * broker.contract_size
        self._log(
            EventType.ERROR,
            "tracked position no longer reported by broker; close event not captured — "
            "recording estimated trade from last known price",
            payload={"position_id": pos.id, "estimated_exit_price": current_price},
        )
        self.handle_external_close(
            broker_ticket=None,
            exit_price=current_price,
            exit_reason=ExitReason.MANUAL,
            pnl=pnl,
            commission=0.0,
            closed_at=now_utc(),
            detail="position missing from broker; close event not captured (estimated pnl)",
        )

    # -- kill switch ----------------------------------------------------

    def _handle_kill_switch(self, candle: Candle) -> None:
        if self.state == SystemState.KILL_SWITCH_ACTIVE:
            return  # already handled — nothing new to do this tick
        self.state = SystemState.KILL_SWITCH_ACTIVE
        if self.open_position is not None:
            try:
                if self.open_position.broker_ticket is not None:
                    self._closing_tickets.add(self.open_position.broker_ticket)
                trade = self.execution.close_position(
                    self.open_position, ExitReason.KILL_SWITCH, candle.close
                )
                self._record_trade(trade, candle.timestamp)
                self.open_position = None
                self._trailing_stop_state = None
            except ExecutionError as exc:
                # Even the emergency close can fail. Log loudly — do not
                # retry, do not pretend the position is closed.
                self._log(
                    EventType.ERROR,
                    f"kill switch triggered but position close FAILED: {exc}",
                    payload={"position_id": self.open_position.id},
                )
        self._log(
            EventType.KILL_SWITCH_TRIGGERED,
            self.kill_switch.triggered_reason or "kill switch triggered",
        )

    # -- normal trading flow ---------------------------------------------

    def _check_exit(self, candle: Candle) -> None:
        pos = self.open_position
        assert pos is not None
        result = check_stop_or_target(pos, candle)
        if result is None:
            return
        exit_reason, reference_price = result

        if pos.broker_ticket is not None:
            self._closing_tickets.add(pos.broker_ticket)
        try:
            trade = self.execution.close_position(pos, exit_reason, reference_price)
        except ExecutionError as exc:
            self._closing_tickets.discard(pos.broker_ticket) if pos.broker_ticket else None
            self._log(EventType.ERROR, f"failed to close position on {exit_reason.value}: {exc}")
            return  # position remains open in our records; next tick will retry the check

        self._record_trade(trade, candle.timestamp)
        self.open_position = None
        self._trailing_stop_state = None
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

        if self._hooks.get("signal"):
            self._hooks["signal"](signal)

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
        if self._hooks.get("signal_approved"):
            self._hooks["signal_approved"](signal)
        self.state = SystemState.ORDER_PENDING
        try:
            position = self.execution.place_order(result)
        except ExecutionError as exc:
            self.state = SystemState.ORDER_FAILED
            self._log(EventType.ORDER_FAILED, str(exc), signal_id=signal.id)
            self.state = SystemState.IDLE
            return

        self.open_position = position
        if self._hooks.get("position_open"):
            self._hooks["position_open"](position)
        self._equity_at_entry = account.equity
        self._trailing_stop_state = (
            TrailingStopState.initial(position)
            if (self.trailing_stop_atr_multiplier is not None and position.take_profit is None)
            else None
        )
        self.state = SystemState.MONITORING
        self._log(
            EventType.ORDER_FILLED,
            f"filled {position.direction.value} {position.lot_size} lots @ {position.entry_price}",
            signal_id=signal.id,
        )

    def _reject(self, signal: Signal, reason, detail: str, timestamp) -> None:
        rejected = RejectedSignal(signal=signal, reason=reason, detail=detail, timestamp=timestamp)
        self.rejected_signals.append(rejected)
        if self._hooks.get("rejection"):
            self._hooks["rejection"](rejected)
        self._log(EventType.SIGNAL_REJECTED, detail, signal_id=signal.id)
        self.state = SystemState.IDLE

    # -- trailing stop (live: actually moves the broker's stop) ----------

    def _maybe_trail(self, candle: Candle) -> None:
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
        if new_stop is not None and new_stop != pos.stop_loss:
            try:
                updated = self.execution.modify_sl_tp(pos, new_stop_loss=new_stop)
            except ExecutionError as exc:
                self._log(EventType.ERROR, f"trailing stop modification FAILED: {exc}")
                return
            if updated is not None:
                self.open_position = updated
                self._log(
                    EventType.SL_MODIFIED,
                    f"trailing stop moved to {updated.stop_loss}",
                )

    def _record_trade(self, trade: Trade, closed_at) -> None:
        self.trades.append(trade)
        self.account_tracker.record_realized_trade(trade)
        self.consecutive_loss_tracker.record_result(is_win=trade.pnl > 0, at=closed_at)
        if self._hooks.get("trade"):
            self._hooks["trade"](trade)
        if self._hooks.get("position_close"):
            self._hooks["position_close"](self.open_position or trade)

    def _log(self, event_type: EventType, message: str, **kwargs) -> None:
        if self.event_logger is not None:
            self.event_logger.log(BotEvent(event_type=event_type, message=message, **kwargs))
