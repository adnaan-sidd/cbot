"""
Demonstration script — runs TrendPullbackStrategy through the paper
trading state machine (BotStateMachine + PaperExecutor), replaying
SYNTHETIC price data as if it arrived live, one candle at a time.

Same synthetic-data caveat as run_demo_backtest.py: this proves the
Phase 5 real-time orchestration (state machine, account tracker with
live mark-to-market equity, kill switch) runs correctly end-to-end. It
does not, and cannot, prove anything about real-world profitability.
"""

from __future__ import annotations

from config.config_schema import BotConfig, BrokerConfig, FilterConfig, KillSwitchConfig, RiskConfig
from market_data.historical_feed import HistoricalFeed
from orchestrator.bot_runner import run_paper
from scripts.run_demo_backtest import generate_synthetic_m15_candles
from strategy.trend_pullback_strategy import TrendPullbackStrategy


def build_paper_config() -> BotConfig:
    return BotConfig(
        environment="paper",
        risk=RiskConfig(max_drawdown_percent=0.10),
        filters=FilterConfig(max_spread_points=35, allowed_sessions=None),
        broker=BrokerConfig(),
        kill_switch=KillSwitchConfig(),
    )


if __name__ == "__main__":
    config = build_paper_config()
    candles = generate_synthetic_m15_candles(12_000, seed=42)
    strategy = TrendPullbackStrategy(
        htf_minutes=60, htf_fast_ema_period=50, htf_slow_ema_period=200,
        ltf_fast_ema_period=20, atr_period=14, atr_stop_multiplier=1.5,
        reward_risk_ratio=2.0, rsi_period=14,
    )

    sm = run_paper(
        config=config, feed=HistoricalFeed(candles), strategy=strategy,
        starting_balance=10_000.0, symbol="XAUUSD",
    )

    print(f"Candles processed:      {len(sm.history)}")
    print(f"Final state:            {sm.state.value}")
    print(f"Kill switch armed:      {sm.kill_switch.armed}")
    if not sm.kill_switch.armed:
        print(f"Kill switch reason:     {sm.kill_switch.triggered_reason}")
    print(f"Total trades:           {len(sm.trades)}")
    print(f"Rejected signals:       {len(sm.rejected_signals)}")
    print(f"Realized balance:       ${sm.account_tracker.balance:.2f}")
    print(f"Peak equity seen:       ${sm.account_tracker.peak_equity:.2f}")
    print(f"Open position remains:  {sm.open_position is not None}")
