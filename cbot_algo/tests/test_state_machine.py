"""State machine integration tests — port of xauusd_bot's
test_state_machine.py.

The original ran these against PaperExecutor (spread/slippage/cost
model indirection). The cBot project doesn't ship the backtest cost
stack, so this port uses a DeterministicExecutor that fills at the
signal's entry price with zero costs — the DECISION logic under test
(risk_gate, filter_chain, exit_logic, kill switch, state transitions)
is exercised identically, which is the point of the suite.

Also new here: tests for the cBot port's tick path (on_monitor_tick)
and broker-reconciliation entry points (handle_external_close /
reconcile_open_position), which have no external-process counterpart.
"""

from datetime import datetime, timedelta, timezone

import pytest

from bot_state_machine import BotStateMachine
from config_schema import BotConfig, BrokerConfig, FilterConfig, KillSwitchConfig, RiskConfig
from core_enums import Direction, ExitReason, SystemState
from core_models import Candle, Position, Signal, Trade, TradeRequest
from execution_interface import ExecutionInterface
from strategy_interface import MarketState, Strategy


def _config(**risk_overrides) -> BotConfig:
    return BotConfig(
        environment="paper",
        risk=RiskConfig(**risk_overrides),
        filters=FilterConfig(max_spread_points=35, allowed_sessions=None),
        broker=BrokerConfig(),
        kill_switch=KillSwitchConfig(),
    )


def _candle(i: int, price: float, spread_points=10.0, minutes=15) -> Candle:
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes * i)
    return Candle(timestamp=ts, open=price, high=price + 1, low=price - 1, close=price,
                  volume=10, spread_points=spread_points)


class DeterministicExecutor(ExecutionInterface):
    """Zero-cost, zero-slippage fills exactly at the signal's entry price
    and exits exactly at the reference price."""

    def __init__(self):
        self.last_position = None
        self.connected = True

    def place_order(self, trade_request: TradeRequest) -> Position:
        sig = trade_request.signal
        self.last_position = Position(
            symbol=sig.symbol,
            direction=sig.direction,
            lot_size=trade_request.lot_size,
            entry_price=sig.entry_price,
            stop_loss=sig.stop_loss,
            take_profit=sig.take_profit,
            broker_ticket="mock-ticket",
        )
        return self.last_position

    def close_position(self, position: Position, exit_reason: ExitReason, reference_price: float) -> Trade:
        broker = BrokerConfig()
        if position.direction == Direction.LONG:
            pnl = (reference_price - position.entry_price) * position.lot_size * broker.contract_size
        else:
            pnl = (position.entry_price - reference_price) * position.lot_size * broker.contract_size
        now = datetime.now(timezone.utc)
        return Trade(
            symbol=position.symbol,
            direction=position.direction,
            lot_size=position.lot_size,
            entry_price=position.entry_price,
            exit_price=reference_price,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            opened_at=position.opened_at,
            closed_at=now,
            pnl=pnl,
            pnl_percent=0.0,
            exit_reason=exit_reason,
            commission=0.0,
            slippage_points=0.0,
        )

    def modify_sl_tp(self, position, *, new_stop_loss=None, new_take_profit=None) -> Position:
        if new_stop_loss is not None:
            position.stop_loss = new_stop_loss
        if new_take_profit is not None:
            position.take_profit = new_take_profit
        return position

    def get_open_position(self, symbol: str):
        return self.last_position if (self.last_position and self.last_position.symbol == symbol) else None

    def is_connected(self) -> bool:
        return self.connected


def _executor() -> DeterministicExecutor:
    return DeterministicExecutor()


