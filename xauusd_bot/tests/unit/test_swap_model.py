from datetime import datetime

import pytest

from backtesting.swap_model import calculate_swap_cost
from config.config_schema import BrokerConfig
from core.enums import Direction


def _broker(**overrides) -> BrokerConfig:
    kwargs = dict(
        swap_enabled=True,
        swap_long_points=-58.6,
        swap_short_points=40.9,
        triple_swap_weekday=2,  # Wednesday
        weekend_swap_disabled=True,
        digits=2,       # point_size = 0.01
        contract_size=100.0,
    )
    kwargs.update(overrides)
    return BrokerConfig(**kwargs)


class TestSwapDisabledByDefault:
    def test_disabled_returns_zero_regardless_of_holding_period(self):
        broker = _broker(swap_enabled=False)
        cost = calculate_swap_cost(
            direction=Direction.LONG,
            lot_size=1.0,
            opened_at=datetime(2026, 1, 5),   # Monday
            closed_at=datetime(2026, 1, 10),  # Saturday, several nights held
            broker=broker,
        )
        assert cost == 0.0


class TestSameDayTrade:
    def test_no_nights_held_means_no_swap(self):
        broker = _broker()
        cost = calculate_swap_cost(
            direction=Direction.LONG,
            lot_size=1.0,
            opened_at=datetime(2026, 1, 5, 10, 0),
            closed_at=datetime(2026, 1, 5, 18, 0),  # closed same calendar day
            broker=broker,
        )
        assert cost == 0.0


class TestSingleOrdinaryNight:
    def test_long_single_night_matches_hand_calculation(self):
        """Monday -> Tuesday: one ordinary night, no Wednesday, no weekend.
        swap_price_per_lot_per_night = -58.6 * 0.01 * 100 = -58.6
        lot_size=1.0, 1 night -> total = -58.6
        """
        broker = _broker()
        cost = calculate_swap_cost(
            direction=Direction.LONG,
            lot_size=1.0,
            opened_at=datetime(2026, 1, 5),  # Monday
            closed_at=datetime(2026, 1, 6),  # Tuesday
            broker=broker,
        )
        assert cost == pytest.approx(-58.6)

    def test_short_single_night_is_a_credit(self):
        # 40.9 * 0.01 * 100 = 40.9
        broker = _broker()
        cost = calculate_swap_cost(
            direction=Direction.SHORT,
            lot_size=1.0,
            opened_at=datetime(2026, 1, 5),
            closed_at=datetime(2026, 1, 6),
            broker=broker,
        )
        assert cost == pytest.approx(40.9)

    def test_cost_scales_with_lot_size(self):
        broker = _broker()
        cost = calculate_swap_cost(
            direction=Direction.LONG,
            lot_size=0.5,
            opened_at=datetime(2026, 1, 5),
            closed_at=datetime(2026, 1, 6),
            broker=broker,
        )
        assert cost == pytest.approx(-29.3)


class TestWednesdayTripling:
    def test_wednesday_night_charged_triple(self):
        """Wednesday(Jan 7) -> Thursday(Jan 8): the night held is
        Wednesday's, so it's tripled: -58.6 * 3 = -175.8"""
        broker = _broker()
        cost = calculate_swap_cost(
            direction=Direction.LONG,
            lot_size=1.0,
            opened_at=datetime(2026, 1, 7),  # Wednesday
            closed_at=datetime(2026, 1, 8),  # Thursday
            broker=broker,
        )
        assert cost == pytest.approx(-58.6 * 3)


class TestWeekendSkipped:
    def test_saturday_and_sunday_nights_not_charged(self):
        """Friday(Jan 9) -> Monday(Jan 12): nights held are Fri, Sat, Sun.
        Only Friday should be charged (Sat/Sun skipped since
        weekend_swap_disabled=True) -> -58.6 * 1"""
        broker = _broker()
        cost = calculate_swap_cost(
            direction=Direction.LONG,
            lot_size=1.0,
            opened_at=datetime(2026, 1, 9),   # Friday
            closed_at=datetime(2026, 1, 12),  # Monday
            broker=broker,
        )
        assert cost == pytest.approx(-58.6)

    def test_weekend_charged_when_disabled_flag_is_false(self):
        """Same span as above, but with weekend swaps NOT disabled —
        Fri, Sat, Sun should all be charged -> -58.6 * 3"""
        broker = _broker(weekend_swap_disabled=False)
        cost = calculate_swap_cost(
            direction=Direction.LONG,
            lot_size=1.0,
            opened_at=datetime(2026, 1, 9),
            closed_at=datetime(2026, 1, 12),
            broker=broker,
        )
        assert cost == pytest.approx(-58.6 * 3)


class TestSpanningWednesdayAndWeekend:
    def test_full_week_matches_hand_calculation(self):
        """Monday(Jan 5) -> the following Monday(Jan 12): nights held are
        Mon, Tue, Wed, Thu, Fri, Sat, Sun.
        Wed tripled (3x), Sat/Sun skipped -> multiplier sum = 1+1+3+1+1 = 7
        total = -58.6 * 7 = -410.2
        """
        broker = _broker()
        cost = calculate_swap_cost(
            direction=Direction.LONG,
            lot_size=1.0,
            opened_at=datetime(2026, 1, 5),
            closed_at=datetime(2026, 1, 12),
            broker=broker,
        )
        assert cost == pytest.approx(-58.6 * 7)


class TestValidation:
    def test_zero_lot_size_rejected(self):
        broker = _broker()
        with pytest.raises(ValueError):
            calculate_swap_cost(
                direction=Direction.LONG,
                lot_size=0,
                opened_at=datetime(2026, 1, 5),
                closed_at=datetime(2026, 1, 6),
                broker=broker,
            )

    def test_closed_before_opened_rejected(self):
        broker = _broker()
        with pytest.raises(ValueError):
            calculate_swap_cost(
                direction=Direction.LONG,
                lot_size=1.0,
                opened_at=datetime(2026, 1, 6),
                closed_at=datetime(2026, 1, 5),
                broker=broker,
            )

    def test_disabled_swap_skips_validation_entirely(self):
        """When swap is disabled, the function returns 0.0 immediately
        without validating other arguments — a disabled feature should
        never be able to raise, even with nonsensical inputs, since
        callers may call this unconditionally regardless of config."""
        broker = _broker(swap_enabled=False)
        cost = calculate_swap_cost(
            direction=Direction.LONG,
            lot_size=1.0,
            opened_at=datetime(2026, 1, 5),
            closed_at=datetime(2026, 1, 6),
            broker=broker,
        )
        assert cost == 0.0
