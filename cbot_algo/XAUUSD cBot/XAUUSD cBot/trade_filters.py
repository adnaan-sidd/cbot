"""Trade filters — cBot port of xauusd_bot/trade_filters/ (4 modules, 1 file).

Unchanged behavior: spread -> session -> duplicate-order checks in that
order (cheapest / most-likely-to-fail first), short-circuiting on the
first failure. They answer "is right now / this exact idea okay to trade
at all" — separate from the risk gate, which answers "is this trade
sized/margined safely for THIS account".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from config_schema import FilterConfig
from core_enums import Direction, RejectReason
from core_models import Signal

# =====================================================================
# spread_filter (xauusd_bot/trade_filters/spread_filter.py)
# =====================================================================

def check_spread(current_spread_points: float, filters: FilterConfig) -> Optional[RejectReason]:
    if current_spread_points < 0:
        raise ValueError(f"current_spread_points cannot be negative, got {current_spread_points}")
    if current_spread_points > filters.max_spread_points:
        return RejectReason.SPREAD_TOO_WIDE
    return None


# =====================================================================
# session_filter (xauusd_bot/trade_filters/session_filter.py)
# =====================================================================

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


# =====================================================================
# duplicate_order_guard (xauusd_bot/trade_filters/duplicate_order_guard.py)
# =====================================================================

@dataclass
class _SubmittedRecord:
    symbol: str
    direction: Direction
    entry_price: float
    submitted_at: datetime


@dataclass
class DuplicateOrderGuard:
    debounce_seconds: int
    price_tolerance: float = 0.0  # in price units (e.g. USD for XAUUSD); 0 = exact match required
    _recent: list[_SubmittedRecord] = field(default_factory=list)

    def _prune(self, *, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.debounce_seconds)
        self._recent = [r for r in self._recent if r.submitted_at >= cutoff]

    def check(self, signal: Signal, *, now: datetime) -> Optional[RejectReason]:
        """Read-only check — does NOT record the signal. Call `record()`
        separately once the signal is actually submitted, so a signal
        that fails a later check (e.g. risk gate) doesn't block a
        legitimate retry of the same idea."""
        self._prune(now=now)
        for r in self._recent:
            if (
                r.symbol == signal.symbol
                and r.direction == signal.direction
                and abs(r.entry_price - signal.entry_price) <= self.price_tolerance
            ):
                return RejectReason.DUPLICATE_ORDER
        return None

    def record(self, signal: Signal, *, now: datetime) -> None:
        self._prune(now=now)
        self._recent.append(
            _SubmittedRecord(
                symbol=signal.symbol,
                direction=signal.direction,
                entry_price=signal.entry_price,
                submitted_at=now,
            )
        )


# =====================================================================
# filter_chain (xauusd_bot/trade_filters/filter_chain.py)
# =====================================================================

@dataclass
class FilterResult:
    passed: bool
    reason: Optional[RejectReason] = None
    detail: str = ""


def run_filter_chain(
    *,
    signal: Signal,
    current_spread_points: float,
    now: datetime,
    filters: FilterConfig,
    duplicate_guard: DuplicateOrderGuard,
) -> FilterResult:
    reason = check_spread(current_spread_points, filters)
    if reason:
        return FilterResult(
            passed=False,
            reason=reason,
            detail=f"spread={current_spread_points} > max_spread_points={filters.max_spread_points}",
        )

    reason = check_session(now, filters)
    if reason:
        return FilterResult(
            passed=False,
            reason=reason,
            detail=f"timestamp {now.isoformat()} outside allowed_sessions={filters.allowed_sessions}",
        )

    reason = duplicate_guard.check(signal, now=now)
    if reason:
        return FilterResult(
            passed=False,
            reason=reason,
            detail=(
                f"matching {signal.direction.value} {signal.symbol} signal near "
                f"{signal.entry_price} submitted within the last "
                f"{duplicate_guard.debounce_seconds}s"
            ),
        )

    return FilterResult(passed=True)

