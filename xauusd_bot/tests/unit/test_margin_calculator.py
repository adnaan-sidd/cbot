import pytest

from config.config_schema import BrokerConfig, RiskConfig
from risk_engine.margin_calculator import calculate_required_margin, check_margin_sufficient


@pytest.fixture
def broker() -> BrokerConfig:
    return BrokerConfig()


@pytest.fixture
def risk() -> RiskConfig:
    return RiskConfig()


class TestCalculateRequiredMargin:
    def test_typical_case(self, broker):
        # lot=0.17, contract_size=100, entry=3600, margin_rate=1.0, leverage=100
        # notional = 0.17*100*3600*1.0 = 61200; margin = 61200/100 = 612.0
        margin = calculate_required_margin(lot_size=0.17, entry_price=3600.0, broker=broker)
        assert margin == pytest.approx(612.0)

    def test_higher_leverage_reduces_required_margin(self, broker):
        low_leverage = broker.model_copy(update={"leverage": 50})
        high_leverage = broker.model_copy(update={"leverage": 100})
        m_low = calculate_required_margin(lot_size=0.10, entry_price=3600.0, broker=low_leverage)
        m_high = calculate_required_margin(lot_size=0.10, entry_price=3600.0, broker=high_leverage)
        assert m_high < m_low

    def test_zero_lot_size_rejected(self, broker):
        with pytest.raises(ValueError):
            calculate_required_margin(lot_size=0, entry_price=3600.0, broker=broker)

    def test_negative_entry_price_rejected(self, broker):
        with pytest.raises(ValueError):
            calculate_required_margin(lot_size=0.1, entry_price=-1, broker=broker)


class TestCheckMarginSufficient:
    def test_sufficient_margin_passes(self, risk):
        result = check_margin_sufficient(required_margin=100.0, free_margin=1000.0, risk=risk)
        # cap=0.5 -> available=500, required=100 -> sufficient
        assert result.sufficient is True

    def test_insufficient_margin_fails(self, risk):
        result = check_margin_sufficient(required_margin=600.0, free_margin=1000.0, risk=risk)
        # available=500, required=600 -> insufficient
        assert result.sufficient is False

    def test_exactly_at_cap_boundary_is_sufficient(self, risk):
        result = check_margin_sufficient(required_margin=500.0, free_margin=1000.0, risk=risk)
        assert result.sufficient is True

    def test_margin_never_increases_lot_size(self, broker, risk):
        """Explicit regression guard for the architecture's hard rule:
        margin availability must never be used to size UP a position.
        This module has no function that takes 'available margin' as an
        input to a sizing decision — only as a pass/fail gate on an
        already-determined lot size. Asserting the function signature
        shape here (margin check takes required_margin, not equity/risk%)
        is a structural proxy for that rule."""
        import inspect

        sig = inspect.signature(check_margin_sufficient)
        assert "equity" not in sig.parameters
        assert "risk_percent" not in sig.parameters
