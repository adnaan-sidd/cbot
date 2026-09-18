from datetime import datetime, timedelta

import pytest

from config.config_schema import BotConfig, BrokerConfig, FilterConfig, KillSwitchConfig, RiskConfig
from core.enums import Direction
from core.models import Candle, Signal
from market_data.historical_feed import HistoricalFeed
from orchestrator.bot_runner import build_paper_state_machine, run_backtest, run_paper
from strategy.strategy_interface import MarketState, Strategy


def _config(environment: str) -> BotConfig:
    return BotConfig(
        environment=environment,
        risk=RiskConfig(),
        filters=FilterConfig(max_spread_points=35, allowed_sessions=None),
        broker=BrokerConfig(),
        kill_switch=KillSwitchConfig(),
    )


def _candles(n: int, price: float = 3600.0) -> list[Candle]:
    return [
        Candle(
            timestamp=datetime(2026, 1, 1) + timedelta(minutes=15 * i),
            open=price, high=price + 1, low=price - 1, close=price, volume=10, spread_points=10.0,
        )
        for i in range(n)
    ]


class _OneShotStrategy(Strategy):
    name = "one_shot_test_only"

    def __init__(self, fire_at_index=2):
        self.fire_at_index = fire_at_index
        self._fired = False

    def generate_signal(self, market_state: MarketState):
        idx = len(market_state.history) - 1
        if self._fired or idx != self.fire_at_index:
            return None
        self._fired = True
        return Signal(
            symbol=market_state.symbol, direction=Direction.LONG, entry_price=3600.0,
            stop_loss=3590.0, take_profit=3620.0, strategy_name=self.name,
        )


class TestRunBacktest:
    def test_wrong_environment_rejected(self):
        with pytest.raises(ValueError):
            run_backtest(
                config=_config("paper"), feed=HistoricalFeed(_candles(5)),
                strategy=_OneShotStrategy(), starting_equity=10_000.0, symbol="XAUUSD",
            )

    def test_runs_and_produces_a_result(self):
        result = run_backtest(
            config=_config("backtest"), feed=HistoricalFeed(_candles(10)),
            strategy=_OneShotStrategy(), starting_equity=10_000.0, symbol="XAUUSD",
        )
        assert result.starting_equity == pytest.approx(10_000.0)
        assert len(result.equity_curve) == 10


class TestBuildPaperStateMachine:
    def test_wrong_environment_rejected(self):
        with pytest.raises(ValueError):
            build_paper_state_machine(
                config=_config("backtest"), strategy=_OneShotStrategy(),
                starting_balance=10_000.0, symbol="XAUUSD",
            )

    def test_builds_a_ready_state_machine(self):
        sm = build_paper_state_machine(
            config=_config("paper"), strategy=_OneShotStrategy(),
            starting_balance=10_000.0, symbol="XAUUSD",
        )
        assert sm.state.value == "idle"


class TestRunPaper:
    def test_replays_feed_and_opens_a_position(self):
        sm = run_paper(
            config=_config("paper"), feed=HistoricalFeed(_candles(5)),
            strategy=_OneShotStrategy(), starting_balance=10_000.0, symbol="XAUUSD",
        )
        assert sm.open_position is not None
        assert len(sm.history) == 5
