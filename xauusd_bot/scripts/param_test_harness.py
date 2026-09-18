"""Ad-hoc harness for iterating on TrendPullbackStrategy parameters
against a real-data CSV slice. Not part of the test suite -- a
throwaway tool for the Phase 4 strategy-tuning work."""
import sys
import time
sys.path.insert(0, '/home/claude/xauusd_bot')

from backtesting.backtest_engine import BacktestEngine
from backtesting.cost_model import CostModel
from backtesting.performance_report import generate_report
from backtesting.slippage_model import VolatilitySlippageModel
from config.config_schema import BotConfig, BrokerConfig, FilterConfig, KillSwitchConfig, RiskConfig
from market_data.historical_feed import HistoricalFeed
from strategy.trend_pullback_strategy import TrendPullbackStrategy
from strategy.trend_pullback_breakout_strategy import TrendPullbackBreakoutStrategy
from strategy.mean_reversion_strategy import MeanReversionStrategy


def run_variant(csv_path, name, strategy_class=TrendPullbackStrategy, trailing_stop_atr_multiplier=None, trailing_stop_atr_period=14, **strategy_kwargs):
    config = BotConfig(
        environment="backtest", risk=RiskConfig(),
        filters=FilterConfig(max_spread_points=35, allowed_sessions=None),
        broker=BrokerConfig(), kill_switch=KillSwitchConfig(),
    )
    feed = HistoricalFeed.from_csv(csv_path)
    candles = list(feed.candles())
    strategy = strategy_class(**strategy_kwargs)
    engine = BacktestEngine(
        feed=HistoricalFeed(candles), strategy=strategy, config=config,
        cost_model=CostModel(commission_per_million_usd=config.broker.commission_per_million_usd),
        slippage_model=VolatilitySlippageModel(range_fraction=0.05),
        starting_equity=10_000.0, symbol="XAUUSD",
        trailing_stop_atr_multiplier=trailing_stop_atr_multiplier,
        trailing_stop_atr_period=trailing_stop_atr_period,
    )
    start = time.time()
    result = engine.run()
    elapsed = time.time() - start
    report = generate_report(result.trades, starting_equity=result.starting_equity)

    from collections import Counter
    reject_reasons = Counter(r.reason.value for r in result.rejected_signals)

    print(f"\n=== {name} ({elapsed:.0f}s, {len(candles)} candles) ===")
    print(f"params: {strategy_kwargs}")
    print(f"trades={report.total_trades} win_rate={report.win_rate:.1%} "
          f"PF={report.profit_factor:.2f} expectancy=${report.expectancy:.2f} "
          f"return={report.total_return_percent:.2%} maxDD={report.max_drawdown_percent:.2%} "
          f"streak={report.longest_losing_streak} rejected={dict(reject_reasons)}")
    return report