class _OneShotStrategy(Strategy):
    """Fires exactly one signal on the Nth candle, then never again."""

    name = "one_shot_test_only"

    def __init__(self, fire_at_index: int, entry: float, sl: float, tp: float, direction=Direction.LONG):
        self.fire_at_index = fire_at_index
        self.entry = entry
        self.sl = sl
        self.tp = tp
        self.direction = direction
        self._fired = False

    def generate_signal(self, market_state: MarketState):
        idx = len(market_state.history) - 1
        if self._fired or idx != self.fire_at_index:
            return None
        self._fired = True
        return Signal(
            symbol=market_state.symbol, direction=self.direction, entry_price=self.entry,
            stop_loss=self.sl, take_profit=self.tp, strategy_name=self.name,
        )


class _NeverSignalStrategy(Strategy):
    name = "never"

    def generate_signal(self, market_state: MarketState):
        return None


class TestBasicLifecycle:
    def test_boots_into_idle_state(self):
        sm = BotStateMachine(
            strategy=_NeverSignalStrategy(), config=_config(), execution=_executor(),
            starting_balance=10_000.0, symbol="XAUUSD",
        )
        assert sm.state == SystemState.IDLE

    def test_no_signal_stays_idle_and_flat(self):
        sm = BotStateMachine(
            strategy=_NeverSignalStrategy(), config=_config(), execution=_executor(),
            starting_balance=10_000.0, symbol="XAUUSD",
        )
        for i in range(5):
            sm.on_candle(_candle(i, 3600.0))
        assert sm.state == SystemState.IDLE
        assert sm.open_position is None
        assert sm.trades == []

    def test_signal_leads_to_filled_position(self):
        strategy = _OneShotStrategy(fire_at_index=2, entry=3600.0, sl=3590.0, tp=3620.0)
        sm = BotStateMachine(
            strategy=strategy, config=_config(), execution=_executor(),
            starting_balance=10_000.0, symbol="XAUUSD",
        )
        for i in range(3):
            sm.on_candle(_candle(i, 3600.0))
        assert sm.open_position is not None
        assert sm.state == SystemState.MONITORING

    def test_stop_loss_hit_closes_position_and_records_trade(self):
        strategy = _OneShotStrategy(fire_at_index=2, entry=3600.0, sl=3590.0, tp=3620.0)
        sm = BotStateMachine(
            strategy=strategy, config=_config(), execution=_executor(),
            starting_balance=10_000.0, symbol="XAUUSD",
        )
        for i in range(3):
            sm.on_candle(_candle(i, 3600.0))
        assert sm.open_position is not None

        crash_candle = Candle(
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=45),
            open=3595, high=3596, low=3585, close=3590, volume=10, spread_points=10.0,
        )
        sm.on_candle(crash_candle)
        assert sm.open_position is None
        assert len(sm.trades) == 1
        assert sm.trades[0].exit_reason == ExitReason.STOP_LOSS
        assert sm.state == SystemState.IDLE

    def test_take_profit_hit_closes_position_with_profit(self):
        strategy = _OneShotStrategy(fire_at_index=2, entry=3600.0, sl=3590.0, tp=3620.0)
        sm = BotStateMachine(
            strategy=strategy, config=_config(), execution=_executor(),
            starting_balance=10_000.0, symbol="XAUUSD",
        )
        for i in range(3):
            sm.on_candle(_candle(i, 3600.0))

        rally_candle = Candle(
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=45),
            open=3615, high=3625, low=3610, close=3620, volume=10, spread_points=10.0,
        )
        sm.on_candle(rally_candle)
        assert sm.open_position is None
        assert sm.trades[0].exit_reason == ExitReason.TAKE_PROFIT
        assert sm.trades[0].pnl > 0


class TestRejectedSignals:
    def test_rejected_signal_recorded_and_no_position_opened(self):
        # Force a rejection: min-lot-exceeds-risk on a tiny account.
        strategy = _OneShotStrategy(fire_at_index=2, entry=3600.0, sl=3000.0, tp=3900.0)
        sm = BotStateMachine(
            strategy=strategy, config=_config(), execution=_executor(),
            starting_balance=50.0, symbol="XAUUSD",
        )
        for i in range(3):
            sm.on_candle(_candle(i, 3600.0))
        assert sm.open_position is None
        assert len(sm.rejected_signals) == 1
        assert sm.state == SystemState.IDLE


