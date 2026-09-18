from datetime import date

import pytest

from config_schema import BrokerConfig
from risk_engine import (
    RawAccountSnapshot,
    commission_to_base_currency,
    normalize_account_state,
    to_base_currency,
)


@pytest.fixture
def cents_broker() -> BrokerConfig:
    """Explicitly constructs a cents-account config for this test only —
    the bot's CURRENT broker (XM) defaults to a standard USD account, but
    this module must still work correctly for a cents account, since
    that's exactly what changed once already in this project."""
    return BrokerConfig(account_currency_unit="cents", unit_scale_factor=100.0)


@pytest.fixture
def usd_broker() -> BrokerConfig:
    return BrokerConfig(account_currency_unit="usd", unit_scale_factor=1.0)


class TestToBaseCurrency:
    def test_cents_account_divides_by_scale_factor(self, cents_broker):
        # MT5 shows 1,000,000 for a real $10,000 deposit
        assert to_base_currency(1_000_000.0, cents_broker) == pytest.approx(10_000.0)

    def test_usd_account_passes_through_unchanged(self, usd_broker):
        assert to_base_currency(10_000.0, usd_broker) == pytest.approx(10_000.0)


class TestCommissionConversion:
    def test_usc_commission_converted_to_usd(self):
        assert commission_to_base_currency(3.5) == pytest.approx(0.035)

    def test_round_trip_commission(self):
        one_way = commission_to_base_currency(3.5)
        assert (one_way * 2) == pytest.approx(0.07)


class TestNormalizeAccountState:
    def test_full_snapshot_normalized_correctly(self, cents_broker):
        raw = RawAccountSnapshot(
            equity=1_000_000.0,
            balance=1_000_000.0,
            free_margin=900_000.0,
            used_margin=100_000.0,
            open_positions_count=0,
            daily_start_equity=1_000_000.0,
            peak_equity=1_050_000.0,
            consecutive_losses=1,
            trading_day=date.today(),
        )
        account = normalize_account_state(raw, cents_broker)
        assert account.equity == pytest.approx(10_000.0)
        assert account.balance == pytest.approx(10_000.0)
        assert account.free_margin == pytest.approx(9_000.0)
        assert account.used_margin == pytest.approx(1_000.0)
        assert account.peak_equity == pytest.approx(10_500.0)
        # non-monetary fields pass through untouched
        assert account.open_positions_count == 0
        assert account.consecutive_losses == 1

    def test_normalized_drawdown_percent_matches_real_world_percent(self, cents_broker):
        """The whole point of normalization: drawdown % must come out
        identical whether computed from raw cents values or real USD —
        a percentage is unit-invariant, so a normalization bug would
        only show up as a wrong ABSOLUTE number feeding into it, not
        directly as a wrong percent. This test locks in the expected
        absolute values so such a bug would be caught."""
        raw = RawAccountSnapshot(
            equity=945_000.0,   # $9,450 real
            balance=945_000.0,
            free_margin=800_000.0,
            used_margin=145_000.0,
            open_positions_count=0,
            daily_start_equity=950_000.0,
            peak_equity=1_050_000.0,  # $10,500 real
            consecutive_losses=0,
            trading_day=date.today(),
        )
        account = normalize_account_state(raw, cents_broker)
        assert account.equity == pytest.approx(9_450.0)
        assert account.peak_equity == pytest.approx(10_500.0)
        assert account.drawdown_percent == pytest.approx(0.10, rel=1e-3)
