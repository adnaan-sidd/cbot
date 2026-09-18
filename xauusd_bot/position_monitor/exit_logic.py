"""
Shared exit-condition and unrealized-P&L logic.

Extracted in Phase 5 so live/paper trading (position_monitor) and
backtesting (backtest_engine) use the EXACT SAME stop-loss/take-profit
detection code — this is the architecture's core rule ("backtest and
live share the same risk/strategy code") applied to exit logic too,
which Phase 3 had inlined only inside the backtest engine since nothing
else needed it yet.
"""

from __future__ import annotations

from typing import Optional

from core.enums import Direction, ExitReason
from core.models import Candle, Position


def check_stop_or_target(position: Position, candle: Candle) -> Optional[tuple[ExitReason, float]]:
    """Returns (exit_reason, exit_price_before_slippage) if this candle's
    range touches the position's stop-loss or take-profit, else None.

    Worst-case assumption when a single candle's range touches BOTH: the
    stop-loss is assumed hit first (not the take-profit) — same
    documented assumption as the Phase 3 backtest engine."""
    if position.direction == Direction.LONG:
        hit_sl = candle.low <= position.stop_loss
        hit_tp = position.take_profit is not None and candle.high >= position.take_profit
    else:
        hit_sl = candle.high >= position.stop_loss
        hit_tp = position.take_profit is not None and candle.low <= position.take_profit

    if hit_sl:
        return ExitReason.STOP_LOSS, position.stop_loss
    if hit_tp:
        return ExitReason.TAKE_PROFIT, position.take_profit
    return None


def compute_unrealized_pnl(position: Position, current_price: float, contract_size: float) -> float:
    """Mark-to-market P&L for an OPEN position at `current_price`, before
    commission (commission is only realized on close). This is what
    Phase 3's backtest engine explicitly did NOT compute (equity only
    updated on trade close there) — Phase 5 adds real intrabar/live
    floating P&L so the kill switch can react to a breach WHILE a
    position is open, not only block new ones."""
    price_diff = (
        current_price - position.entry_price
        if position.direction == Direction.LONG
        else position.entry_price - current_price
    )
    return price_diff * position.lot_size * contract_size
