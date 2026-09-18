"""
Performance reporting (architecture doc §10 — required metrics: profit
factor, expectancy, max drawdown, win rate, average win/loss, losing
streak, trade count).

Pure function of a trade list + starting equity — no engine dependency,
so it can be unit-tested against hand-computed toy examples independent
of the backtest engine itself (and reused later for live/paper trade
history reporting, not just backtests).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.models import Trade


@dataclass
class PerformanceReport:
    total_trades: int
    win_rate: float
    profit_factor: float           # gross_profit / abs(gross_loss); inf if no losses and profit>0
    expectancy: float              # average net pnl per trade
    average_win: float             # positive magnitude, 0.0 if no wins
    average_loss: float            # positive magnitude, 0.0 if no losses
    max_drawdown_amount: float
    max_drawdown_percent: float
    longest_losing_streak: int
    starting_equity: float
    ending_equity: float
    total_return_percent: float


def _empty_report(starting_equity: float) -> PerformanceReport:
    return PerformanceReport(
        total_trades=0,
        win_rate=0.0,
        profit_factor=0.0,
        expectancy=0.0,
        average_win=0.0,
        average_loss=0.0,
        max_drawdown_amount=0.0,
        max_drawdown_percent=0.0,
        longest_losing_streak=0,
        starting_equity=starting_equity,
        ending_equity=starting_equity,
        total_return_percent=0.0,
    )


def generate_report(trades: list[Trade], starting_equity: float) -> PerformanceReport:
    if not trades:
        return _empty_report(starting_equity)
    if starting_equity <= 0:
        raise ValueError("starting_equity must be > 0")

    ordered = sorted(trades, key=lambda t: t.closed_at)

    wins = [t for t in ordered if t.pnl > 0]
    losses = [t for t in ordered if t.pnl < 0]

    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))

    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = math.inf if gross_profit > 0 else 0.0

    win_rate = len(wins) / len(ordered)
    expectancy = sum(t.pnl for t in ordered) / len(ordered)
    average_win = gross_profit / len(wins) if wins else 0.0
    average_loss = gross_loss / len(losses) if losses else 0.0

    # Equity curve reconstructed purely from closed trades, in order.
    equity_points = [starting_equity]
    running = starting_equity
    for t in ordered:
        running += t.pnl
        equity_points.append(running)

    peak = equity_points[0]
    max_dd_amount = 0.0
    max_dd_percent = 0.0
    for e in equity_points:
        peak = max(peak, e)
        dd_amount = peak - e
        max_dd_amount = max(max_dd_amount, dd_amount)
        if peak > 0:
            max_dd_percent = max(max_dd_percent, dd_amount / peak)

    longest_losing_streak = 0
    current_streak = 0
    for t in ordered:
        if t.pnl < 0:
            current_streak += 1
            longest_losing_streak = max(longest_losing_streak, current_streak)
        else:
            current_streak = 0

    ending_equity = equity_points[-1]
    total_return_percent = (ending_equity - starting_equity) / starting_equity

    return PerformanceReport(
        total_trades=len(ordered),
        win_rate=win_rate,
        profit_factor=profit_factor,
        expectancy=expectancy,
        average_win=average_win,
        average_loss=average_loss,
        max_drawdown_amount=max_dd_amount,
        max_drawdown_percent=max_dd_percent,
        longest_losing_streak=longest_losing_streak,
        starting_equity=starting_equity,
        ending_equity=ending_equity,
        total_return_percent=total_return_percent,
    )
