"""
Orchestrator (architecture doc, orchestrator module table).

Wires the pieces built across Phases 1-5 together per environment.
Backtest keeps using the dedicated, self-contained BacktestEngine from
Phase 3 (fast, no executor indirection needed for historical replay).
Paper and live both run through BotStateMachine — paper with
PaperExecutor, live with the cTrader executor/feed. The only difference
between paper and live at this layer is which ExecutionInterface and
MarketDataFeed get constructed; the state machine, risk gate, and
filters are identical.
"""

from __future__ import annotations

from typing import Optional

from backtesting.backtest_engine import BacktestEngine, BacktestResult
from backtesting.cost_model import CostModel
from backtesting.slippage_model import SlippageModel, VolatilitySlippageModel
from config.config_schema import BotConfig
from execution.execution_interface import ExecutionInterface
from execution.paper_executor import PaperExecutor
from market_data.feed_interface import MarketDataFeed
from persistence.logger import EventLogger
from state_machine.bot_state_machine import BotStateMachine
from strategy.strategy_interface import Strategy


def run_backtest(
    *,
    config: BotConfig,
    feed: MarketDataFeed,
    strategy: Strategy,
    starting_equity: float,
    symbol: str,
    slippage_model: Optional[SlippageModel] = None,
) -> BacktestResult:
    if config.environment != "backtest":
        raise ValueError(f"run_backtest requires environment='backtest', got {config.environment!r}")
    engine = BacktestEngine(
        feed=feed,
        strategy=strategy,
        config=config,
        cost_model=CostModel(commission_per_million_usd=config.broker.commission_per_million_usd),
        slippage_model=slippage_model or VolatilitySlippageModel(range_fraction=0.05),
        starting_equity=starting_equity,
        symbol=symbol,
    )
    return engine.run()


def build_paper_state_machine(
    *,
    config: BotConfig,
    strategy: Strategy,
    starting_balance: float,
    symbol: str,
    slippage_model: Optional[SlippageModel] = None,
    event_logger: Optional[EventLogger] = None,
) -> BotStateMachine:
    if config.environment != "paper":
        raise ValueError(f"build_paper_state_machine requires environment='paper', got {config.environment!r}")
    executor = PaperExecutor(
        broker=config.broker,
        cost_model=CostModel(commission_per_million_usd=config.broker.commission_per_million_usd),
        slippage_model=slippage_model or VolatilitySlippageModel(range_fraction=0.05),
    )
    return BotStateMachine(
        strategy=strategy,
        config=config,
        execution=executor,
        starting_balance=starting_balance,
        symbol=symbol,
        event_logger=event_logger,
    )


def run_paper(
    *,
    config: BotConfig,
    feed: MarketDataFeed,
    strategy: Strategy,
    starting_balance: float,
    symbol: str,
    slippage_model: Optional[SlippageModel] = None,
    event_logger: Optional[EventLogger] = None,
) -> BotStateMachine:
    """Replays `feed` through a paper-trading state machine. `feed` can
    be a HistoricalFeed (for testing/dry-running paper logic against
    known data) or, once market_data/ctrader_feed.py is verified working,
    a live cTrader feed — the state machine code is identical either way.
    """
    sm = build_paper_state_machine(
        config=config,
        strategy=strategy,
        starting_balance=starting_balance,
        symbol=symbol,
        slippage_model=slippage_model,
        event_logger=event_logger,
    )
    for candle in feed.candles():
        sm.on_candle(candle)
    return sm


def build_live_state_machine(
    *,
    config: BotConfig,
    strategy: Strategy,
    execution: ExecutionInterface,
    starting_balance: float,
    symbol: str,
    event_logger: Optional[EventLogger] = None,
) -> BotStateMachine:
    """Wires a state machine for LIVE trading. Unlike the paper/backtest
    builders above, this takes a pre-constructed `execution` (a real
    cTrader executor) rather than building one itself — see
    execution/ctrader_executor.py's module docstring for the mandatory
    verification steps before this is ever used with real capital.
    """
    if config.environment != "live":
        raise ValueError(f"build_live_state_machine requires environment='live', got {config.environment!r}")
    return BotStateMachine(
        strategy=strategy,
        config=config,
        execution=execution,
        starting_balance=starting_balance,
        symbol=symbol,
        event_logger=event_logger,
    )
