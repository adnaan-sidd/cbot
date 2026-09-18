"""
Model invariant tests. The core property under test: it must be
IMPOSSIBLE to construct a Signal/Position without a valid stop-loss,
and impossible to construct one with self-contradictory prices.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from core.enums import Direction, ExitReason
from core.models import AccountState, Candle, Position, Signal, Trade, TradeRequest


class TestCandle:
    def test_valid_candle_constructs(self):
        c = Candle(datetime.now(timezone.utc), open=100, high=105, low=95, close=102, volume=10)
        assert c.high == 105

    def test_high_below_low_rejected(self):
        with pytest.raises(ValueError):
            Candle(datetime.now(timezone.utc), open=100, high=90, low=95, close=92, volume=10)

    def test_open_outside_range_rejected(self):
        with pytest.raises(ValueError):
            Candle(datetime.now(timezone.utc), open=999, high=105, low=95, close=100, volume=10)


class TestSignal:
    def test_valid_long_signal_constructs(self):
        s = Signal(
            symbol="XAUUSD",
            direction=Direction.LONG,
            entry_price=3600.0,
            stop_loss=3590.0,
            strategy_name="test_strategy",
        )
        assert s.stop_loss_distance == pytest.approx(10.0)

    def test_signal_without_stop_loss_rejected(self):
        with pytest.raises(TypeError):
            # stop_loss has no default — omitting it is a TypeError,
            # not a constructible "None" state.
            Signal(
                symbol="XAUUSD",
                direction=Direction.LONG,
                entry_price=3600.0,
                strategy_name="test_strategy",
            )

    def test_signal_with_zero_stop_loss_rejected(self):
        with pytest.raises(ValueError):
            Signal(
                symbol="XAUUSD",
                direction=Direction.LONG,
                entry_price=3600.0,
                stop_loss=0,
                strategy_name="test_strategy",
            )

    def test_long_signal_stop_loss_above_entry_rejected(self):
        with pytest.raises(ValueError):
            Signal(
                symbol="XAUUSD",
                direction=Direction.LONG,
                entry_price=3600.0,
                stop_loss=3610.0,  # wrong side for a long
                strategy_name="test_strategy",
            )

    def test_short_signal_stop_loss_below_entry_rejected(self):
        with pytest.raises(ValueError):
            Signal(
                symbol="XAUUSD",
                direction=Direction.SHORT,
                entry_price=3600.0,
                stop_loss=3590.0,  # wrong side for a short
                strategy_name="test_strategy",
            )

    def test_long_signal_take_profit_below_entry_rejected(self):
        with pytest.raises(ValueError):
            Signal(
                symbol="XAUUSD",
                direction=Direction.LONG,
                entry_price=3600.0,
                stop_loss=3590.0,
                take_profit=3580.0,  # wrong side for a long
                strategy_name="test_strategy",
            )

    def test_negative_entry_price_rejected(self):
        with pytest.raises(ValueError):
            Signal(
                symbol="XAUUSD",
                direction=Direction.LONG,
                entry_price=-1,
                stop_loss=10,
                strategy_name="test_strategy",
            )


class TestTradeRequest:
    def _signal(self):
        return Signal(
            symbol="XAUUSD",
            direction=Direction.LONG,
            entry_price=3600.0,
            stop_loss=3590.0,
            strategy_name="test_strategy",
        )

    def test_valid_trade_request_constructs(self):
        tr = TradeRequest(
            signal=self._signal(),
            lot_size=0.02,
            risk_amount=20.0,
            risk_percent=0.004,
            required_margin=72.0,
        )
        assert tr.lot_size == 0.02

    def test_zero_lot_size_rejected(self):
        with pytest.raises(ValueError):
            TradeRequest(
                signal=self._signal(),
                lot_size=0,
                risk_amount=20.0,
                risk_percent=0.004,
                required_margin=72.0,
            )


class TestPosition:
    def test_position_without_stop_loss_rejected(self):
        with pytest.raises(ValueError):
            Position(
                symbol="XAUUSD",
                direction=Direction.LONG,
                lot_size=0.02,
                entry_price=3600.0,
                stop_loss=0,
                take_profit=None,
            )


class TestTrade:
    def test_closed_before_opened_rejected(self):
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError):
            Trade(
                symbol="XAUUSD",
                direction=Direction.LONG,
                lot_size=0.02,
                entry_price=3600.0,
                exit_price=3610.0,
                stop_loss=3590.0,
                take_profit=None,
                opened_at=now,
                closed_at=now - timedelta(minutes=5),
                pnl=20.0,
                pnl_percent=0.5,
                exit_reason=ExitReason.TAKE_PROFIT,
                commission=0.07,
                slippage_points=0.1,
            )

    def test_is_win_property(self):
        now = datetime.now(timezone.utc)
        t = Trade(
            symbol="XAUUSD",
            direction=Direction.LONG,
            lot_size=0.02,
            entry_price=3600.0,
            exit_price=3590.0,
            stop_loss=3590.0,
            take_profit=None,
            opened_at=now,
            closed_at=now + timedelta(minutes=5),
            pnl=-20.0,
            pnl_percent=-0.5,
            exit_reason=ExitReason.STOP_LOSS,
            commission=0.07,
            slippage_points=0.1,
        )
        assert t.is_win is False


class TestAccountState:
    def _account(self, **overrides):
        kwargs = dict(
            equity=10000.0,
            balance=10000.0,
            free_margin=9000.0,
            used_margin=1000.0,
            open_positions_count=0,
            daily_start_equity=10000.0,
            peak_equity=10500.0,
            consecutive_losses=0,
            trading_day=date.today(),
        )
        kwargs.update(overrides)
        return AccountState(**kwargs)

    def test_negative_equity_rejected(self):
        with pytest.raises(ValueError):
            self._account(equity=-1)

    def test_drawdown_percent_computed_correctly(self):
        acc = self._account(equity=9450.0, peak_equity=10500.0)
        assert acc.drawdown_percent == pytest.approx(0.10, rel=1e-3)

    def test_drawdown_percent_zero_when_at_peak(self):
        acc = self._account(equity=10500.0, peak_equity=10500.0)
        assert acc.drawdown_percent == 0.0

    def test_daily_loss_percent_computed_correctly(self):
        acc = self._account(equity=9800.0, daily_start_equity=10000.0)
        assert acc.daily_loss_percent == pytest.approx(0.02, rel=1e-3)
