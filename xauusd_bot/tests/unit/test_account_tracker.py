from datetime import date, datetime, timedelta

import pytest

from config.config_schema import BrokerConfig
from core.enums import Direction, ExitReason
from core.models import Position, Trade
from position_monitor.account_tracker import AccountTracker


def _broker() -> BrokerConfig:
    return BrokerConfig()  # contract_size=100, leverage=100


def _position(entry=3600.0, lot=0.1) -> Position:
    return Position(
        symbol="XAUUSD", direction=Direction.LONG, lot_size=lot,
        entry_price=entry, stop_loss=3590.0, take_profit=3620.0,
    )


def _trade(pnl: float) -> Trade:
    return Trade(
        symbol="XAUUSD", direction=Direction.LONG, lot_size=0.1,
        entry_price=3600.0, exit_price=3600.0 + pnl, stop_loss=3590.0, take_profit=3620.0,
        opened_at=datetime(2026, 1, 1), closed_at=datetime(2026, 1, 1, 1),
        pnl=pnl, pnl_percent=pnl / 10_000, exit_reason=ExitReason.TAKE_PROFIT,
        commission=0.5, slippage_points=0.1,
    )


class TestConstruction:
    def test_negative_starting_balance_rejected(self):
        with pytest.raises(ValueError):
            AccountTracker(-100.0)

    def test_zero_starting_balance_rejected(self):
        with pytest.raises(ValueError):
            AccountTracker(0.0)


class TestDailyRollover:
    def test_first_call_sets_trading_day(self):
        tracker = AccountTracker(10_000.0)
        tracker.roll_daily_if_needed(datetime(2026, 1, 5, 10, 0))
        assert tracker.current_trading_day == date(2026, 1, 5)

    def test_same_day_does_not_reset_daily_start(self):
        tracker = AccountTracker(10_000.0)
        tracker.roll_daily_if_needed(datetime(2026, 1, 5, 10, 0))
        tracker.balance = 9_900.0  # simulate a realized loss within the day
        tracker.roll_daily_if_needed(datetime(2026, 1, 5, 15, 0))
        assert tracker.daily_start_equity == 10_000.0  # unchanged

    def test_new_day_resets_daily_start_to_current_balance(self):
        tracker = AccountTracker(10_000.0)
        tracker.roll_daily_if_needed(datetime(2026, 1, 5, 23, 0))
        tracker.balance = 9_900.0
        tracker.roll_daily_if_needed(datetime(2026, 1, 6, 0, 5))
        assert tracker.daily_start_equity == 9_900.0


class TestRecordRealizedTrade:
    def test_balance_updates_by_pnl(self):
        tracker = AccountTracker(10_000.0)
        tracker.record_realized_trade(_trade(150.0))
        assert tracker.balance == pytest.approx(10_150.0)

    def test_peak_equity_only_moves_upward(self):
        tracker = AccountTracker(10_000.0)
        tracker.record_realized_trade(_trade(200.0))
        assert tracker.peak_equity == pytest.approx(10_200.0)
        tracker.record_realized_trade(_trade(-500.0))
        assert tracker.balance == pytest.approx(9_700.0)
        assert tracker.peak_equity == pytest.approx(10_200.0)  # unchanged by a loss


class TestGetLiveAccountState:
    def test_requires_daily_roll_first(self):
        tracker = AccountTracker(10_000.0)
        with pytest.raises(RuntimeError):
            tracker.get_live_account_state(
                open_position=None, current_price=None, broker=_broker(), consecutive_losses=0
            )

    def test_flat_account_equity_equals_balance(self):
        tracker = AccountTracker(10_000.0)
        tracker.roll_daily_if_needed(datetime(2026, 1, 5))
        account = tracker.get_live_account_state(
            open_position=None, current_price=None, broker=_broker(), consecutive_losses=0
        )
        assert account.equity == pytest.approx(10_000.0)
        assert account.used_margin == pytest.approx(0.0)
        assert account.open_positions_count == 0

    def test_open_position_marks_to_market(self):
        tracker = AccountTracker(10_000.0)
        tracker.roll_daily_if_needed(datetime(2026, 1, 5))
        position = _position(entry=3600.0, lot=0.1)
        account = tracker.get_live_account_state(
            open_position=position, current_price=3620.0, broker=_broker(), consecutive_losses=0
        )
        # floating pnl = (3620-3600)*0.1*100 = 200
        assert account.equity == pytest.approx(10_200.0)
        assert account.balance == pytest.approx(10_000.0)  # balance itself is unaffected until realized
        assert account.open_positions_count == 1

    def test_open_position_used_margin_computed(self):
        tracker = AccountTracker(10_000.0)
        tracker.roll_daily_if_needed(datetime(2026, 1, 5))
        position = _position(entry=3600.0, lot=0.1)
        account = tracker.get_live_account_state(
            open_position=position, current_price=3600.0, broker=_broker(), consecutive_losses=0
        )
        # required margin = 0.1*100*3600*1/100 = 360.0
        assert account.used_margin == pytest.approx(360.0)
        assert account.free_margin == pytest.approx(10_000.0 - 360.0)

    def test_no_current_price_yet_treats_floating_pnl_as_zero(self):
        tracker = AccountTracker(10_000.0)
        tracker.roll_daily_if_needed(datetime(2026, 1, 5))
        position = _position(entry=3600.0)
        account = tracker.get_live_account_state(
            open_position=position, current_price=None, broker=_broker(), consecutive_losses=0
        )
        assert account.equity == pytest.approx(10_000.0)

    def test_floating_loss_updates_peak_equity_tracking_correctly(self):
        """Peak equity must reflect the REAL historical high, and must
        never be pulled down by a floating loss -- it only ever holds
        steady or increases."""
        tracker = AccountTracker(10_000.0)
        tracker.roll_daily_if_needed(datetime(2026, 1, 5))
        position = _position(entry=3600.0)

        # first mark: a floating gain raises peak_equity
        tracker.get_live_account_state(
            open_position=position, current_price=3650.0, broker=_broker(), consecutive_losses=0
        )
        assert tracker.peak_equity > 10_000.0
        peak_after_gain = tracker.peak_equity

        # second mark: a floating LOSS must not reduce peak_equity
        account = tracker.get_live_account_state(
            open_position=position, current_price=3550.0, broker=_broker(), consecutive_losses=0
        )
        assert tracker.peak_equity == pytest.approx(peak_after_gain)
        assert account.equity < account.peak_equity  # drawdown is visible in the returned state

    def test_consecutive_losses_passed_through(self):
        tracker = AccountTracker(10_000.0)
        tracker.roll_daily_if_needed(datetime(2026, 1, 5))
        account = tracker.get_live_account_state(
            open_position=None, current_price=None, broker=_broker(), consecutive_losses=3
        )
        assert account.consecutive_losses == 3
