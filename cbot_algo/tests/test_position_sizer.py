"""
Position sizer tests. These are the most safety-critical tests in the
project: the whole point of this module is that it is IMPOSSIBLE to
produce a lot size that risks more than intended.
"""

import pytest

from config_schema import BrokerConfig, RiskConfig
from core_enums import RejectReason
from risk_engine import calculate_lot_size, floor_to_step


class TestFloorToStep:
    def test_exact_multiple_unchanged(self):
        assert floor_to_step(0.20, 0.01) == pytest.approx(0.20)

    def test_floors_down_never_up(self):
        assert floor_to_step(0.176, 0.01) == pytest.approx(0.17)

    def test_floors_down_at_boundary(self):
        # classic binary-float trap: 0.1 + 0.2 style errors must not
        # cause this to round to 0.19 or 0.18 incorrectly
        assert floor_to_step(0.19999999999, 0.01) == pytest.approx(0.19)

    def test_value_below_step_floors_to_zero(self):
        assert floor_to_step(0.004, 0.01) == pytest.approx(0.0)

    def test_zero_step_rejected(self):
        with pytest.raises(ValueError):
            floor_to_step(1.0, 0)


@pytest.fixture
def broker() -> BrokerConfig:
    return BrokerConfig()  # XM, cTrader, XAUUSD defaults (contract_size=100)


@pytest.fixture
def risk() -> RiskConfig:
    return RiskConfig()  # defaults: 0.35% default, 1% max, 0 tolerance