class TestKillSwitchForcesPositionClosed:
    """With proper risk-based sizing, a SINGLE trade's worst-case loss is
    capped near the configured risk % — far below a max-drawdown
    threshold. To exercise the kill switch forcing an OPEN position
    closed, the account is pre-primed 4.8% under peak (threshold 5%),
    then a $25 floating loss breaches it BEFORE the position's own
    30-point stop would have."""

    def _build_and_prime(self, config):
        strategy = _OneShotStrategy(fire_at_index=2, entry=3600.0, sl=3570.0, tp=3900.0)
        sm = BotStateMachine(
            strategy=strategy, config=config, execution=_executor(),
            starting_balance=10_000.0, symbol="XAUUSD",
        )
        sm.account_tracker.balance = 9_520.0  # simulate prior realized losses
        for i in range(3):
            sm.on_candle(_candle(i, 3600.0))
        assert sm.open_position is not None, "test setup assumption failed: position should have opened"
        return sm

    def test_drawdown_breach_force_closes_open_position(self):
        config = _config(max_drawdown_percent=0.05)
        sm = self._build_and_prime(config)

        loss_candle = Candle(
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=45),
            open=3580, high=3582, low=3573, close=3575, volume=10, spread_points=10.0,
        )
        sm.on_candle(loss_candle)

        assert sm.state == SystemState.KILL_SWITCH_ACTIVE
        assert sm.open_position is None  # forced closed
        assert sm.kill_switch.armed is False
        assert any(t.exit_reason == ExitReason.KILL_SWITCH for t in sm.trades)
        assert loss_candle.low > 3570.0  # natural stop never touched

    def test_kill_switch_blocks_new_entries_after_triggering(self):
        config = _config(max_drawdown_percent=0.05)
        sm = self._build_and_prime(config)

        loss_candle = Candle(
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=45),
            open=3580, high=3582, low=3573, close=3575, volume=10, spread_points=10.0,
        )
        sm.on_candle(loss_candle)
        assert sm.state == SystemState.KILL_SWITCH_ACTIVE
        trades_before = len(sm.trades)

        for i in range(10):
            sm.on_candle(_candle(100 + i, 3575.0))
        assert sm.open_position is None
        assert len(sm.trades) == trades_before


# ---------------------------------------------------------------------
# cBot-port additions: tick monitoring path + broker reconciliation
# ---------------------------------------------------------------------

