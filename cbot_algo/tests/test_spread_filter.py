import pytest

from config_schema import FilterConfig
from core_enums import RejectReason
from trade_filters import check_spread


@pytest.fixture
def filters() -> FilterConfig:
    return FilterConfig(max_spread_points=35)


class TestCheckSpread:
    def test_spread_within_limit_passes(self, filters):
        assert check_spread(20.0, filters) is None

    def test_spread_at_exact_limit_passes(self, filters):
        assert check_spread(35.0, filters) is None

    def test_spread_above_limit_rejected(self, filters):
        assert check_spread(35.1, filters) == RejectReason.SPREAD_TOO_WIDE

    def test_zero_spread_passes(self, filters):
        assert check_spread(0.0, filters) is None

    def test_negative_spread_raises(self, filters):
        with pytest.raises(ValueError):
            check_spread(-1.0, filters)
