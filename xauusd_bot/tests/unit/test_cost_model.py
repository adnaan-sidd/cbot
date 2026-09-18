import pytest

from backtesting.cost_model import CostModel, apply_entry_spread, apply_slippage
from core.enums import Direction


class TestCostModel:
    def test_commission_for_notional_matches_xm_spec(self):
        # $30 per $1M notional, one way. lot=1.0, contract_size=100, price=3600
        # -> notional = 1.0*100*3600 = 360,000 -> commission = 0.36 * 30 = $10.80
        model = CostModel(commission_per_million_usd=30.0)
        commission = model.commission_for_notional(lot_size=1.0, fill_price=3600.0, contract_size=100.0)
        assert commission == pytest.approx(10.80)

    def test_commission_scales_with_lot_size(self):
        model = CostModel(commission_per_million_usd=30.0)
        c1 = model.commission_for_notional(lot_size=0.17, fill_price=3600.0, contract_size=100.0)
        c2 = model.commission_for_notional(lot_size=0.34, fill_price=3600.0, contract_size=100.0)
        assert c2 == pytest.approx(c1 * 2)

    def test_commission_scales_with_fill_price(self):
        """Unlike a flat per-lot commission, this structure means the SAME
        lot size costs more commission at a higher gold price — this is
        the whole reason entry and exit commission must be computed from
        their own fill prices rather than doubling a single calculation."""
        model = CostModel(commission_per_million_usd=30.0)
        low_price = model.commission_for_notional(lot_size=0.1, fill_price=3000.0, contract_size=100.0)
        high_price = model.commission_for_notional(lot_size=0.1, fill_price=3600.0, contract_size=100.0)
        assert high_price > low_price

    def test_round_trip_commission_with_price_movement(self):
        model = CostModel(commission_per_million_usd=30.0)
        entry_commission = model.commission_for_notional(lot_size=0.17, fill_price=3601.2, contract_size=100.0)
        exit_commission = model.commission_for_notional(lot_size=0.17, fill_price=3619.8, contract_size=100.0)
        total = entry_commission + exit_commission
        assert total > 0
        assert entry_commission != pytest.approx(exit_commission)  # different fill prices -> different commission

    def test_zero_lot_size_rejected(self):
        model = CostModel(commission_per_million_usd=30.0)
        with pytest.raises(ValueError):
            model.commission_for_notional(lot_size=0, fill_price=3600.0, contract_size=100.0)

    def test_zero_fill_price_rejected(self):
        model = CostModel(commission_per_million_usd=30.0)
        with pytest.raises(ValueError):
            model.commission_for_notional(lot_size=0.1, fill_price=0, contract_size=100.0)

    def test_zero_contract_size_rejected(self):
        model = CostModel(commission_per_million_usd=30.0)
        with pytest.raises(ValueError):
            model.commission_for_notional(lot_size=0.1, fill_price=3600.0, contract_size=0)


class TestApplyEntrySpread:
    def test_long_entry_fills_above_close(self):
        price = apply_entry_spread(3600.0, Direction.LONG, spread_points=20, point_size=0.1)
        assert price == pytest.approx(3602.0)  # 20 * 0.1 = $2 worse

    def test_short_entry_fills_below_close(self):
        price = apply_entry_spread(3600.0, Direction.SHORT, spread_points=20, point_size=0.1)
        assert price == pytest.approx(3598.0)

    def test_zero_spread_no_change(self):
        price = apply_entry_spread(3600.0, Direction.LONG, spread_points=0, point_size=0.1)
        assert price == pytest.approx(3600.0)

    def test_negative_spread_rejected(self):
        with pytest.raises(ValueError):
            apply_entry_spread(3600.0, Direction.LONG, spread_points=-1, point_size=0.1)


class TestApplySlippage:
    def test_long_entry_slips_up(self):
        price = apply_slippage(3600.0, Direction.LONG, is_entry=True, slippage_amount=0.5)
        assert price == pytest.approx(3600.5)

    def test_long_exit_slips_down(self):
        # closing a long = selling -> adverse is downward
        price = apply_slippage(3600.0, Direction.LONG, is_entry=False, slippage_amount=0.5)
        assert price == pytest.approx(3599.5)

    def test_short_entry_slips_down(self):
        price = apply_slippage(3600.0, Direction.SHORT, is_entry=True, slippage_amount=0.5)
        assert price == pytest.approx(3599.5)

    def test_short_exit_slips_up(self):
        # closing a short = buying back -> adverse is upward
        price = apply_slippage(3600.0, Direction.SHORT, is_entry=False, slippage_amount=0.5)
        assert price == pytest.approx(3600.5)

    def test_zero_slippage_no_change(self):
        price = apply_slippage(3600.0, Direction.LONG, is_entry=True, slippage_amount=0.0)
        assert price == pytest.approx(3600.0)

    def test_negative_slippage_rejected(self):
        with pytest.raises(ValueError):
            apply_slippage(3600.0, Direction.LONG, is_entry=True, slippage_amount=-1)
