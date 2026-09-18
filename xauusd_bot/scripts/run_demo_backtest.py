"""
Demonstration script — runs TrendPullbackStrategy through the full
backtest pipeline (risk_gate + filter_chain + cost/slippage models +
performance_report + Monte Carlo + one stress scenario) on SYNTHETIC
price data.

IMPORTANT: the price data here is a synthetically generated random walk,
NOT real XAUUSD history. This script exists to prove the full pipeline
runs end-to-end with the real Phase 4 strategy, not to claim any
real-world profitability. Before trusting any performance numbers,
re-run this exact script against real historical XAUUSD M15 data loaded
via HistoricalFeed.from_csv().
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from backtesting.backtest_engine import BacktestEngine
from backtesting.cost_model import CostModel
from backtesting.monte_carlo import run_monte_carlo
from backtesting.performance_report import generate_report
from backtesting.slippage_model import VolatilitySlippageModel
from backtesting.stress_tests import SPREAD_BLOWOUT, apply_stress_scenario
from config.config_schema import BotConfig, BrokerConfig, FilterConfig, KillSwitchConfig, RiskConfig
from core.models import Candle
from market_data.historical_feed import HistoricalFeed
from strategy.trend_pullback_strategy import TrendPullbackStrategy


def generate_synthetic_m15_candles(n: int, seed: int = 42, start_price: float = 3600.0) -> list[Candle]:
    """Random walk with mild drift and simple volatility clustering —
    NOT calibrated to real gold statistics, just varied enough to
    exercise trend/pullback/flat conditions across the run."""
    rng = random.Random(seed)
    candles = []
    price = start_price
    vol = 1.0
    ts = datetime(2026, 1, 1)
    for i in range(n):
        # slowly wandering volatility regime
        vol = max(0.3, min(4.0, vol + rng.uniform(-0.05, 0.05)))
        drift = rng.uniform(-0.15, 0.2)  # slight upward bias on average
        change = rng.gauss(drift, vol)
        open_ = price
        close = price + change
        high = max(open_, close) + abs(rng.gauss(0, vol * 0.4))
        low = min(open_, close) - abs(rng.gauss(0, vol * 0.4))
        spread = max(1.0, rng.gauss(3.0, 1.0))
        candles.append(
            Candle(
                timestamp=ts + timedelta(minutes=15 * i),
                open=open_, high=high, low=low, close=close,
                volume=rng.uniform(50, 200), spread_points=spread,
            )
        )
        price = close
    return candles


def build_config() -> BotConfig:
    return BotConfig(
        environment="backtest",
        risk=RiskConfig(),
        filters=FilterConfig(max_spread_points=35, allowed_sessions=None),
        broker=BrokerConfig(),
        kill_switch=KillSwitchConfig(),
    )


def run(candles, config, seed_label=""):
    strategy = TrendPullbackStrategy(
        htf_minutes=60,
        htf_fast_ema_period=50,
        htf_slow_ema_period=200,
        ltf_fast_ema_period=20,
        atr_period=14,
        atr_stop_multiplier=1.5,
        reward_risk_ratio=2.0,
        rsi_period=14,
    )
    cost_model = CostModel(commission_per_million_usd=config.broker.commission_per_million_usd)
    slippage_model = VolatilitySlippageModel(range_fraction=0.05)

    engine = BacktestEngine(
        feed=HistoricalFeed(candles),
        strategy=strategy,
        config=config,
        cost_model=cost_model,
        slippage_model=slippage_model,
        starting_equity=10_000.0,
        symbol="XAUUSD",
    )
    result = engine.run()
    report = generate_report(result.trades, starting_equity=result.starting_equity)
    print(f"\n=== {seed_label} ===")
    print(f"Candles processed:      {len(candles)}")
    print(f"Total trades:           {report.total_trades}")
    print(f"Rejected signals:       {len(result.rejected_signals)}")
    if report.total_trades > 0:
        print(f"Win rate:               {report.win_rate:.1%}")
        print(f"Profit factor:          {report.profit_factor:.2f}")
        print(f"Expectancy/trade:       ${report.expectancy:.2f}")
        print(f"Average win:            ${report.average_win:.2f}")
        print(f"Average loss:           ${report.average_loss:.2f}")
        print(f"Longest losing streak:  {report.longest_losing_streak}")
        print(f"Max drawdown:           {report.max_drawdown_percent:.2%} (${report.max_drawdown_amount:.2f})")
        print(f"Total return:           {report.total_return_percent:.2%}")
        print(f"Ending equity:          ${report.ending_equity:.2f}")
    return result, report


if __name__ == "__main__":
    config = build_config()
    candles = generate_synthetic_m15_candles(12_000, seed=42)

    result, report = run(candles, config, "BASELINE (synthetic data)")

    if report.total_trades >= 5:
        mc = run_monte_carlo(result.trades, starting_equity=10_000.0, num_simulations=2000, seed=7)
        print("\n=== Monte Carlo (2000 resamples of realized trades) ===")
        print(f"5th pct final equity:   ${mc.percentile_5_final_equity:.2f}")
        print(f"50th pct final equity:  ${mc.percentile_50_final_equity:.2f}")
        print(f"95th pct final equity:  ${mc.percentile_95_final_equity:.2f}")
        print(f"5th pct max drawdown:   {mc.percentile_5_max_drawdown_percent:.2%}")
        print(f"50th pct max drawdown:  {mc.percentile_50_max_drawdown_percent:.2%}")
        print(f"95th pct max drawdown:  {mc.percentile_95_max_drawdown_percent:.2%}")

        stressed_feed, stressed_slippage = apply_stress_scenario(
            HistoricalFeed(candles), VolatilitySlippageModel(range_fraction=0.05), SPREAD_BLOWOUT
        )
        strategy2 = TrendPullbackStrategy(
            htf_minutes=60, htf_fast_ema_period=50, htf_slow_ema_period=200,
            ltf_fast_ema_period=20, atr_period=14, atr_stop_multiplier=1.5,
            reward_risk_ratio=2.0, rsi_period=14,
        )
        stressed_engine = BacktestEngine(
            feed=stressed_feed, strategy=strategy2, config=config,
            cost_model=CostModel(commission_per_million_usd=config.broker.commission_per_million_usd),
            slippage_model=stressed_slippage, starting_equity=10_000.0, symbol="XAUUSD",
        )
        stressed_result = stressed_engine.run()
        stressed_report = generate_report(stressed_result.trades, starting_equity=10_000.0)
        print(f"\n=== STRESS: {SPREAD_BLOWOUT.name} (5x spread) ===")
        print(f"Total trades:           {stressed_report.total_trades}")
        if stressed_report.total_trades > 0:
            print(f"Profit factor:          {stressed_report.profit_factor:.2f} (baseline: {report.profit_factor:.2f})")
            print(f"Total return:           {stressed_report.total_return_percent:.2%} (baseline: {report.total_return_percent:.2%})")
    else:
        print("\nToo few trades for Monte Carlo / stress comparison to be meaningful.")
