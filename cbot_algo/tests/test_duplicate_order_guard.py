from datetime import datetime, timedelta, timezone

import pytest

from core_enums import Direction, RejectReason
from core_models import Signal
from trade_filters import DuplicateOrderGuard


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


class TestDuplicateOrderGuard:
    def test_first_signal_never_a_duplicate(self):
        guard = DuplicateOrderGuard(debounce_seconds=5)
        now = datetime.now(timezone.utc)
        assert guard.check(_signal(), now=now) is None

    def test_check_alone_does_not_record(self):
        guard = DuplicateOrderGuard(debounce_seconds=5)
        now = datetime.now(timezone.utc)
        guard.check(_signal(), now=now)
        guard.check(_signal(), now=now)  # still not a duplicate — check() never records
        assert guard.check(_signal(), now=now) is None

    def test_identical_signal_within_debounce_window_rejected(self):
        guard = DuplicateOrderGuard(debounce_seconds=5)
        now = datetime.now(timezone.utc)
        guard.record(_signal(), now=now)
        result = guard.check(_signal(), now=now + timedelta(seconds=2))
        assert result == RejectReason.DUPLICATE_ORDER

    def test_identical_signal_after_debounce_window_allowed(self):
        guard = DuplicateOrderGuard(debounce_seconds=5)
        now = datetime.now(timezone.utc)
        guard.record(_signal(), now=now)
        result = guard.check(_signal(), now=now + timedelta(seconds=6))
        assert result is None

    def test_different_symbol_not_a_duplicate(self):
        guard = DuplicateOrderGuard(debounce_seconds=5)
        now = datetime.now(timezone.utc)
        guard.record(_signal(symbol="EURUSD"), now=now)
        result = guard.check(_signal(symbol="XAUUSD"), now=now + timedelta(seconds=1))
        assert result is None

    def test_different_direction_not_a_duplicate(self):
        guard = DuplicateOrderGuard(debounce_seconds=5)
        now = datetime.now(timezone.utc)
        guard.record(_signal(direction=Direction.LONG), now=now)
        result = guard.check(
            _signal(direction=Direction.SHORT, entry_price=3600.0, stop_loss=3610.0),
            now=now + timedelta(seconds=1),
        )
        assert result is None

    def test_price_outside_tolerance_not_a_duplicate(self):
        guard = DuplicateOrderGuard(debounce_seconds=5, price_tolerance=0.5)
        now = datetime.now(timezone.utc)
        guard.record(_signal(entry_price=3600.0), now=now)
        result = guard.check(_signal(entry_price=3601.0), now=now + timedelta(seconds=1))
        assert result is None

    def test_price_within_tolerance_is_a_duplicate(self):
        guard = DuplicateOrderGuard(debounce_seconds=5, price_tolerance=0.5)
        now = datetime.now(timezone.utc)
        guard.record(_signal(entry_price=3600.0), now=now)
        result = guard.check(_signal(entry_price=3600.3), now=now + timedelta(seconds=1))
        assert result == RejectReason.DUPLICATE_ORDER

    def test_default_zero_tolerance_requires_exact_match(self):
        guard = DuplicateOrderGuard(debounce_seconds=5)
        now = datetime.now(timezone.utc)
        guard.record(_signal(entry_price=3600.0), now=now)
        result = guard.check(_signal(entry_price=3600.01), now=now + timedelta(seconds=1))
        assert result is None

    def test_old_records_pruned_and_dont_leak_memory(self):
        guard = DuplicateOrderGuard(debounce_seconds=5)
        now = datetime.now(timezone.utc)
        guard.record(_signal(), now=now)
        guard.check(_signal(), now=now + timedelta(seconds=10))  # triggers prune
        assert len(guard._recent) == 0
