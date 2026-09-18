from datetime import datetime, timedelta

import pytest

from backtesting.cost_model import CostModel
from backtesting.slippage_model import FixedSlippageModel
from config.config_schema import BrokerConfig
from core.enums import Direction, ExitReason
from core.models import Candle, Signal, TradeRequest
from execution.execution_interface import BrokerConnectionError, OrderRejectedError, PositionNotFoundError
from execution.paper_executor import PaperExecutor


def _broker() -> BrokerConfig:
    return BrokerConfig()  # XM defaults: contract_size=100, digits=2, commission_per_million_usd=30


def _candle(price: float, spread_points=10.0, minute=0) -> Candle:
    ts = datetime(2026, 1, 1) + timedelta(minutes=minute)
    return Candle(timestamp=ts, open=price, high=price + 1, low=price - 1, close=price, volume=10, spread_points=spread_points)


def _trade_request(entry=3600.0, sl=3590.0, tp=3620.0, direction=Direction.LONG, lot=0.03) -> TradeRequest:
    signal = Signal(
        symbol="XAUUSD", direction=direction, entry_price=entry, stop_loss=sl, take_profit=tp,
        strategy_name="test",
    )
    return TradeRequest(signal=signal, lot_size=lot, risk_amount=30.0, risk_percent=0.003, required_margin=100.0)


def _executor() -> PaperExecutor:
    return PaperExecutor(
        broker=_broker(),
        cost_model=CostModel(commission_per_million_usd=30.0),
        slippage_model=FixedSlippageModel(price_amount=0.20),
    )


class TestPlaceOrder:
    def test_no_market_price_yet_raises_connection_error(self):
        executor = _executor()
        with pytest.raises(BrokerConnectionError):
            executor.place_order(_trade_request())

    def test_successful_fill_applies_spread_and_slippage(self):
        executor = _executor()
        executor.update_market_price(_candle(3600.0, spread_points=10.0))
        position = executor.place_order(_trade_request(entry=3600.0))
        # spread: 10 * 0.01 = 0.10; slippage: 0.20 -> entry = 3600 + 0.10 + 0.20
        assert position.entry_price == pytest.approx(3600.30)

    def test_duplicate_symbol_position_rejected(self):
        executor = _executor()
        executor.update_market_price(_candle(3600.0))
        executor.place_order(_trade_request())
        with pytest.raises(OrderRejectedError):
            executor.place_order(_trade_request())

    def test_short_order_fills_correctly(self):
        executor = _executor()
        executor.update_market_price(_candle(3600.0, spread_points=10.0))
        position = executor.place_order(
            _trade_request(entry=3600.0, sl=3610.0, tp=3580.0, direction=Direction.SHORT)
        )
        # short entry: spread pushes DOWN, slippage pushes DOWN too
        assert position.entry_price == pytest.approx(3600.0 - 0.10 - 0.20)

    def test_position_tracked_and_retrievable(self):
        executor = _executor()
        executor.update_market_price(_candle(3600.0))
        position = executor.place_order(_trade_request())
        assert executor.get_open_position("XAUUSD") is not None
        assert executor.get_open_position("XAUUSD").id == position.id

    def test_no_position_for_unknown_symbol(self):
        executor = _executor()
        assert executor.get_open_position("EURUSD") is None


class TestClosePosition:
    def test_close_computes_pnl_and_removes_from_tracking(self):
        executor = _executor()
        executor.update_market_price(_candle(3600.0, spread_points=10.0))
        position = executor.place_order(_trade_request(entry=3600.0, sl=3590.0, tp=3620.0))

        executor.update_market_price(_candle(3620.0, spread_points=10.0, minute=15))
        trade = executor.close_position(position, ExitReason.TAKE_PROFIT, 3620.0)

        assert trade.exit_reason == ExitReason.TAKE_PROFIT
        assert trade.pnl != 0
        assert executor.get_open_position("XAUUSD") is None

    def test_exit_slippage_is_adverse_for_long(self):
        executor = _executor()
        executor.update_market_price(_candle(3600.0, spread_points=0.0))
        position = executor.place_order(_trade_request(entry=3600.0))
        executor.update_market_price(_candle(3620.0, spread_points=0.0, minute=15))
        trade = executor.close_position(position, ExitReason.TAKE_PROFIT, 3620.0)
        # closing a long = selling -> slippage should reduce the exit price
        assert trade.exit_price == pytest.approx(3620.0 - 0.20)

    def test_close_unknown_position_raises(self):
        executor = _executor()
        executor.update_market_price(_candle(3600.0))
        position = executor.place_order(_trade_request())
        executor.update_market_price(_candle(3610.0, minute=15))
        executor.close_position(position, ExitReason.TAKE_PROFIT, 3610.0)
        # position is now closed -- closing it again must fail, not silently no-op
        with pytest.raises(PositionNotFoundError):
            executor.close_position(position, ExitReason.MANUAL, 3610.0)

    def test_no_market_price_raises_before_any_price_update(self):
        executor = _executor()
        request = _trade_request()
        with pytest.raises(BrokerConnectionError):
            executor.close_position(
                # constructing a Position directly since place_order requires a price first
                __import__("core.models", fromlist=["Position"]).Position(
                    symbol="XAUUSD", direction=Direction.LONG, lot_size=0.03,
                    entry_price=3600.0, stop_loss=3590.0, take_profit=3620.0,
                ),
                ExitReason.MANUAL,
                3600.0,
            )


class TestModifySlTp:
    def test_modify_updates_stop_loss(self):
        executor = _executor()
        executor.update_market_price(_candle(3600.0))
        position = executor.place_order(_trade_request(entry=3600.0, sl=3590.0, tp=3620.0))
        updated = executor.modify_sl_tp(position, new_stop_loss=3595.0)
        assert updated.stop_loss == pytest.approx(3595.0)
        assert updated.take_profit == pytest.approx(3620.0)  # unchanged

    def test_modify_updates_take_profit_only(self):
        executor = _executor()
        executor.update_market_price(_candle(3600.0))
        position = executor.place_order(_trade_request(entry=3600.0, sl=3590.0, tp=3620.0))
        updated = executor.modify_sl_tp(position, new_take_profit=3630.0)
        assert updated.take_profit == pytest.approx(3630.0)
        assert updated.stop_loss == pytest.approx(3590.0)

    def test_modify_unknown_position_raises(self):
        executor = _executor()
        executor.update_market_price(_candle(3600.0))
        position = executor.place_order(_trade_request())
        executor.update_market_price(_candle(3610.0, minute=15))
        executor.close_position(position, ExitReason.MANUAL, 3610.0)
        with pytest.raises(PositionNotFoundError):
            executor.modify_sl_tp(position, new_stop_loss=3595.0)


class TestIsConnected:
    def test_paper_executor_always_reports_connected(self):
        executor = _executor()
        assert executor.is_connected() is True
