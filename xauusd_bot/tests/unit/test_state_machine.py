"""
State machine integration tests. Uses PaperExecutor throughout — no
network, no live broker — but exercises the REAL risk_gate, filter_chain,
and exit_logic exactly as a live/paper run would call them, plus the
kill switch actually forcing a position closed mid-trade, which the
Phase 3 backtest engine could never do (it lacked mark-to-market
equity — see AccountTracker's module docstring).
"""

from datetime import datetime, timedelta

import pytest

from backtesting.cost_model import CostModel
from backtesting.slippage_model import FixedSlippageModel
from config.config_schema import BotConfig, BrokerConfig, FilterConfig, KillSwitchConfig, RiskConfig
from core.enums import Direction, ExitReason, SystemState
from core.models import Candle, Signal
from execution.paper_executor import PaperExecutor
from state_machine.bot_state_machine import BotStateMachine
from strategy.strategy_interface import MarketState, Strategy


def _config(**risk_overrides) -> BotConfig:
    return BotConfig(
        environment="paper",
        risk=RiskConfig(**risk_overrides),
        filters=FilterConfig(max_spread_points=35, allowed_sessions=None),
        broker=BrokerConfig(),
        kill_switch=KillSwitchConfig(),
    )


def _candle(i: int, price: float, spread_points=10.0, minutes=15) -> Candle:
    ts = datetime(2026, 1, 1) + timedelta(minutes=minutes * i)
    return Candle(timestamp=ts, open=price, high=price + 1, low=price - 1, close=price, volume=10, spread_points=spread_points)


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


def _executor() -> PaperExecutor:
    return PaperExecutor(
        broker=BrokerConfig(),
        cost_model=CostModel(commission_per_million_usd=30.0),
        slippage_model=FixedSlippageModel(price_amount=0.0),
    )


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

        # candle whose low touches the stop-loss
        crash_candle = Candle(
            timestamp=datetime(2026, 1, 1) + timedelta(minutes=45),
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
            timestamp=datetime(2026, 1, 1) + timedelta(minutes=45),
            open=3615, high=3625, low=3610, close=3620, volume=10, spread_points=10.0,
        )
        sm.on_candle(rally_candle)
        assert sm.open_position is None
        assert sm.trades[0].exit_reason == ExitReason.TAKE_PROFIT
        assert sm.trades[0].pnl > 0


class TestRejectedSignals:
    def test_rejected_signal_recorded_and_no_position_opened(self):
        # Force a rejection: risk_percent above max via a directly
        # oversized stop (min-lot-exceeds-risk on a tiny account).
        strategy = _OneShotStrategy(fire_at_index=2, entry=3600.0, sl=3000.0, tp=3900.0)
        config = _config()
        sm = BotStateMachine(
            strategy=strategy, config=config, execution=_executor(),
            starting_balance=50.0, symbol="XAUUSD",
        )
        for i in range(3):
            sm.on_candle(_candle(i, 3600.0))
        assert sm.open_position is None
        assert len(sm.rejected_signals) == 1
        assert sm.state == SystemState.IDLE


class TestKillSwitchForcesPositionClosed:
    """
    Important realization baked into these numbers: with proper
    risk-based sizing, a SINGLE trade's worst-case loss (at its own
    stop) is deliberately capped near the configured risk % — far below
    a 5-10% max-drawdown threshold. That's the risk engine working
    correctly, but it also means a single trade can never single-handedly
    breach account drawdown on its own. To actually exercise the kill
    switch forcing an OPEN position closed (not just blocking new ones),
    the account must already be close to the drawdown boundary from
    prior activity — simulated here by directly setting the account
    tracker's balance, representing a rough day/week already behind it.

    Numbers: peak_equity=10,000 (from construction), balance manually
    set to 9,520 (4.8% existing drawdown, threshold=5%). A new position
    risks the normal ~0.35%, sized with sl_distance=30 -> lot=0.01. A
    further $25 floating loss (well within the $30 stop, so the natural
    stop has NOT fired) brings equity to 9,495 -> 5.05% drawdown,
    breaching the 5% kill-switch threshold BEFORE the position's own
    stop-loss would have.
    """

    def _build_and_prime(self, config):
        strategy = _OneShotStrategy(fire_at_index=2, entry=3600.0, sl=3570.0, tp=3900.0)
        sm = BotStateMachine(
            strategy=strategy, config=config, execution=_executor(),
            starting_balance=10_000.0, symbol="XAUUSD",
        )
        sm.account_tracker.balance = 9_520.0  # simulate prior realized losses this session
        for i in range(3):
            sm.on_candle(_candle(i, 3600.0))
        assert sm.open_position is not None, "test setup assumption failed: position should have opened"
        return sm

    def test_drawdown_breach_force_closes_open_position(self):
        config = _config(max_drawdown_percent=0.05)
        sm = self._build_and_prime(config)

        # Floating loss candle: close 25 below entry (well within the 30
        # point stop -- low stays above stop_loss=3570).
        loss_candle = Candle(
            timestamp=datetime(2026, 1, 1) + timedelta(minutes=45),
            open=3580, high=3582, low=3573, close=3575, volume=10, spread_points=10.0,
        )
        sm.on_candle(loss_candle)

        assert sm.state == SystemState.KILL_SWITCH_ACTIVE
        assert sm.open_position is None  # forced closed
        assert sm.kill_switch.armed is False
        assert any(t.exit_reason == ExitReason.KILL_SWITCH for t in sm.trades)
        # confirms this was a FORCED close, not the natural stop -- the
        # candle's low (3573) never reached the stop_loss (3570)
        assert loss_candle.low > 3570.0

    def test_kill_switch_blocks_new_entries_after_triggering(self):
        config = _config(max_drawdown_percent=0.05)
        sm = self._build_and_prime(config)

        loss_candle = Candle(
            timestamp=datetime(2026, 1, 1) + timedelta(minutes=45),
            open=3580, high=3582, low=3573, close=3575, volume=10, spread_points=10.0,
        )
        sm.on_candle(loss_candle)
        assert sm.state == SystemState.KILL_SWITCH_ACTIVE
        trades_before = len(sm.trades)

        # even more candles arrive -- no new position should ever open
        for i in range(10):
            sm.on_candle(_candle(100 + i, 3575.0))
        assert sm.open_position is None
        assert len(sm.trades) == trades_before  # no further trades at all
