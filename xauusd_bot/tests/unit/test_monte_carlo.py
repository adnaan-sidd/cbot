from datetime import datetime, timedelta

import pytest

from backtesting.monte_carlo import run_monte_carlo
from core.enums import Direction, ExitReason
from core.models import Trade


def _trade(pnl: float, i: int) -> Trade:
    base = datetime(2026, 1, 1)
    return Trade(
        symbol="XAUUSD",
        direction=Direction.LONG,
        lot_size=0.1,
        entry_price=3600.0,
        exit_price=3600.0 + pnl,
        stop_loss=3590.0,
        take_profit=3620.0,
        opened_at=base + timedelta(hours=i),
        closed_at=base + timedelta(hours=i, minutes=5),
        pnl=pnl,
        pnl_percent=pnl / 10_000.0,
        exit_reason=ExitReason.STOP_LOSS,
        commission=0.07,
        slippage_points=0.1,
    )


class TestRunMonteCarlo:
    def test_empty_trades_rejected(self):
        with pytest.raises(ValueError):
            run_monte_carlo([], starting_equity=10_000.0)

    def test_negative_starting_equity_rejected(self):
        with pytest.raises(ValueError):
            run_monte_carlo([_trade(10, 0)], starting_equity=-1)

    def test_deterministic_with_seed(self):
        trades = [_trade(pnl, i) for i, pnl in enumerate([100, -50, 150, -50, -50])]
        result_a = run_monte_carlo(trades, 10_000.0, num_simulations=200, seed=42)
        result_b = run_monte_carlo(trades, 10_000.0, num_simulations=200, seed=42)
        assert result_a == result_b

    def test_different_seeds_can_differ(self):
        trades = [_trade(pnl, i) for i, pnl in enumerate([100, -50, 150, -50, -50])]
        result_a = run_monte_carlo(trades, 10_000.0, num_simulations=200, seed=1)
        result_b = run_monte_carlo(trades, 10_000.0, num_simulations=200, seed=2)
        # not guaranteed different, but overwhelmingly likely with 200 sims of 5 trades
        assert result_a != result_b

    def test_percentiles_are_correctly_ordered(self):
        trades = [_trade(pnl, i) for i, pnl in enumerate([100, -50, 150, -50, -50, 200, -100])]
        result = run_monte_carlo(trades, 10_000.0, num_simulations=500, seed=7)
        assert result.percentile_5_final_equity <= result.percentile_50_final_equity
        assert result.percentile_50_final_equity <= result.percentile_95_final_equity
        assert result.percentile_5_max_drawdown_percent <= result.percentile_50_max_drawdown_percent
        assert result.percentile_50_max_drawdown_percent <= result.percentile_95_max_drawdown_percent

    def test_all_winning_trades_never_produce_drawdown(self):
        trades = [_trade(50, i) for i in range(5)]
        result = run_monte_carlo(trades, 10_000.0, num_simulations=200, seed=1)
        assert result.percentile_95_max_drawdown_percent == pytest.approx(0.0)
        assert result.percentile_5_final_equity > 10_000.0

    def test_num_trades_per_simulation_matches_input(self):
        trades = [_trade(pnl, i) for i, pnl in enumerate([10, 20, 30])]
        result = run_monte_carlo(trades, 10_000.0, num_simulations=50, seed=1)
        assert result.num_trades_per_simulation == 3