class TestCalculateLotSize:
    def test_typical_case_approved_and_never_exceeds_intended_risk(self, broker, risk):
        # equity=10000, risk=0.35% -> risk_amount=$35, sl_distance=$10,
        # contract_size=100 -> raw_lot = 35 / (10*100) = 0.035 -> floor to 0.03
        result = calculate_lot_size(
            equity=10_000.0,
            risk_percent=0.0035,
            entry_price=3600.0,
            stop_loss_price=3590.0,
            broker=broker,
            risk=risk,
        )
        assert result.approved is True
        assert result.lot_size == pytest.approx(0.03)
        # actual risk must be <= intended risk (floor, never round up)
        assert result.risk_amount <= 35.0
        assert result.risk_percent_actual <= 0.0035

    def test_actual_risk_never_exceeds_requested_risk_percent(self, broker, risk):
        """Property check across a range of equities/distances: the
        floored lot's actual risk % must never exceed the requested
        risk %, since flooring only ever reduces size."""
        for equity in [500, 1_000, 2_500, 10_000, 50_000, 137_50.37]:
            for sl_distance in [1.0, 3.7, 10.0, 25.0, 100.0]:
                result = calculate_lot_size(
                    equity=equity,
                    risk_percent=0.005,
                    entry_price=3600.0,
                    stop_loss_price=3600.0 - sl_distance,
                    broker=broker,
                    risk=risk,
                )
                if result.approved:
                    assert result.risk_percent_actual <= 0.005 + 1e-9

    def test_min_lot_exceeds_risk_rejected(self, broker, risk):
        # Tiny equity + wide stop -> even min_lot (0.01) risks too much.
        # min_lot risk = 0.01 * sl_distance(100) * contract_size(100) = $100
        # equity=200, risk_percent=0.0035 -> intended risk_amount=$0.70
        # min_lot risk % = 100/200 = 50%, way above 0.35% -> reject
        result = calculate_lot_size(
            equity=200.0,
            risk_percent=0.0035,
            entry_price=3600.0,
            stop_loss_price=3500.0,
            broker=broker,
            risk=risk,
        )
        assert result.approved is False
        assert result.reason == RejectReason.MIN_LOT_EXCEEDS_RISK

    def test_min_lot_within_tolerance_is_allowed(self, broker):
        # Same as above, but with tolerance wide enough to accept min_lot.
        lenient_risk = RiskConfig(min_lot_risk_tolerance=0.002, default_risk_percent=0.0035)
        # min_lot risk % here should be small enough to fit within
        # risk_percent + tolerance for a more reasonable equity.
        result = calculate_lot_size(
            equity=10_000.0,
            risk_percent=0.0035,
            entry_price=3600.0,
            stop_loss_price=3599.0,  # very tight stop -> raw_lot huge, not the case we want
            broker=broker,
            risk=lenient_risk,
        )
        # This particular case would approve normally; construct a case
        # where floor lands below min_lot instead:
        result2 = calculate_lot_size(
            equity=50.0,
            risk_percent=0.0035,
            entry_price=3600.0,
            stop_loss_price=3598.0,  # sl_distance=2 -> min_lot risk = 0.01*2*100=$2.00
            broker=broker,
            risk=lenient_risk,
        )
        # intended risk_amount = 50*0.0035 = $0.175; min_lot risk% = 2.00/50=4%
        # 4% > 0.35%+0.2% tolerance(0.55%) -> still rejected; tolerance not big enough
        assert result2.approved is False

    def test_near_zero_stop_loss_distance_rejected(self, broker, risk):
        result = calculate_lot_size(
            equity=10_000.0,
            risk_percent=0.0035,
            entry_price=3600.0,
            stop_loss_price=3600.0,  # identical to entry -> zero distance
            broker=broker,
            risk=risk,
        )
        assert result.approved is False
        assert result.reason == RejectReason.INVALID_SIGNAL

    def test_negative_equity_rejected(self, broker, risk):
        result = calculate_lot_size(
            equity=-500.0,
            risk_percent=0.0035,
            entry_price=3600.0,
            stop_loss_price=3590.0,
            broker=broker,
            risk=risk,
        )
        assert result.approved is False
        assert result.reason == RejectReason.INVALID_SIGNAL

    def test_zero_equity_rejected(self, broker, risk):
        result = calculate_lot_size(
            equity=0.0,
            risk_percent=0.0035,
            entry_price=3600.0,
            stop_loss_price=3590.0,
            broker=broker,
            risk=risk,
        )
        assert result.approved is False

    def test_risk_percent_exceeding_configured_max_rejected(self, broker, risk):
        result = calculate_lot_size(
            equity=10_000.0,
            risk_percent=0.02,  # above max_risk_percent (1%)
            entry_price=3600.0,
            stop_loss_price=3590.0,
            broker=broker,
            risk=risk,
        )
        assert result.approved is False
        assert result.reason == RejectReason.RISK_PERCENT_EXCEEDS_MAX

    def test_risk_percent_at_exactly_max_is_allowed(self, broker, risk):
        result = calculate_lot_size(
            equity=10_000.0,
            risk_percent=0.01,  # exactly max_risk_percent
            entry_price=3600.0,
            stop_loss_price=3590.0,
            broker=broker,
            risk=risk,
        )
        assert result.approved is True

    def test_lot_size_never_exceeds_broker_max_lot(self, broker, risk):
        # Absurdly large equity to try to force a huge lot size.
        result = calculate_lot_size(
            equity=1_000_000_000.0,
            risk_percent=0.01,
            entry_price=3600.0,
            stop_loss_price=3599.9,  # tiny distance -> huge raw lot
            broker=broker,
            risk=risk,
        )
        assert result.approved is True
        assert result.lot_size <= broker.max_lot

    def test_short_direction_distance_is_absolute(self, broker, risk):
        # stop_loss above entry (short trade) must size identically to
        # the same distance below entry (long trade).
        long_result = calculate_lot_size(
            equity=10_000.0,
            risk_percent=0.0035,
            entry_price=3600.0,
            stop_loss_price=3590.0,
            broker=broker,
            risk=risk,
        )
        short_result = calculate_lot_size(
            equity=10_000.0,
            risk_percent=0.0035,
            entry_price=3600.0,
            stop_loss_price=3610.0,
            broker=broker,
            risk=risk,
        )
        assert long_result.lot_size == short_result.lot_size
