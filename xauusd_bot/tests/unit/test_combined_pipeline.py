"""
Combined pipeline test: filter_chain + risk_gate together, in the order
the (not-yet-built) state machine will call them — filters first (cheap,
market-condition checks), then the risk gate (sizing/margin/account
halts). Neither module imports the other; this test is what proves they
actually compose correctly as two independent, injectable stages.
"""

from datetime import date, datetime, timezone

from config.config_schema import BotConfig, BrokerConfig, FilterConfig, KillSwitchConfig, RiskConfig
from core.enums import Direction, RejectReason
from core.models import AccountState, RejectedSignal, Signal, TradeRequest
from risk_engine.risk_gate import evaluate_signal
from risk_engine.risk_limits import ConsecutiveLossTracker
from trade_filters.duplicate_order_guard import DuplicateOrderGuard
from trade_filters.filter_chain import run_filter_chain


def _config() -> BotConfig:
    return BotConfig(
        environment="backtest",
        risk=RiskConfig(),
        filters=FilterConfig(max_spread_points=35, allowed_sessions=None),
        broker=BrokerConfig(),
        kill_switch=KillSwitchConfig(),
    )


def _account() -> AccountState:
    return AccountState(
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


def evaluate_full_pipeline(signal, account, config, tracker, guard, spread, now):
    """Mirrors the sequencing the eventual state machine's RISK_CHECK
    state will use: filters first, then risk gate. Kept local to this
    test file (not part of production code) since the real orchestrator
    doesn't exist until a later phase."""
    filter_result = run_filter_chain(
        signal=signal,
        current_spread_points=spread,
        now=now,
        filters=config.filters,
        duplicate_guard=guard,
    )
    if not filter_result.passed:
        return RejectedSignal(
            signal=signal, reason=filter_result.reason, detail=filter_result.detail, timestamp=now
        )
    return evaluate_signal(signal, account, config, tracker, now=now)


class TestCombinedPipeline:
    def test_valid_signal_passes_both_stages(self):
        config = _config()
        result = evaluate_full_pipeline(
            _signal(),
            _account(),
            config,
            ConsecutiveLossTracker.from_config(config.risk),
            DuplicateOrderGuard(debounce_seconds=5),
            spread=20.0,
            now=datetime.now(timezone.utc),
        )
        assert isinstance(result, TradeRequest)

    def test_wide_spread_blocks_before_risk_gate_even_runs(self):
        config = _config()
        result = evaluate_full_pipeline(
            _signal(),
            _account(),
            config,
            ConsecutiveLossTracker.from_config(config.risk),
            DuplicateOrderGuard(debounce_seconds=5),
            spread=999.0,  # absurdly wide
            now=datetime.now(timezone.utc),
        )
        assert isinstance(result, RejectedSignal)
        assert result.reason == RejectReason.SPREAD_TOO_WIDE

    def test_filters_pass_but_risk_gate_rejects_on_account_halt(self):
        config = _config()
        halted_account = AccountState(
            equity=8_800.0,
            balance=8_800.0,
            free_margin=8_000.0,
            used_margin=800.0,
            open_positions_count=0,
            daily_start_equity=8_800.0,
            peak_equity=10_000.0,  # 12% drawdown
            consecutive_losses=0,
            trading_day=date.today(),
        )
        result = evaluate_full_pipeline(
            _signal(),
            halted_account,
            config,
            ConsecutiveLossTracker.from_config(config.risk),
            DuplicateOrderGuard(debounce_seconds=5),
            spread=20.0,
            now=datetime.now(timezone.utc),
        )
        assert isinstance(result, RejectedSignal)
        assert result.reason == RejectReason.MAX_DRAWDOWN_HIT

    def test_duplicate_signal_blocked_by_filter_before_reaching_risk_gate(self):
        config = _config()
        guard = DuplicateOrderGuard(debounce_seconds=5)
        now = datetime.now(timezone.utc)
        signal = _signal()
        guard.record(signal, now=now)
        result = evaluate_full_pipeline(
            signal,
            _account(),
            config,
            ConsecutiveLossTracker.from_config(config.risk),
            guard,
            spread=20.0,
            now=now,
        )
        assert isinstance(result, RejectedSignal)
        assert result.reason == RejectReason.DUPLICATE_ORDER
