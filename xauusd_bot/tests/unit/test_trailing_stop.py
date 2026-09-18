from datetime import datetime

import pytest

from core.enums import Direction
from core.models import Candle, Position
from position_monitor.trailing_stop import (
    TrailingStopState,
    compute_trailing_stop_candidate,
    maybe_update_trailing_stop,
)


def _long_position(entry=3600.0, stop=3590.0) -> Position:
    return Position(symbol="XAUUSD", direction=Direction.LONG, lot_size=0.1, entry_price=entry, stop_loss=stop, take_profit=None)


def _short_position(entry=3600.0, stop=3610.0) -> Position:
    return Position(symbol="XAUUSD", direction=Direction.SHORT, lot_size=0.1, entry_price=entry, stop_loss=stop, take_profit=None)


def _candle(high, low, close=None) -> Candle:
    c = close if close is not None else (high + low) / 2
    return Candle(timestamp=datetime(2026, 1, 1), open=c, high=high, low=low, close=c, volume=10)


class TestTrailingStopState:
    def test_initial_state_starts_at_entry_price(self):
        pos = _long_position(entry=3600.0)
        state = TrailingStopState.initial(pos)
        assert state.favorable_extreme == pytest.approx(3600.0)

    def test_long_extreme_tracks_running_high(self):
        pos = _long_position()
        state = TrailingStopState.initial(pos)
        state.update_extreme(pos, _candle(high=3610, low=3605))
        assert state.favorable_extreme == pytest.approx(3610.0)
        state.update_extreme(pos, _candle(high=3605, low=3600))  # lower high -> no change
        assert state.favorable_extreme == pytest.approx(3610.0)
        state.update_extreme(pos, _candle(high=3620, low=3615))  # new high -> updates
        assert state.favorable_extreme == pytest.approx(3620.0)

    def test_short_extreme_tracks_running_low(self):
        pos = _short_position()
        state = TrailingStopState.initial(pos)
        state.update_extreme(pos, _candle(high=3595, low=3590))
        assert state.favorable_extreme == pytest.approx(3590.0)
        state.update_extreme(pos, _candle(high=3592, low=3591))  # higher low -> no change
        assert state.favorable_extreme == pytest.approx(3590.0)
        state.update_extreme(pos, _candle(high=3585, low=3580))  # new low -> updates
        assert state.favorable_extreme == pytest.approx(3580.0)


class TestComputeTrailingStopCandidate:
    def test_long_candidate_below_favorable_extreme(self):
        pos = _long_position()
        state = TrailingStopState(favorable_extreme=3620.0)
        candidate = compute_trailing_stop_candidate(pos, state, current_atr=2.0, atr_multiplier=1.5)
        assert candidate == pytest.approx(3620.0 - 3.0)

    def test_short_candidate_above_favorable_extreme(self):
        pos = _short_position()
        state = TrailingStopState(favorable_extreme=3580.0)
        candidate = compute_trailing_stop_candidate(pos, state, current_atr=2.0, atr_multiplier=1.5)
        assert candidate == pytest.approx(3580.0 + 3.0)

    def test_zero_atr_rejected(self):
        pos = _long_position()
        state = TrailingStopState(favorable_extreme=3620.0)
        with pytest.raises(ValueError):
            compute_trailing_stop_candidate(pos, state, current_atr=0.0, atr_multiplier=1.5)

    def test_zero_multiplier_rejected(self):
        pos = _long_position()
        state = TrailingStopState(favorable_extreme=3620.0)
        with pytest.raises(ValueError):
            compute_trailing_stop_candidate(pos, state, current_atr=2.0, atr_multiplier=0.0)


class TestMaybeUpdateTrailingStop:
    def test_long_stop_moves_up_when_candidate_is_tighter(self):
        pos = _long_position(entry=3600.0, stop=3590.0)
        state = TrailingStopState.initial(pos)
        # price rallies to 3620 -> candidate = 3620 - (2.0*1.5)=3617 > current stop 3590
        new_stop = maybe_update_trailing_stop(
            pos, state, _candle(high=3620, low=3615), current_atr=2.0, atr_multiplier=1.5
        )
        assert new_stop == pytest.approx(3617.0)

    def test_long_stop_never_loosens(self):
        # stop already trailed very tight (3618), favorable_extreme=3620.
        # A candle with no new high keeps extreme at 3620 -> candidate =
        # 3620 - (2.0*1.5)=3617, which is WORSE (lower) than the current
        # stop of 3618 -- this must NOT be applied, since it would loosen.
        pos = _long_position(entry=3600.0, stop=3618.0)
        state = TrailingStopState(favorable_extreme=3620.0)
        new_stop = maybe_update_trailing_stop(
            pos, state, _candle(high=3619, low=3615), current_atr=2.0, atr_multiplier=1.5
        )
        assert new_stop is None

    def test_short_stop_moves_down_when_candidate_is_tighter(self):
        pos = _short_position(entry=3600.0, stop=3610.0)
        state = TrailingStopState.initial(pos)
        new_stop = maybe_update_trailing_stop(
            pos, state, _candle(high=3585, low=3580), current_atr=2.0, atr_multiplier=1.5
        )
        assert new_stop == pytest.approx(3583.0)

    def test_no_new_favorable_extreme_produces_no_update(self):
        pos = _long_position(entry=3600.0, stop=3610.0)
        state = TrailingStopState(favorable_extreme=3620.0)
        new_stop = maybe_update_trailing_stop(
            pos, state, _candle(high=3615, low=3610), current_atr=2.0, atr_multiplier=1.5
        )
        # favorable_extreme unchanged (3620 > 3615) -> candidate = 3620-3=3617 > stop 3610 -> DOES tighten
        assert new_stop == pytest.approx(3617.0)

    def test_state_extreme_still_updates_even_when_stop_does_not(self):
        pos = _long_position(entry=3600.0, stop=3618.0)
        state = TrailingStopState(favorable_extreme=3620.0)
        result = maybe_update_trailing_stop(
            pos, state, _candle(high=3621, low=3619), current_atr=2.0, atr_multiplier=1.5
        )
        # extreme updates to 3621 regardless of whether the stop moves
        assert state.favorable_extreme == pytest.approx(3621.0)
