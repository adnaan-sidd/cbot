"""
Tests for the real (not placeholder) cTrader executor and feed.

Two categories, deliberately kept separate:
  1. Local bookkeeping logic (tick-to-candle aggregation, not-connected
     error paths) — fully testable and tested here without any network
     access.
  2. Actual connect() behavior — this environment has no network route
     to cTrader's servers, so these tests can only confirm FAIL-CLOSED
     behavior (a connection attempt that can't succeed must raise, never
     silently report success) using a short timeout. They do NOT verify
     protocol correctness against a real server — see ctrader_executor.py
     and ctrader_feed.py's module docstrings for what still needs your
     own demo-account testing.
"""

from datetime import datetime, timezone

import pytest

from config.config_schema import BrokerConfig
from core.enums import Direction
from core.models import Signal, TradeRequest
from execution.ctrader_executor import CTraderExecutor
from execution.execution_interface import BrokerConnectionError
from market_data.ctrader_feed import CTraderFeed


def _trade_request() -> TradeRequest:
    signal = Signal(
        symbol="XAUUSD", direction=Direction.LONG, entry_price=3600.0,
        stop_loss=3590.0, take_profit=3620.0, strategy_name="test",
    )
    return TradeRequest(signal=signal, lot_size=0.03, risk_amount=30.0, risk_percent=0.003, required_margin=100.0)


def _executor(**overrides) -> CTraderExecutor:
    kwargs = dict(
        broker=BrokerConfig(), client_id="test", client_secret="test", access_token="test",
        ctid_trader_account_id=1, symbol_id=1, connect_timeout_seconds=0.5,
    )
    kwargs.update(overrides)
    return CTraderExecutor(**kwargs)


def _feed(**overrides) -> CTraderFeed:
    kwargs = dict(
        client_id="test", client_secret="test", access_token="test",
        ctid_trader_account_id=1, symbol_id=1, digits=2, connect_timeout_seconds=0.5,
    )
    kwargs.update(overrides)
    return CTraderFeed(**kwargs)


class TestCTraderFeedTickAggregation:
    """Pure local logic -- no network involved."""

    def test_first_tick_starts_a_bucket_without_emitting(self):
        feed = _feed(timeframe_minutes=15)
        feed._on_tick(datetime(2026, 1, 1, 10, 5, tzinfo=timezone.utc), 3600.0)
        assert feed._queue.empty()

    def test_ticks_within_same_bucket_do_not_emit(self):
        feed = _feed(timeframe_minutes=15)
        feed._on_tick(datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc), 3600.0)
        feed._on_tick(datetime(2026, 1, 1, 10, 5, tzinfo=timezone.utc), 3601.0)
        feed._on_tick(datetime(2026, 1, 1, 10, 10, tzinfo=timezone.utc), 3599.0)
        assert feed._queue.empty()

    def test_tick_in_next_bucket_emits_the_completed_one(self):
        feed = _feed(timeframe_minutes=15)
        feed._on_tick(datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc), 3600.0)
        feed._on_tick(datetime(2026, 1, 1, 10, 5, tzinfo=timezone.utc), 3605.0)
        feed._on_tick(datetime(2026, 1, 1, 10, 10, tzinfo=timezone.utc), 3595.0)
        feed._on_tick(datetime(2026, 1, 1, 10, 16, tzinfo=timezone.utc), 3602.0)  # next bucket

        assert not feed._queue.empty()
        candle = feed._queue.get()
        assert candle.timestamp == datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
        assert candle.open == pytest.approx(3600.0)
        assert candle.high == pytest.approx(3605.0)
        assert candle.low == pytest.approx(3595.0)
        assert candle.close == pytest.approx(3595.0)

    def test_buckets_align_to_clock_not_stream_start(self):
        feed = _feed(timeframe_minutes=60)
        feed._on_tick(datetime(2026, 1, 1, 10, 37, tzinfo=timezone.utc), 3600.0)
        feed._on_tick(datetime(2026, 1, 1, 11, 2, tzinfo=timezone.utc), 3610.0)
        candle = feed._queue.get()
        assert candle.timestamp == datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)

    def test_generator_stops_on_none_sentinel(self):
        feed = _feed(timeframe_minutes=15)
        feed._on_tick(datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc), 3600.0)
        feed._on_tick(datetime(2026, 1, 1, 10, 16, tzinfo=timezone.utc), 3605.0)
        feed._queue.put(None)

        candles = list(feed.candles())
        assert len(candles) == 1

    def test_not_connected_by_default(self):
        feed = _feed()
        assert feed.is_connected() is False


class TestCTraderFeedConnectFailsClosed:
    def test_connect_raises_rather_than_silently_succeeding(self):
        """No network route to cTrader's servers exists in this
        environment -- connect() must raise, never report success it
        didn't actually achieve."""
        feed = _feed(connect_timeout_seconds=0.5)
        with pytest.raises(ConnectionError):
            feed.connect()
        assert feed.is_connected() is False


class TestCTraderExecutorNotConnectedPaths:
    def test_not_connected_by_default(self):
        assert _executor().is_connected() is False

    def test_place_order_raises_when_not_connected(self):
        with pytest.raises(BrokerConnectionError):
            _executor().place_order(_trade_request())

    def test_close_position_raises_when_not_connected(self):
        from core.models import Position

        position = Position(
            symbol="XAUUSD", direction=Direction.LONG, lot_size=0.03,
            entry_price=3600.0, stop_loss=3590.0, take_profit=3620.0,
        )
        with pytest.raises(BrokerConnectionError):
            from core.enums import ExitReason

            _executor().close_position(position, ExitReason.MANUAL, 3600.0)

    def test_modify_sl_tp_raises_when_not_connected(self):
        from core.models import Position

        position = Position(
            symbol="XAUUSD", direction=Direction.LONG, lot_size=0.03,
            entry_price=3600.0, stop_loss=3590.0, take_profit=3620.0,
        )
        with pytest.raises(BrokerConnectionError):
            _executor().modify_sl_tp(position, new_stop_loss=3595.0)


class TestCTraderExecutorConnectFailsClosed:
    def test_connect_raises_rather_than_silently_succeeding(self):
        executor = _executor(connect_timeout_seconds=0.5)
        with pytest.raises(BrokerConnectionError):
            executor.connect()
        assert executor.is_connected() is False


class TestCTraderExecutorDuplicateGuard:
    def test_place_order_rejects_duplicate_symbol_even_if_somehow_connected(self):
        """Defensive check: even bypassing the connection gate to
        simulate an already-open position being tracked, a second order
        for the same symbol must still be rejected -- this doesn't rely
        on a real connection to verify."""
        executor = _executor()
        executor._connected = True
        from core.models import Position

        executor._open_positions["XAUUSD"] = Position(
            symbol="XAUUSD", direction=Direction.LONG, lot_size=0.03,
            entry_price=3600.0, stop_loss=3590.0, take_profit=3620.0,
        )
        from execution.execution_interface import OrderRejectedError

        with pytest.raises(OrderRejectedError):
            executor.place_order(_trade_request())
