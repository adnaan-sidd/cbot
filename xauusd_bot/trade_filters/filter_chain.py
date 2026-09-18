"""
Filter chain (architecture doc, trade_filters module table).

Runs spread -> session -> duplicate-order checks in that order (cheapest
and most-likely-to-fail first), short-circuiting on the first failure.
This sits alongside risk_gate.evaluate_signal — a signal must pass BOTH
the filter chain and the risk gate before becoming a TradeRequest. They
are kept as separate modules because they answer different questions:
risk_gate asks "is this trade sized/margined safely for THIS account",
filter_chain asks "is right now/this exact idea okay to trade at all".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from config.config_schema import FilterConfig
from core.enums import RejectReason
from core.models import Signal
from trade_filters.duplicate_order_guard import DuplicateOrderGuard
from trade_filters.session_filter import check_session
from trade_filters.spread_filter import check_spread


@dataclass
class FilterResult:
    passed: bool
    reason: Optional[RejectReason] = None
    detail: str = ""


def run_filter_chain(
    *,
    signal: Signal,
    current_spread_points: float,
    now: datetime,
    filters: FilterConfig,
    duplicate_guard: DuplicateOrderGuard,
) -> FilterResult:
    reason = check_spread(current_spread_points, filters)
    if reason:
        return FilterResult(
            passed=False,
            reason=reason,
            detail=f"spread={current_spread_points} > max_spread_points={filters.max_spread_points}",
        )

    reason = check_session(now, filters)
    if reason:
        return FilterResult(
            passed=False,
            reason=reason,
            detail=f"timestamp {now.isoformat()} outside allowed_sessions={filters.allowed_sessions}",
        )

    reason = duplicate_guard.check(signal, now=now)
    if reason:
        return FilterResult(
            passed=False,
            reason=reason,
            detail=(
                f"matching {signal.direction.value} {signal.symbol} signal near "
                f"{signal.entry_price} submitted within the last "
                f"{duplicate_guard.debounce_seconds}s"
            ),
        )

    return FilterResult(passed=True)
