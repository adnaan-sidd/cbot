from datetime import date

import pytest

from config_schema import KillSwitchConfig, RiskConfig
from core_models import AccountState
from position_monitor import KillSwitch


def _account(**overrides) -> AccountState:
    kwargs = dict(
        equity=10_000.0, balance=10_000.0, free_margin=9_000.0, used_margin=1_000.0,
        open_positions_count=0, daily_start_equity=10_000.0, peak_equity=10_000.0,
        consecutive_losses=0, trading_day=date.today(),
    )
    kwargs.update(overrides)
    return AccountState(**kwargs)


def _switch(**overrides) -> KillSwitch:
    risk_kwargs = dict()
    config_kwargs = dict(trigger_on_max_drawdown=True, trigger_on_broker_disconnect=True)
    config_kwargs.update(overrides)
    return KillSwitch(risk=RiskConfig(**risk_kwargs), config=KillSwitchConfig(**config_kwargs))


class TestArmedByDefault:
    def test_starts_armed_with_no_trigger_reason(self):
        switch = _switch()
        assert switch.armed is True
        assert switch.triggered_reason is None

    def test_normal_conditions_do_not_trigger(self):
        switch = _switch()
        result = switch.check(_account(), broker_connected=True)
        assert result is False
        assert switch.armed is True


class TestDrawdownTrigger:
    def test_below_threshold_does_not_trigger(self):
        switch = _switch()
        account = _account(equity=9_500.0, peak_equity=10_000.0)  # 5% drawdown
        assert switch.check(account, broker_connected=True) is False

    def test_at_threshold_triggers(self):
        switch = _switch()
        account = _account(equity=9_000.0, peak_equity=10_000.0)  # exactly 10%
        result = switch.check(account, broker_connected=True)
        assert result is True
        assert switch.armed is False
        assert "drawdown" in switch.triggered_reason.lower()

    def test_disabled_trigger_never_fires_on_drawdown(self):
        switch = _switch(trigger_on_max_drawdown=False)
        account = _account(equity=8_000.0, peak_equity=10_000.0)  # 20% drawdown
        result = switch.check(account, broker_connected=True)
        assert result is False
        assert switch.armed is True


class TestBrokerDisconnectTrigger:
    def test_disconnect_triggers(self):
        switch = _switch()
        result = switch.check(_account(), broker_connected=False)
        assert result is True
        assert switch.armed is False
        assert "connection" in switch.triggered_reason.lower()

    def test_disabled_trigger_never_fires_on_disconnect(self):
        switch = _switch(trigger_on_broker_disconnect=False)
        result = switch.check(_account(), broker_connected=False)
        assert result is False
        assert switch.armed is True


class TestStaysTrippedUntilRearmed:
    def test_remains_triggered_on_subsequent_calls_even_if_conditions_recover(self):
        switch = _switch()
        account_bad = _account(equity=9_000.0, peak_equity=10_000.0)
        switch.check(account_bad, broker_connected=True)
        assert switch.armed is False

        # conditions recover fully -- should NOT auto-clear
        account_good = _account(equity=10_000.0, peak_equity=10_000.0)
        result = switch.check(account_good, broker_connected=True)
        assert result is True
        assert switch.armed is False

    def test_rearm_clears_triggered_state(self):
        switch = _switch()
        switch.check(_account(equity=9_000.0, peak_equity=10_000.0), broker_connected=True)
        assert switch.armed is False
        switch.rearm()
        assert switch.armed is True
        assert switch.triggered_reason is None

    def test_after_rearm_normal_conditions_do_not_retrigger(self):
        switch = _switch()
        switch.check(_account(equity=9_000.0, peak_equity=10_000.0), broker_connected=True)
        switch.rearm()
        result = switch.check(_account(), broker_connected=True)
        assert result is False
