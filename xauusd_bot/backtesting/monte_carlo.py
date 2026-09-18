"""
Monte Carlo analysis (architecture doc §10 — "Monte Carlo analysis where
practical").

Resamples the realized trade P&L sequence WITH replacement to get a
distribution of plausible outcomes, rather than trusting the single
historical path. This does not generate new price data or new strategy
behavior — it only asks "if these same trades had happened in a
different order/frequency, how bad could the ride have been?"
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional

from core.models import Trade


@dataclass
class MonteCarloResult:
    num_simulations: int
    num_trades_per_simulation: int
    percentile_5_final_equity: float
    percentile_50_final_equity: float
    percentile_95_final_equity: float
    percentile_5_max_drawdown_percent: float
    percentile_50_max_drawdown_percent: float
    percentile_95_max_drawdown_percent: float


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        raise ValueError("cannot take a percentile of an empty list")
    idx = round(p * (len(sorted_values) - 1))
    return sorted_values[idx]


def run_monte_carlo(
    trades: list[Trade],
    starting_equity: float,
    *,
    num_simulations: int = 1000,
    seed: Optional[int] = None,
) -> MonteCarloResult:
    if not trades:
        raise ValueError("cannot run Monte Carlo analysis on an empty trade list")
    if starting_equity <= 0:
        raise ValueError("starting_equity must be > 0")
    if num_simulations <= 0:
        raise ValueError("num_simulations must be > 0")

    rng = random.Random(seed)
    pnls = [t.pnl for t in trades]

    final_equities: list[float] = []
    max_drawdowns: list[float] = []

    for _ in range(num_simulations):
        sample = rng.choices(pnls, k=len(pnls))
        equity = starting_equity
        peak = starting_equity
        max_dd = 0.0
        for pnl in sample:
            equity += pnl
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak)
        final_equities.append(equity)
        max_drawdowns.append(max_dd)

    final_equities.sort()
    max_drawdowns.sort()

    return MonteCarloResult(
        num_simulations=num_simulations,
        num_trades_per_simulation=len(trades),
        percentile_5_final_equity=_percentile(final_equities, 0.05),
        percentile_50_final_equity=_percentile(final_equities, 0.50),
        percentile_95_final_equity=_percentile(final_equities, 0.95),
        percentile_5_max_drawdown_percent=_percentile(max_drawdowns, 0.05),
        percentile_50_max_drawdown_percent=_percentile(max_drawdowns, 0.50),
        percentile_95_max_drawdown_percent=_percentile(max_drawdowns, 0.95),
    )
