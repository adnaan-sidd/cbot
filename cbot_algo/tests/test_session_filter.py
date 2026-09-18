from datetime import datetime, time

import pytest

from config_schema import FilterConfig
from core_enums import RejectReason
from trade_filters import (
    check_session,
    is_within_allowed_sessions,
    parse_session_string,
)


class TestParseSessionString:
    def test_single_day(self):
        w = parse_session_string("FRI:01:00-23:57")
        assert w.weekdays == frozenset({4})
        assert w.start_time == time(1, 0)
        assert w.end_time == time(23, 57)

    def test_day_range(self):
        w = parse_session_string("MON-FRI:01:00-23:58")
        assert w.weekdays == frozenset({0, 1, 2, 3, 4})

    def test_wrapping_day_range(self):
        # e.g. FRI-MON would wrap around the week
        w = parse_session_string("FRI-MON:00:00-23:59")
        assert w.weekdays == frozenset({4, 5, 6, 0})

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError):
            parse_session_string("garbage")

    def test_unknown_weekday_raises(self):
        with pytest.raises(KeyError):
            parse_session_string("XXX:01:00-02:00")


class TestIsWithinAllowedSessions:
    def test_none_means_always_allowed(self):
        dt = datetime(2026, 1, 1, 3, 0)  # a Thursday
        assert is_within_allowed_sessions(dt, None) is True

    def test_within_configured_window_passes(self):
        sessions = ["MON-FRI:01:00-23:58"]
        dt = datetime(2026, 1, 6, 12, 0)  # a Tuesday
        assert is_within_allowed_sessions(dt, sessions) is True

    def test_outside_configured_window_fails(self):
        sessions = ["MON-FRI:01:00-23:58"]
        dt = datetime(2026, 1, 6, 0, 30)  # Tuesday 00:30, before window opens
        assert is_within_allowed_sessions(dt, sessions) is False

    def test_wrong_weekday_fails(self):
        sessions = ["MON-FRI:01:00-23:58"]
        dt = datetime(2026, 1, 4, 12, 0)  # a Sunday
        assert is_within_allowed_sessions(dt, sessions) is False

    def test_matches_any_of_multiple_windows(self):
        sessions = ["MON-THU:01:00-23:58", "FRI:01:00-23:57"]
        friday = datetime(2026, 1, 9, 23, 0)  # Friday, within its own window
        assert is_within_allowed_sessions(friday, sessions) is True

    def test_friday_after_its_earlier_close_fails(self):
        sessions = ["MON-THU:01:00-23:58", "FRI:01:00-23:57"]
        friday_late = datetime(2026, 1, 9, 23, 58)  # after Friday's 23:57 close
        assert is_within_allowed_sessions(friday_late, sessions) is False


class TestCheckSession:
    def test_no_restriction_passes(self):
        filters = FilterConfig(max_spread_points=35, allowed_sessions=None)
        assert check_session(datetime(2026, 1, 4, 3, 0), filters) is None  # Sunday, no restriction

    def test_restricted_outside_window_rejected(self):
        filters = FilterConfig(max_spread_points=35, allowed_sessions=["MON-FRI:01:00-23:58"])
        assert check_session(datetime(2026, 1, 4, 3, 0), filters) == RejectReason.SESSION_CLOSED  # Sunday
