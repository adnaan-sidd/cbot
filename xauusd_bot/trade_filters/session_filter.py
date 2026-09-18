"""
Session filter (architecture doc, trade_filters module table).

Optional by default: `FilterConfig.allowed_sessions is None` means no
restriction at all (matches config.yaml's default `allowed_sessions: null`).
When configured, sessions are given as simple strings so they stay easy
to read/edit in YAML:

    "MON-FRI:01:00-23:58"   # a weekday range with one daily time window
    "FRI:01:00-23:57"       # a single weekday

All times are interpreted as UTC, consistent with the daily-loss-limit
reset (UTC midnight) decided for this bot. A signal passes if its
timestamp falls inside ANY configured window (windows are OR'd together,
not AND'd) — this lets you express "trade Mon-Thu one way, Friday another"
as two separate strings in the same list.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Optional

from config.config_schema import FilterConfig
from core.enums import RejectReason

_WEEKDAY_NAMES = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


@dataclass(frozen=True)
class SessionWindow:
    weekdays: frozenset[int]  # 0=Monday ... 6=Sunday (datetime.weekday() convention)
    start_time: time
    end_time: time

    def contains(self, dt: datetime) -> bool:
        if dt.weekday() not in self.weekdays:
            return False
        t = dt.time()
        if self.start_time <= self.end_time:
            return self.start_time <= t <= self.end_time
        # overnight window (e.g. 22:00-02:00) — not currently needed by any
        # configured trading session but supported for completeness
        return t >= self.start_time or t <= self.end_time


def _parse_time(value: str) -> time:
    hour_str, minute_str = value.strip().split(":")
    return time(hour=int(hour_str), minute=int(minute_str))


def parse_session_string(session: str) -> SessionWindow:
    """Parse one session string, e.g. 'MON-FRI:01:00-23:58' or 'FRI:01:00-23:57'."""
    try:
        day_part, time_part = session.split(":", 1)
        start_str, end_str = time_part.split("-")
    except ValueError as exc:
        raise ValueError(
            f"invalid session string {session!r}; expected format 'DAY[-DAY]:HH:MM-HH:MM'"
        ) from exc

    if "-" in day_part:
        start_day_str, end_day_str = day_part.split("-")
        start_day = _WEEKDAY_NAMES[start_day_str.strip().upper()]
        end_day = _WEEKDAY_NAMES[end_day_str.strip().upper()]
        if start_day <= end_day:
            weekdays = frozenset(range(start_day, end_day + 1))
        else:
            weekdays = frozenset(list(range(start_day, 7)) + list(range(0, end_day + 1)))
    else:
        weekdays = frozenset({_WEEKDAY_NAMES[day_part.strip().upper()]})

    return SessionWindow(
        weekdays=weekdays,
        start_time=_parse_time(start_str),
        end_time=_parse_time(end_str),
    )


def is_within_allowed_sessions(dt: datetime, allowed_sessions: Optional[list[str]]) -> bool:
    if allowed_sessions is None:
        return True
    windows = [parse_session_string(s) for s in allowed_sessions]
    return any(w.contains(dt) for w in windows)


def check_session(dt: datetime, filters: FilterConfig) -> Optional[RejectReason]:
    if not is_within_allowed_sessions(dt, filters.allowed_sessions):
        return RejectReason.SESSION_CLOSED
    return None
