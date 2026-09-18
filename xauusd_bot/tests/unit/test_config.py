"""
Config validation tests. The core property under test: bad config must
raise at construction time, never clamp silently.
"""

import pytest
from pydantic import ValidationError

from config.config_schema import BotConfig, BrokerConfig, FilterConfig, KillSwitchConfig, RiskConfig


def _base_kwargs(**overrides):
    kwargs = dict(
        environment="backtest",
        risk=RiskConfig(),
        filters=FilterConfig(max_spread_points=35),
        broker=BrokerConfig(),
        kill_switch=KillSwitchConfig(),
    )
    kwargs.update(overrides)
    return kwargs


class TestRiskConfigBounds:
    def test_default_risk_within_brief_range_is_valid(self):
        cfg = RiskConfig(default_risk_percent=0.004)
        assert cfg.default_risk_percent == 0.004

    def test_default_risk_below_minimum_rejected(self):
        with pytest.raises(ValidationError):
            RiskConfig(default_risk_percent=0.001)  # below 0.25%

    def test_default_risk_above_band_max_rejected(self):
        with pytest.raises(ValidationError):
            RiskConfig(default_risk_percent=0.006)  # above 0.5% band ceiling

    def test_max_risk_percent_cannot_exceed_one_percent(self):
        with pytest.raises(ValidationError):
            RiskConfig(max_risk_percent=0.011)

    def test_max_risk_percent_at_exactly_one_percent_is_valid(self):
        cfg = RiskConfig(max_risk_percent=0.01)
        assert cfg.max_risk_percent == 0.01

    def test_default_exceeding_max_rejected(self):
        with pytest.raises(ValidationError):
            RiskConfig(default_risk_percent=0.005, max_risk_percent=0.004)

    def test_max_open_positions_locked_to_one(self):
        with pytest.raises(ValidationError):
            RiskConfig(max_open_positions=2)

    def test_daily_loss_limit_default_is_two_percent(self):
        assert RiskConfig().daily_loss_limit_percent == 0.02

    def test_max_drawdown_default_is_ten_percent(self):
        assert RiskConfig().max_drawdown_percent == 0.10

    def test_negative_daily_loss_limit_rejected(self):
        with pytest.raises(ValidationError):
            RiskConfig(daily_loss_limit_percent=-0.01)

    def test_min_lot_risk_tolerance_defaults_to_strict_zero(self):
        assert RiskConfig().min_lot_risk_tolerance == 0.0

    def test_min_lot_risk_tolerance_excessive_value_rejected(self):
        with pytest.raises(ValidationError):
            RiskConfig(min_lot_risk_tolerance=0.05)


class TestBrokerConfig:
    def test_defaults_match_xm_ctrader_spec(self):
        cfg = BrokerConfig()
        assert cfg.symbol == "XAUUSD"
        assert cfg.contract_size == 100.0
        assert cfg.digits == 2
        assert cfg.min_lot == 0.01
        assert cfg.max_lot == 100.0
        assert cfg.lot_step == 0.01
        assert cfg.commission_per_million_usd == 30.0
        assert cfg.account_currency_unit == "usd"
        assert cfg.unit_scale_factor == 1.0

    def test_swap_not_assumed_free_by_default(self):
        """Unlike the previous (confirmed-Islamic) broker, swap status on
        this account is unconfirmed, so swap must default OFF rather than
        silently assumed free — but the real point values must still be
        present so enabling it later is a one-line config change."""
        cfg = BrokerConfig()
        assert cfg.swap_enabled is False
        assert cfg.swap_long_points == pytest.approx(-58.6)
        assert cfg.swap_short_points == pytest.approx(40.9)
        assert cfg.triple_swap_weekday == 2  # Wednesday
        assert cfg.weekend_swap_disabled is True

    def test_point_size_derived_from_digits(self):
        cfg = BrokerConfig(digits=2)
        assert cfg.point_size == pytest.approx(0.01)

    def test_min_lot_above_max_lot_rejected(self):
        with pytest.raises(ValidationError):
            BrokerConfig(min_lot=10.0, max_lot=1.0)

    def test_cents_unit_with_scale_factor_one_rejected(self):
        with pytest.raises(ValidationError):
            BrokerConfig(account_currency_unit="cents", unit_scale_factor=1.0)

    def test_leverage_above_cap_rejected(self):
        with pytest.raises(ValidationError):
            BrokerConfig(leverage=200)


class TestBotConfig:
    def test_valid_config_constructs(self):
        cfg = BotConfig(**_base_kwargs())
        assert cfg.environment == "backtest"

    def test_unknown_field_rejected(self):
        kwargs = _base_kwargs()
        with pytest.raises(ValidationError):
            BotConfig(**kwargs, some_unexpected_field=True)

    def test_live_environment_requires_positive_spread_filter(self):
        kwargs = _base_kwargs(
            environment="live",
            filters=FilterConfig.model_construct(
                max_spread_points=0, duplicate_order_debounce_seconds=5, allowed_sessions=None
            ),
        )
        # FilterConfig itself would reject max_spread_points<=0 via Field(gt=0);
        # model_construct bypasses that to specifically exercise BotConfig's
        # own live-environment guard rail.
        with pytest.raises(ValidationError):
            BotConfig(**kwargs)

    def test_kill_switch_manual_trigger_disabled_by_default(self):
        cfg = BotConfig(**_base_kwargs())
        assert cfg.kill_switch.manual_trigger_enabled is False
        assert cfg.kill_switch.trigger_on_max_drawdown is True
        assert cfg.kill_switch.trigger_on_broker_disconnect is True
