"""
Risk gate integration tests. Each test exercises the FULL pipeline
(account halts -> sizing -> margin) exactly as it will be called from
the state machine in a later phase.
"""

from datetime import date, datetime, timezone

import pytest

from config.config_schema import BotConfig, BrokerConfig, FilterConfig, KillSwitchConfig, RiskConfig
from core.enums import Direction, RejectReason
from core.models import AccountState, RejectedSignal, Signal, TradeRequest
from risk_engine.risk_gate import evaluate_signal
from risk_engine.risk_limits import ConsecutiveLossTracker


def _config(**risk_overrides) -> BotConfig:
    return BotConfig(
        environment="backtest",
        risk=RiskConfig(**risk_overrides),
        filters=FilterConfig(max_spread_points=35),
        broker=BrokerConfig(),
        kill_switch=KillSwitchConfig(),
    )


def _account(**overrides) -> AccountState:
    kwargs = dict(
        equity=10_000.0,
        balance=10_000.0,
        free_margin=9_000.0,
        used_margin=1_000.0,
        open_positions_count=0,
        daily_start_equity=10_000.0,
        peak_equity=10_000.0,
        consecutive_losses=0,
        trading_day=date.today(),
    )
    kwargs.update(overrides)
    return AccountState(**kwargs)


def _signal(**overrides) -> Signal:
    kwargs = dict(
        symbol="XAUUSD",
        direction=Direction.LONG,
        entry_price=3600.0,
        stop_loss=3590.0,
        strategy_name="test_strategy",
    )
    kwargs.update(overrides)
    return Signal(**kwargs)


def _tracker(config: BotConfig) -> ConsecutiveLossTracker:
    return ConsecutiveLossTracker.from_config(config.risk)


class TestEvaluateSignalHappyPath:
    def test_valid_signal_produces_trade_request(self):
        config = _config()
        result = evaluate_signal(_signal(), _account(), config, _tracker(config))
        assert isinstance(result, TradeRequest)
        assert result.lot_size > 0
        assert result.risk_percent <= config.risk.max_risk_percent

    def test_explicit_risk_percent_override_respected(self):
        config = _config()
        result = evaluate_signal(
            _signal(), _account(), config, _tracker(config), requested_risk_percent=0.005
        )
        assert isinstance(result, TradeRequest)
        assert result.risk_percent <= 0.005 + 1e-9


class TestEvaluateSignalAccountHalts:
    def test_max_open_positions_blocks_new_signal(self):
        config = _config()
        account = _account(open_positions_count=1)
        result = evaluate_signal(_signal(), account, config, _tracker(config))
        assert isinstance(result, RejectedSignal)
        assert result.reason == RejectReason.POSITION_ALREADY_OPEN

    def test_daily_loss_limit_blocks_new_signal(self):
        config = _config()
        account = _account(equity=9_700.0, daily_start_equity=10_000.0)  # 3% daily loss
        result = evaluate_signal(_signal(), account, config, _tracker(config))
        assert isinstance(result, RejectedSignal)
        assert result.reason == RejectReason.DAILY_LOSS_LIMIT_HIT

    def test_max_drawdown_blocks_new_signal(self):
        config = _config()
        # daily_start_equity == equity so this ISN'T also a daily-loss
        # breach — isolates the drawdown check specifically.
        account = _account(equity=8_800.0, daily_start_equity=8_800.0, peak_equity=10_000.0)
        result = evaluate_signal(_signal(), account, config, _tracker(config))
        assert isinstance(result, RejectedSignal)
        assert result.reason == RejectReason.MAX_DRAWDOWN_HIT

    def test_consecutive_loss_cooldown_blocks_new_signal(self):
        config = _config(max_consecutive_losses=2, consecutive_loss_cooldown_hours=24)
        tracker = _tracker(config)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        tracker.record_result(is_win=False, at=now)
        tracker.record_result(is_win=False, at=now)
        result = evaluate_signal(_signal(), _account(), config, tracker, now=now)
        assert isinstance(result, RejectedSignal)
        assert result.reason == RejectReason.CONSECUTIVE_LOSS_COOLDOWN_ACTIVE

    def test_halts_checked_before_sizing_even_with_bad_signal_economics(self):
        """An account that's already halted should reject on the halt
        reason even if the signal would ALSO fail sizing — halts take
        priority, proving check order (§ risk_gate docstring)."""
        config = _config()
        account = _account(equity=8_800.0, daily_start_equity=8_800.0, peak_equity=10_000.0)  # drawdown breach only
        # tiny equity + wide stop would also fail sizing, but drawdown
        # should be the reported reason since it's checked first
        result = evaluate_signal(
            _signal(stop_loss=3000.0), account, config, _tracker(config)
        )
        assert result.reason == RejectReason.MAX_DRAWDOWN_HIT


class TestEvaluateSignalSizingAndMargin:
    def test_min_lot_exceeds_risk_propagates_as_rejection(self):
        config = _config()
        # daily_start_equity == equity and peak_equity == equity so this
        # trade fails on SIZING, not on an account-level halt.
        account = _account(equity=200.0, daily_start_equity=200.0, peak_equity=200.0, free_margin=180.0)
        result = evaluate_signal(
            _signal(stop_loss=3500.0), account, config, _tracker(config)
        )
        assert isinstance(result, RejectedSignal)
        assert result.reason == RejectReason.MIN_LOT_EXCEEDS_RISK

    def test_insufficient_margin_rejects_even_with_valid_sizing(self):
        config = _config()
        # Plenty of equity for sizing, but almost no free margin.
        account = _account(equity=10_000.0, free_margin=1.0)
        result = evaluate_signal(_signal(), account, config, _tracker(config))
        assert isinstance(result, RejectedSignal)
        assert result.reason == RejectReason.MARGIN_INSUFFICIENT

    def test_rejected_signal_retains_original_signal_reference(self):
        config = _config()
        account = _account(open_positions_count=1)
        signal = _signal()
        result = evaluate_signal(signal, account, config, _tracker(config))
        assert result.signal is signal