if __name__ == "__main__":
    csv_path = sys.argv[1]
    variant = sys.argv[2]

    base = dict(
        htf_minutes=60, htf_fast_ema_period=50, htf_slow_ema_period=200,
        ltf_fast_ema_period=20, atr_period=14, atr_stop_multiplier=1.5,
        reward_risk_ratio=2.0, rsi_period=14,
    )

    variants = {
        "baseline": base,
        "wider_stop": {**base, "atr_stop_multiplier": 2.0},
        "tighter_rsi": {**base, "rsi_long_min": 45.0, "rsi_long_max": 70.0, "rsi_short_min": 30.0, "rsi_short_max": 55.0},
        "faster_htf": {**base, "htf_fast_ema_period": 30, "htf_slow_ema_period": 100},
        "min_atr_filter": {**base, "min_atr_price": 1.0},
        "wider_stop_higher_rr": {**base, "atr_stop_multiplier": 2.0, "reward_risk_ratio": 2.5},
        "tighter_stop_lower_rr": {**base, "atr_stop_multiplier": 1.0, "reward_risk_ratio": 1.5},
    }

    v2_base = dict(
        htf_minutes=60, htf_fast_ema_period=50, htf_slow_ema_period=200, htf_atr_period=14,
        min_trend_strength_atr_multiples=1.0,
        ltf_fast_ema_period=20, min_pullback_bars=2, max_pullback_bars=8,
        atr_period=14, stop_buffer_atr_multiplier=0.3, reward_risk_ratio=2.0, rsi_period=14,
    )

    v2_variants = {
        "v2_baseline": v2_base,
        "v2_stronger_trend_filter": {**v2_base, "min_trend_strength_atr_multiples": 2.0},
        "v2_wider_pullback": {**v2_base, "min_pullback_bars": 3, "max_pullback_bars": 12},
        "v2_bigger_buffer": {**v2_base, "stop_buffer_atr_multiplier": 0.5},
    }

    trailing_variants = {
        "v2_trailing_1.5x": ({**v2_base, "use_trailing_stop": True}, 1.5, 14),
        "v2_trailing_2.0x": ({**v2_base, "use_trailing_stop": True}, 2.0, 14),
        "v2_trailing_bigbuf_1.5x": ({**v2_base, "use_trailing_stop": True, "stop_buffer_atr_multiplier": 0.5}, 1.5, 14),
    }

    mr_variants = {
        "mr_baseline": dict(ema_period=20, atr_period=14, entry_threshold_atr=2.0, stop_atr_multiplier=1.5),
        "mr_tighter_threshold": dict(ema_period=20, atr_period=14, entry_threshold_atr=1.5, stop_atr_multiplier=1.5),
        "mr_wider_threshold": dict(ema_period=20, atr_period=14, entry_threshold_atr=2.5, stop_atr_multiplier=1.5),
        "mr_with_trend_filter": dict(
            ema_period=20, atr_period=14, entry_threshold_atr=2.0, stop_atr_multiplier=1.5,
            htf_minutes=60, htf_fast_ema_period=50, htf_slow_ema_period=200,
            max_htf_trend_strength_atr_multiples=1.0,
        ),
    }

    swing_variants = {
        "swing_h4_d1": dict(
            htf_minutes=1440, htf_fast_ema_period=50, htf_slow_ema_period=200, htf_atr_period=14,
            min_trend_strength_atr_multiples=1.0,
            ltf_fast_ema_period=20, min_pullback_bars=2, max_pullback_bars=8,
            atr_period=14, stop_buffer_atr_multiplier=0.3, reward_risk_ratio=2.0, rsi_period=14,
        ),
        "swing_h4_d1_trailing": dict(
            htf_minutes=1440, htf_fast_ema_period=50, htf_slow_ema_period=200, htf_atr_period=14,
            min_trend_strength_atr_multiples=1.0,
            ltf_fast_ema_period=20, min_pullback_bars=2, max_pullback_bars=8,
            atr_period=14, stop_buffer_atr_multiplier=0.3, reward_risk_ratio=2.0, rsi_period=14,
            use_trailing_stop=True,
        ),
    }

    if variant in swing_variants:
        kwargs = dict(swing_variants[variant])
        trailing = kwargs.pop("use_trailing_stop", False)
        if trailing:
            kwargs["use_trailing_stop"] = True
        run_variant(
            csv_path, variant, strategy_class=TrendPullbackBreakoutStrategy,
            trailing_stop_atr_multiplier=(2.0 if trailing else None), trailing_stop_atr_period=14,
            **kwargs,
        )
    elif variant in mr_variants:
        run_variant(csv_path, variant, strategy_class=MeanReversionStrategy, **mr_variants[variant])
    elif variant in trailing_variants:
        kwargs, mult, period = trailing_variants[variant]
        run_variant(
            csv_path, variant, strategy_class=TrendPullbackBreakoutStrategy,
            trailing_stop_atr_multiplier=mult, trailing_stop_atr_period=period, **kwargs,
        )
    elif variant in v2_variants:
        run_variant(csv_path, variant, strategy_class=TrendPullbackBreakoutStrategy, **v2_variants[variant])
    else:
        run_variant(csv_path, variant, **variants[variant])
