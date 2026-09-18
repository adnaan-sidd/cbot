from datetime import datetime, timedelta

import pytest

from backtesting.slippage_model import FixedSlippageModel
from backtesting.stress_tests import (
    COMBINED_STRESS,
    GAP_THROUGH_STOP,
    SLIPPAGE_SHOCK,
    SPREAD_BLOWOUT,
    GapRiskSlippageModel,
    ScaledSlippageModel,
    SpreadMultipliedFeed,
    StressScenario,
    apply_stress_scenario,
)
from core.models import Candle
from market_data.historical_feed import HistoricalFeed


def _candle(i: int, spread_points=10.0) -> Candle:
    ts = datetime(2026, 1, 1) + timedelta(minutes=i)
    return Candle(
        timestamp=ts, open=3600, high=3605, low=3595, close=3600, volume=10,
        spread_points=spread_points,
    )


class TestSpreadMultipliedFeed:
    def test_spread_is_multiplied(self):
        base = HistoricalFeed([_candle(i, spread_points=10.0) for i in range(3)])
        stressed = SpreadMultipliedFeed(base, multiplier=5.0)
        candles = list(stressed.candles())
        assert all(c.spread_points == pytest.approx(50.0) for c in candles)

    def test_none_spread_stays_none(self):
        base = HistoricalFeed([_candle(0, spread_points=None)])
        stressed = SpreadMultipliedFeed(base, multiplier=5.0)
        candles = list(stressed.candles())
        assert candles[0].spread_points is None

    def test_other_fields_unchanged(self):
        base = HistoricalFeed([_candle(0)])
        stressed = SpreadMultipliedFeed(base, multiplier=2.0)
        c = list(stressed.candles())[0]
        assert c.close == 3600
        assert c.high == 3605

    def test_multiplier_below_one_rejected(self):
        base = HistoricalFeed([_candle(0)])
        with pytest.raises(ValueError):
            SpreadMultipliedFeed(base, multiplier=0.5)


class TestScaledSlippageModel:
    def test_multiplies_base_slippage(self):
        base = FixedSlippageModel(price_amount=0.5)
        scaled = ScaledSlippageModel(base, multiplier=10.0)
        assert scaled.get_slippage_price(_candle(0)) == pytest.approx(5.0)

    def test_multiplier_below_one_rejected(self):
        base = FixedSlippageModel(price_amount=0.5)
        with pytest.raises(ValueError):
            ScaledSlippageModel(base, multiplier=0.9)


class TestGapRiskSlippageModel:
    def test_never_gaps_with_zero_probability(self):
        base = FixedSlippageModel(price_amount=0.5)
        model = GapRiskSlippageModel(base, gap_probability=0.0, gap_size=100.0, seed=1)
        for _ in range(50):
            assert model.get_slippage_price(_candle(0)) == pytest.approx(0.5)

    def test_always_gaps_with_probability_one(self):
        base = FixedSlippageModel(price_amount=0.5)
        model = GapRiskSlippageModel(base, gap_probability=1.0, gap_size=100.0, seed=1)
        for _ in range(20):
            assert model.get_slippage_price(_candle(0)) == pytest.approx(100.5)

    def test_deterministic_with_seed(self):
        base = FixedSlippageModel(price_amount=0.5)
        model_a = GapRiskSlippageModel(base, gap_probability=0.3, gap_size=20.0, seed=99)
        model_b = GapRiskSlippageModel(base, gap_probability=0.3, gap_size=20.0, seed=99)
        results_a = [model_a.get_slippage_price(_candle(0)) for _ in range(30)]
        results_b = [model_b.get_slippage_price(_candle(0)) for _ in range(30)]
        assert results_a == results_b

    def test_invalid_probability_rejected(self):
        base = FixedSlippageModel(price_amount=0.5)
        with pytest.raises(ValueError):
            GapRiskSlippageModel(base, gap_probability=1.5, gap_size=10.0)


class TestApplyStressScenario:
    def test_baseline_scenario_leaves_inputs_unchanged(self):
        base_feed = HistoricalFeed([_candle(0)])
        base_slippage = FixedSlippageModel(price_amount=0.5)
        scenario = StressScenario(name="baseline")
        feed, slippage = apply_stress_scenario(base_feed, base_slippage, scenario)
        assert feed is base_feed
        assert slippage is base_slippage

    def test_spread_blowout_wraps_feed_only(self):
        base_feed = HistoricalFeed([_candle(0, spread_points=10.0)])
        base_slippage = FixedSlippageModel(price_amount=0.5)
        feed, slippage = apply_stress_scenario(base_feed, base_slippage, SPREAD_BLOWOUT)
        assert isinstance(feed, SpreadMultipliedFeed)
        assert slippage is base_slippage
        assert list(feed.candles())[0].spread_points == pytest.approx(50.0)

    def test_slippage_shock_wraps_slippage_only(self):
        base_feed = HistoricalFeed([_candle(0)])
        base_slippage = FixedSlippageModel(price_amount=0.5)
        feed, slippage = apply_stress_scenario(base_feed, base_slippage, SLIPPAGE_SHOCK)
        assert feed is base_feed
        assert slippage.get_slippage_price(_candle(0)) == pytest.approx(5.0)

    def test_gap_through_stop_produces_occasional_large_slippage(self):
        base_feed = HistoricalFeed([_candle(0)])
        base_slippage = FixedSlippageModel(price_amount=0.5)
        feed, slippage = apply_stress_scenario(
            base_feed, base_slippage, GAP_THROUGH_STOP, seed=1
        )
        results = [slippage.get_slippage_price(_candle(0)) for _ in range(200)]
        assert any(r > 10.0 for r in results)  # at least one gap occurred
        assert any(r == pytest.approx(0.5) for r in results)  # and not every fill gapped

    def test_combined_stress_wraps_both(self):
        base_feed = HistoricalFeed([_candle(0, spread_points=10.0)])
        base_slippage = FixedSlippageModel(price_amount=0.5)
        feed, slippage = apply_stress_scenario(
            base_feed, base_slippage, COMBINED_STRESS, seed=1
        )
        assert list(feed.candles())[0].spread_points == pytest.approx(30.0)
        assert isinstance(slippage, GapRiskSlippageModel)
        assert isinstance(slippage.base_model, ScaledSlippageModel)
