"""
Every non-trivial number here is computed by hand in the test/comment,
not just asserted against whatever the code produces — this is the
"validate performance report math against hand-computed toy examples"
requirement from the architecture doc.
"""

from datetime import datetime, timedelta

import pytest

from backtesting.performance_report import generate_report
from core.enums import Direction, ExitReason
from core.models import Trade


def _trade(pnl: float, closed_at: datetime, exit_reason=ExitReason.STOP_LOSS) -> Trade:
    return Trade(
        symbol="XAUUSD",
        direction=Direction.LONG,
        lot_size=0.1,
        entry_price=3600.0,
        exit_price=3600.0 + pnl,  # not used in calculations, just needs to be valid
        stop_loss=3590.0,
        take_profit=3620.0,
        opened_at=closed_at - timedelta(minutes=5),
        closed_at=closed_at,
        pnl=pnl,
        pnl_percent=pnl / 10_000.0,
        exit_reason=exit_reason,
        commission=0.07,
        slippage_points=0.1,
    )


class TestGenerateReportEmptyInput:
    def test_no_trades_returns_zeroed_report(self):
        report = generate_report([], starting_equity=10_000.0)
        assert report.total_trades == 0
        assert report.win_rate == 0.0
        assert report.profit_factor == 0.0
        assert report.ending_equity == 10_000.0

    def test_negative_starting_equity_rejected(self):
        with pytest.raises(ValueError):
            generate_report([_trade(10, datetime(2026, 1, 1))], starting_equity=-1)


class TestHandComputedToyExample:
    """Toy sequence, starting_equity=$10,000:
        Trade 1: +$100  (win)
        Trade 2: -$50   (loss)
        Trade 3: +$150  (win)
        Trade 4: -$50   (loss)
        Trade 5: -$50   (loss)

    Hand-computed expectations:
        total_trades       = 5
        wins                = [100, 150]  -> gross_profit = 250
        losses              = [-50, -50, -50] -> gross_loss = 150
        profit_factor       = 250 / 150 = 1.6667
        win_rate            = 2/5 = 0.4
        expectancy          = (100-50+150-50-50)/5 = 100/5 = 20
        average_win         = 250/2 = 125
        average_loss        = 150/3 = 50
        longest_losing_streak = 2   (trades 4 and 5 are consecutive losses)

    Equity curve: 10000 -> 10100 -> 10050 -> 10200 -> 10150 -> 10100
        peak tracks: 10000,10100,10100,10200,10200,10200
        drawdowns:   0, 0, 50, 0, 50, 100
        max_drawdown_amount = 100 (at the final point, peak=10200, equity=10100)
        max_drawdown_percent = 100/10200 = 0.009803...

    ending_equity = 10100
    total_return_percent = 100/10000 = 0.01
    """

    def _build_trades(self) -> list[Trade]:
        base = datetime(2026, 1, 1)
        pnls = [100, -50, 150, -50, -50]
        return [_trade(pnl, base + timedelta(hours=i)) for i, pnl in enumerate(pnls)]

    def test_all_metrics_match_hand_computation(self):
        trades = self._build_trades()
        report = generate_report(trades, starting_equity=10_000.0)

        assert report.total_trades == 5
        assert report.win_rate == pytest.approx(0.4)
        assert report.profit_factor == pytest.approx(250 / 150)
        assert report.expectancy == pytest.approx(20.0)
        assert report.average_win == pytest.approx(125.0)
        assert report.average_loss == pytest.approx(50.0)
        assert report.longest_losing_streak == 2
        assert report.max_drawdown_amount == pytest.approx(100.0)
        assert report.max_drawdown_percent == pytest.approx(100 / 10200)
        assert report.ending_equity == pytest.approx(10_100.0)
        assert report.total_return_percent == pytest.approx(0.01)

    def test_order_independence_of_input_list(self):
        """Trades passed out of chronological order must still be sorted
        internally by closed_at before computing streaks/drawdown."""
        trades = self._build_trades()
        shuffled = [trades[2], trades[0], trades[4], trades[1], trades[3]]
        report_shuffled = generate_report(shuffled, starting_equity=10_000.0)
        report_ordered = generate_report(trades, starting_equity=10_000.0)
        assert report_shuffled == report_ordered


class TestEdgeCases:
    def test_all_wins_profit_factor_is_infinite(self):
        base = datetime(2026, 1, 1)
        trades = [_trade(50, base + timedelta(hours=i)) for i in range(3)]
        report = generate_report(trades, starting_equity=10_000.0)
        assert report.profit_factor == float("inf")
        assert report.average_loss == 0.0

    def test_all_losses_profit_factor_is_zero(self):
        base = datetime(2026, 1, 1)
        trades = [_trade(-50, base + timedelta(hours=i)) for i in range(3)]
        report = generate_report(trades, starting_equity=10_000.0)
        assert report.profit_factor == 0.0
        assert report.average_win == 0.0

    def test_breakeven_trade_counted_in_neither_wins_nor_losses(self):
        base = datetime(2026, 1, 1)
        trades = [
            _trade(100, base),
            _trade(0, base + timedelta(hours=1)),
            _trade(-50, base + timedelta(hours=2)),
        ]
        report = generate_report(trades, starting_equity=10_000.0)
        assert report.total_trades == 3
        assert report.win_rate == pytest.approx(1 / 3)  # only 1 win out of 3

    def test_single_trade_max_drawdown_is_zero_if_it_wins(self):
        report = generate_report([_trade(100, datetime(2026, 1, 1))], starting_equity=10_000.0)
        assert report.max_drawdown_amount == pytest.approx(0.0)
        assert report.max_drawdown_percent == pytest.approx(0.0)

    def test_single_losing_trade_drawdown_matches_loss(self):
        report = generate_report([_trade(-200, datetime(2026, 1, 1))], starting_equity=10_000.0)
        assert report.max_drawdown_amount == pytest.approx(200.0)
        assert report.max_drawdown_percent == pytest.approx(200 / 10_000)
        assert report.longest_losing_streak == 1
