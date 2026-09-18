from datetime import datetime

import pytest

from core_enums import Direction, ExitReason
from core_models import Candle, Position
from position_monitor import check_stop_or_target, compute_unrealized_pnl


def _position(direction=Direction.LONG, entry=3600.0, sl=3590.0, tp=3620.0) -> Position:
    return Position(
        symbol="XAUUSD", direction=direction, lot_size=0.1,
        entry_price=entry, stop_loss=sl, take_profit=tp,
    )


def _candle(high, low, close=None) -> Candle:
    c = close if close is not None else (high + low) / 2
    return Candle(timestamp=datetime(2026, 1, 1), open=c, high=high, low=low, close=c, volume=10)


class TestCheckStopOrTarget:
    def test_long_no_hit_returns_none(self):
        pos = _position()
        result = check_stop_or_target(pos, _candle(3605, 3595))
        assert result is None

    def test_long_stop_hit(self):
        pos = _position()
        result = check_stop_or_target(pos, _candle(3595, 3585))
        assert result == (ExitReason.STOP_LOSS, 3590.0)

    def test_long_target_hit(self):
        pos = _position()
        result = check_stop_or_target(pos, _candle(3625, 3615))
        assert result == (ExitReason.TAKE_PROFIT, 3620.0)

    def test_long_both_touched_assumes_stop_first(self):
        pos = _position()
        result = check_stop_or_target(pos, _candle(3625, 3585))
        assert result == (ExitReason.STOP_LOSS, 3590.0)

    def test_short_stop_hit(self):
        pos = _position(direction=Direction.SHORT, entry=3600.0, sl=3610.0, tp=3580.0)
        result = check_stop_or_target(pos, _candle(3615, 3605))
        assert result == (ExitReason.STOP_LOSS, 3610.0)

    def test_short_target_hit(self):
        pos = _position(direction=Direction.SHORT, entry=3600.0, sl=3610.0, tp=3580.0)
        result = check_stop_or_target(pos, _candle(3595, 3575))
        assert result == (ExitReason.TAKE_PROFIT, 3580.0)

    def test_none_take_profit_never_matches(self):
        pos = Position(
            symbol="XAUUSD", direction=Direction.LONG, lot_size=0.1,
            entry_price=3600.0, stop_loss=3590.0, take_profit=None,
        )
        result = check_stop_or_target(pos, _candle(3700, 3650))  # would've hit a TP if one existed
        assert result is None


class TestComputeUnrealizedPnl:
    def test_long_profit(self):
        pos = _position(entry=3600.0)
        pnl = compute_unrealized_pnl(pos, current_price=3610.0, contract_size=100.0)
        assert pnl == pytest.approx((3610.0 - 3600.0) * 0.1 * 100.0)

    def test_long_loss(self):
        pos = _position(entry=3600.0)
        pnl = compute_unrealized_pnl(pos, current_price=3590.0, contract_size=100.0)
        assert pnl < 0

    def test_short_profit(self):
        pos = _position(direction=Direction.SHORT, entry=3600.0, sl=3610.0, tp=3580.0)
        pnl = compute_unrealized_pnl(pos, current_price=3590.0, contract_size=100.0)
        assert pnl == pytest.approx((3600.0 - 3590.0) * 0.1 * 100.0)

    def test_short_loss(self):
        pos = _position(direction=Direction.SHORT, entry=3600.0, sl=3610.0, tp=3580.0)
        pnl = compute_unrealized_pnl(pos, current_price=3610.0, contract_size=100.0)
        assert pnl < 0

    def test_zero_movement_zero_pnl(self):
        pos = _position(entry=3600.0)
        pnl = compute_unrealized_pnl(pos, current_price=3600.0, contract_size=100.0)
        assert pnl == pytest.approx(0.0)
