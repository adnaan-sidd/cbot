from datetime import datetime, timedelta, timezone

import pytest

from config.config_schema import FilterConfig
from core.enums import Direction, RejectReason
from core.models import Signal
from trade_filters.duplicate_order_guard import DuplicateOrderGuard
from trade_filters.filter_chain import run_filter_chain


def _signal(**overrides) -> Signal:
    kwargs = dict(
        symbol="XAUUSD",
        direction=Direction.LONG,
        entry_price=3600.0,
        stop_loss=3590.0,
        strategy_name="test_strategy",
    )
    kwargs.update(overrides)
    return Signal(**kwargs)


class TestRunFilterChain:
    def test_all_filters_pass(self):
        filters = FilterConfig(max_spread_points=35, allowed_sessions=None)
        guard = DuplicateOrderGuard(debounce_seconds=5)
        result = run_filter_chain(
            signal=_signal(),
            current_spread_points=20.0,
            now=datetime.now(timezone.utc),
            filters=filters,
            duplicate_guard=guard,
        )
        assert result.passed is True
        assert result.reason is None

    def test_spread_checked_first_and_short_circuits(self):
        # Also configure a session restriction that would ALSO fail, to
        # prove spread is reported first (cheapest check, evaluated first).
        filters = FilterConfig(
            max_spread_points=10, allowed_sessions=["MON-FRI:01:00-23:58"]
        )
        guard = DuplicateOrderGuard(debounce_seconds=5)
        sunday = datetime(2026, 1, 4, 12, 0, tzinfo=timezone.utc)  # also fails session
        result = run_filter_chain(
            signal=_signal(),
            current_spread_points=50.0,  # fails spread
            now=sunday,
            filters=filters,
            duplicate_guard=guard,
        )
        assert result.passed is False
        assert result.reason == RejectReason.SPREAD_TOO_WIDE

    def test_session_checked_when_spread_passes(self):
        filters = FilterConfig(max_spread_points=35, allowed_sessions=["MON-FRI:01:00-23:58"])
        guard = DuplicateOrderGuard(debounce_seconds=5)
        sunday = datetime(2026, 1, 4, 12, 0, tzinfo=timezone.utc)
        result = run_filter_chain(
            signal=_signal(),
            current_spread_points=20.0,
            now=sunday,
            filters=filters,
            duplicate_guard=guard,
        )
        assert result.passed is False
        assert result.reason == RejectReason.SESSION_CLOSED

    def test_duplicate_checked_last(self):
        filters = FilterConfig(max_spread_points=35, allowed_sessions=None)
        guard = DuplicateOrderGuard(debounce_seconds=5)
        now = datetime.now(timezone.utc)
        signal = _signal()
        guard.record(signal, now=now)
        result = run_filter_chain(
            signal=signal,
            current_spread_points=20.0,
            now=now + timedelta(seconds=1),
            filters=filters,
            duplicate_guard=guard,
        )
        assert result.passed is False
        assert result.reason == RejectReason.DUPLICATE_ORDER

    def test_run_filter_chain_does_not_auto_record(self):
        """The chain only CHECKS the duplicate guard — recording is a
        separate, explicit step the orchestrator takes after a signal
        is fully approved (risk gate + filters), not something the
        filter chain does implicitly."""
        filters = FilterConfig(max_spread_points=35, allowed_sessions=None)
        guard = DuplicateOrderGuard(debounce_seconds=5)
        now = datetime.now(timezone.utc)
        signal = _signal()
        run_filter_chain(
            signal=signal,
            current_spread_points=20.0,
            now=now,
            filters=filters,
            duplicate_guard=guard,
        )
        # Running the chain again immediately should still pass, since
        # nothing was ever recorded.
        result = run_filter_chain(
            signal=signal,
            current_spread_points=20.0,
            now=now + timedelta(milliseconds=1),
            filters=filters,
            duplicate_guard=guard,
        )
        assert result.passed is True