class TestTickMonitoringPath:
    def test_tick_path_marks_to_market_and_trips_kill_switch(self):
        config = _config(max_drawdown_percent=0.05)
        strategy = _OneShotStrategy(fire_at_index=1, entry=3600.0, sl=3570.0, tp=3900.0)
        sm = BotStateMachine(
            strategy=strategy, config=config, execution=_executor(),
            starting_balance=10_000.0, symbol="XAUUSD",
        )
        sm.account_tracker.balance = 9_520.0
        sm.on_candle(_candle(0, 3600.0))
        sm.on_candle(_candle(1, 3600.0))
        assert sm.open_position is not None

        # a tick mid-bar: floating loss breaches drawdown; stop NOT touched
        tick = Candle(
            timestamp=_candle(2, 0.0).timestamp,
            open=3590, high=3590.5, low=3574, close=3575, volume=1, spread_points=10.0,
        )
        sm.on_monitor_tick(tick)
        assert sm.state == SystemState.KILL_SWITCH_ACTIVE
        assert sm.open_position is None
        assert any(t.exit_reason == ExitReason.KILL_SWITCH for t in sm.trades)

    def test_tick_path_never_appends_history_and_never_enters(self):
        class _CountingStrategy(Strategy):
            name = "counting"

            def __init__(self):
                self.calls = 0

            def generate_signal(self, market_state):
                self.calls += 1
                return None

        strategy = _CountingStrategy()
        sm = BotStateMachine(
            strategy=strategy, config=_config(), execution=_executor(),
            starting_balance=10_000.0, symbol="XAUUSD",
        )
        sm.on_candle(_candle(0, 3600.0))
        history_len = len(sm.history)
        sm.on_monitor_tick(_candle(1, 3600.0))
        sm.on_monitor_tick(_candle(2, 3600.0))
        assert len(sm.history) == history_len  # ticks never grow history
        assert strategy.calls == 1             # ... and never evaluate entries

    def test_tick_exit_check_closes_on_forming_bar_sl_touch(self):
        strategy = _OneShotStrategy(fire_at_index=1, entry=3600.0, sl=3590.0, tp=3620.0)
        sm = BotStateMachine(
            strategy=strategy, config=_config(), execution=_executor(),
            starting_balance=10_000.0, symbol="XAUUSD",
        )
        sm.on_candle(_candle(0, 3600.0))
        sm.on_candle(_candle(1, 3600.0))
        assert sm.open_position is not None

        # bar in progress, price dips through the stop
        forming = Candle(
            timestamp=_candle(2, 0.0).timestamp,
            open=3600, high=3600, low=3588, close=3592, volume=1, spread_points=10.0,
        )
        sm.on_monitor_tick(forming)
        assert sm.open_position is None
        assert sm.trades[0].exit_reason == ExitReason.STOP_LOSS


class TestBrokerReconciliation:
    def _sm_with_position(self):
        strategy = _OneShotStrategy(fire_at_index=1, entry=3600.0, sl=3590.0, tp=3620.0)
        sm = BotStateMachine(
            strategy=strategy, config=_config(), execution=_executor(),
            starting_balance=10_000.0, symbol="XAUUSD",
        )
        sm.on_candle(_candle(0, 3600.0))
        sm.on_candle(_candle(1, 3600.0))
        assert sm.open_position is not None
        return sm

    def test_external_close_recorded_once(self):
        sm = self._sm_with_position()
        closed_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        handled = sm.handle_external_close(
            broker_ticket="mock-ticket", exit_price=3590.0,
            exit_reason=ExitReason.STOP_LOSS, pnl=-35.0, commission=1.0, closed_at=closed_at,
        )
        assert handled is True
        assert len(sm.trades) == 1
        assert sm.trades[0].pnl == -35.0
        assert sm.open_position is None
        # the broker event for a bot-initiated close is a no-op:
        handled_again = sm.handle_external_close(
            broker_ticket="mock-ticket", exit_price=3590.0,
            exit_reason=ExitReason.STOP_LOSS, pnl=-35.0, commission=1.0, closed_at=closed_at,
        )
        assert handled_again is False
        assert len(sm.trades) == 1  # no double counting

    def test_external_close_updates_consecutive_losses(self):
        sm = self._sm_with_position()
        sm.handle_external_close(
            broker_ticket="mock-ticket", exit_price=3590.0,
            exit_reason=ExitReason.STOP_LOSS, pnl=-35.0, commission=0.0,
            closed_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        )
        assert sm.consecutive_loss_tracker.consecutive_losses == 1

    def test_reconcile_records_missing_position(self):
        sm = self._sm_with_position()
        sm.execution.last_position = None  # broker no longer reports it
        sm.reconcile_open_position(current_price=3595.0)
        assert sm.open_position is None
        assert len(sm.trades) == 1
        # estimated from last known price: (3595-3600) * lot * 100
        assert sm.trades[0].pnl < 0

    def test_reconcile_noop_when_broker_still_reports_position(self):
        sm = self._sm_with_position()
        sm.reconcile_open_position(current_price=3599.0)
        assert sm.open_position is not None
        assert sm.trades == []
