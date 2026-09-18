from datetime import date, datetime, timedelta, timezone

import pytest

from config_schema import RiskConfig
from core_enums import RejectReason
from core_models import AccountState
from risk_engine import (
    ConsecutiveLossTracker,
    check_consecutive_loss_cooldown,
    check_daily_loss_limit,
    check_max_drawdown,
    check_max_open_positions,
)


def _account(**overrides) -> AccountState:
    kwargs = dict(
        equity=10_000.0,
        balance=10_000.0,
        free_margin=9_000.0,
        used_margin=1_000.0,
        open_positions_count=0,
        daily_start_equity=10_000.0,
        peak_equity=10_000.0,
        consecutive_losses=0,
        trading_day=date.today(),
    )
    kwargs.update(overrides)
    return AccountState(**kwargs)


@pytest.fixture
def risk() -> RiskConfig:
    return RiskConfig()


class TestMaxOpenPositions:
    def test_below_limit_passes(self, risk):
        assert check_max_open_positions(_account(open_positions_count=0), risk) is None

    def test_at_limit_rejected(self, risk):
        assert (
            check_max_open_positions(_account(open_positions_count=1), risk)
            == RejectReason.POSITION_ALREADY_OPEN
        )


class TestDailyLossLimit:
    def test_below_limit_passes(self, risk):
        account = _account(equity=9_900.0, daily_start_equity=10_000.0)  # 1% loss
        assert check_daily_loss_limit(account, risk) is None

    def test_exactly_at_limit_rejected(self, risk):
        account = _account(equity=9_800.0, daily_start_equity=10_000.0)  # exactly 2%
        assert check_daily_loss_limit(account, risk) == RejectReason.DAILY_LOSS_LIMIT_HIT

    def test_above_limit_rejected(self, risk):
        account = _account(equity=9_700.0, daily_start_equity=10_000.0)  # 3%
        assert check_daily_loss_limit(account, risk) == RejectReason.DAILY_LOSS_LIMIT_HIT

    def test_daily_gain_never_triggers(self, risk):
        account = _account(equity=10_500.0, daily_start_equity=10_000.0)
        assert check_daily_loss_limit(account, risk) is None


class TestMaxDrawdown:
    def test_below_limit_passes(self, risk):
        account = _account(equity=9_600.0, peak_equity=10_000.0)  # 4%
        assert check_max_drawdown(account, risk) is None

    def test_exactly_at_limit_rejected(self, risk):
        account = _account(equity=9_000.0, peak_equity=10_000.0)  # exactly 10%
        assert check_max_drawdown(account, risk) == RejectReason.MAX_DRAWDOWN_HIT

    def test_drawdown_measured_from_peak_not_daily_start(self, risk):
        # Slow bleed across many days: daily loss might look fine each day
        # but drawdown from an all-time peak should still catch it.
        account = _account(equity=8_900.0, daily_start_equity=8_950.0, peak_equity=10_000.0)
        assert check_daily_loss_limit(account, risk) is None  # ~0.56% daily, fine
        assert check_max_drawdown(account, risk) == RejectReason.MAX_DRAWDOWN_HIT  # 11% DD, not fine


class TestConsecutiveLossTracker:
    def test_starts_with_zero_losses_no_cooldown(self):
        tracker = ConsecutiveLossTracker(max_consecutive_losses=3, cooldown_hours=24)
        assert tracker.consecutive_losses == 0
        assert tracker.is_in_cooldown() is False

    def test_losses_below_limit_no_cooldown(self):
        tracker = ConsecutiveLossTracker(max_consecutive_losses=3, cooldown_hours=24)
        tracker.record_result(is_win=False)
        tracker.record_result(is_win=False)
        assert tracker.consecutive_losses == 2
        assert tracker.is_in_cooldown() is False

    def test_hitting_limit_triggers_cooldown(self):
        tracker = ConsecutiveLossTracker(max_consecutive_losses=3, cooldown_hours=24)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        tracker.record_result(is_win=False, at=now)
        tracker.record_result(is_win=False, at=now)
        tracker.record_result(is_win=False, at=now)
        assert tracker.consecutive_losses == 3
        assert tracker.is_in_cooldown(now=now) is True
        assert tracker.cooldown_until == now + timedelta(hours=24)

    def test_cooldown_expires_after_window(self):
        tracker = ConsecutiveLossTracker(max_consecutive_losses=2, cooldown_hours=1)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        tracker.record_result(is_win=False, at=now)
        tracker.record_result(is_win=False, at=now)
        assert tracker.is_in_cooldown(now=now + timedelta(minutes=30)) is True
        assert tracker.is_in_cooldown(now=now + timedelta(hours=1, minutes=1)) is False

    def test_win_resets_streak_and_cooldown(self):
        tracker = ConsecutiveLossTracker(max_consecutive_losses=2, cooldown_hours=24)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        tracker.record_result(is_win=False, at=now)
        tracker.record_result(is_win=False, at=now)
        assert tracker.is_in_cooldown(now=now) is True
        tracker.record_result(is_win=True, at=now)
        assert tracker.consecutive_losses == 0
        assert tracker.is_in_cooldown(now=now) is False

    def test_manual_reset(self):
        tracker = ConsecutiveLossTracker(max_consecutive_losses=2, cooldown_hours=24)
        tracker.record_result(is_win=False)
        tracker.record_result(is_win=False)
        tracker.reset()
        assert tracker.consecutive_losses == 0
        assert tracker.is_in_cooldown() is False

    def test_from_config_uses_risk_config_values(self):
        risk = RiskConfig(max_consecutive_losses=5, consecutive_loss_cooldown_hours=12)
        tracker = ConsecutiveLossTracker.from_config(risk)
        assert tracker.max_consecutive_losses == 5
        assert tracker.cooldown_hours == 12

    def test_check_function_returns_reason_only_when_in_cooldown(self):
        tracker = ConsecutiveLossTracker(max_consecutive_losses=1, cooldown_hours=1)
        assert check_consecutive_loss_cooldown(tracker) is None
        now = datetime.now(timezone.utc)
        tracker.record_result(is_win=False, at=now)
        assert (
            check_consecutive_loss_cooldown(tracker, now=now)
            == RejectReason.CONSECUTIVE_LOSS_COOLDOWN_ACTIVE
        )
