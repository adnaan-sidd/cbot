"""Audit persistence — cBot port of xauusd_bot/persistence/.

Porting notes:
  - The schema (db_schema.sql in the original repo) is embedded verbatim
    as SCHEMA_SQL — the cTrader runtime has no guaranteed file paths to
    load it from.
  - The original DBManager read the schema from disk; here it is inline.
    Same connection handling (WAL journal, lazy connection).
  - The original EventLogger wrote to BOTH stdlib logging and the SQLite
    `events` table, and raised on DB failure ("a broken persistence
    layer halts the bot rather than continuing to trade while blind").
    AuditTrail keeps that policy when `strict=True`; when `strict=False`
    it degrades to console-only logging (a cBot operator choice, logged
    loudly). It additionally fills the signals / rejected_signals /
    trades / positions / account_snapshots / daily_stats tables the
    original schema defines — "log before you act" for every lifecycle
    object, not just events.
  - In cTrader, api.Print is the console sink (visible in the cBot's
    Log tab); a local file log is also written next to the DB.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from core_events import BotEvent
from core_enums import EventType
from core_models import AccountState, Position, RejectedSignal, Signal, Trade

SCHEMA_SQL = """
-- XAUUSD bot database schema (architecture doc §4) — embedded verbatim
-- from xauusd_bot/persistence/db_schema.sql for the cBot port.
-- Design rule: append-only. Nothing here is ever UPDATEd or DELETEd except
-- the `positions` table, which mirrors current broker state and is
-- reconciled, not historized (its history lives in `trades` once closed).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS signals (
    id              TEXT PRIMARY KEY,
    timestamp       TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('long', 'short')),
    entry_price     REAL NOT NULL,
    stop_loss       REAL NOT NULL,
    take_profit     REAL,
    strategy_name   TEXT NOT NULL,
    confidence      REAL,
    metadata_json   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rejected_signals (
    id              TEXT PRIMARY KEY,
    signal_id       TEXT NOT NULL REFERENCES signals(id),
    reason          TEXT NOT NULL,
    detail          TEXT,
    timestamp       TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trades (
    id                  TEXT PRIMARY KEY,
    symbol              TEXT NOT NULL,
    direction           TEXT NOT NULL CHECK (direction IN ('long', 'short')),
    lot_size            REAL NOT NULL,
    entry_price         REAL NOT NULL,
    exit_price          REAL NOT NULL,
    stop_loss           REAL NOT NULL,
    take_profit         REAL,
    opened_at           TEXT NOT NULL,
    closed_at           TEXT NOT NULL,
    pnl                 REAL NOT NULL,
    pnl_percent         REAL NOT NULL,
    exit_reason         TEXT NOT NULL CHECK (exit_reason IN ('sl','tp','manual','kill_switch','trailing_stop')),
    commission          REAL NOT NULL,
    slippage_points     REAL NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS positions (
    id              TEXT PRIMARY KEY,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('long', 'short')),
    lot_size        REAL NOT NULL,
    entry_price     REAL NOT NULL,
    stop_loss       REAL NOT NULL,
    take_profit     REAL,
    opened_at       TEXT NOT NULL,
    broker_ticket   TEXT,
    is_open         INTEGER NOT NULL DEFAULT 1 CHECK (is_open IN (0, 1)),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    id              TEXT PRIMARY KEY,
    timestamp       TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    message         TEXT NOT NULL,
    payload_json    TEXT,
    signal_id       TEXT,
    trade_id        TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);

CREATE TABLE IF NOT EXISTS daily_stats (
    trading_day             TEXT PRIMARY KEY,   -- ISO date, UTC
    start_equity            REAL NOT NULL,
    end_equity              REAL,
    pnl                     REAL,
    trades_count            INTEGER NOT NULL DEFAULT 0,
    daily_loss_limit_hit    INTEGER NOT NULL DEFAULT 0 CHECK (daily_loss_limit_hit IN (0, 1)),
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS account_snapshots (
    id              TEXT PRIMARY KEY,
    timestamp       TEXT NOT NULL,
    equity          REAL NOT NULL,
    balance         REAL NOT NULL,
    free_margin     REAL NOT NULL,
    used_margin     REAL NOT NULL,
    peak_equity     REAL NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON account_snapshots(timestamp);
"""


def _iso(dt: Optional[datetime]) -> str:
    if dt is None:
        return datetime.now(timezone.utc).isoformat()
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).isoformat()
    return dt.replace(tzinfo=timezone.utc).isoformat()


class AuditTrail:
    """Single path every module uses to record what happened (the cBot
    equivalent of xauusd_bot's EventLogger + DBManager, extended with the
    per-object writers the schema calls for)."""

    def __init__(
        self,
        *,
        audit_dir: Path,
        print_fn: Optional[Callable[..., None]] = None,
        strict: bool = True,
    ):
        self.audit_dir = Path(audit_dir)
        self.strict = strict
        self._print = print_fn or (lambda *a, **k: None)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.audit_dir / "audit.db"
        self.log_path = self.audit_dir / "bot.log"

        self.logger = self._build_stdlib_logger()
        self._init_schema()

    # -- connection ------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, isolation_level=None)
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_schema(self) -> None:
        conn = self._connect()
        conn.executescript(SCHEMA_SQL)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def _build_stdlib_logger(self) -> logging.Logger:
        logger = logging.getLogger("xauusd_cbot")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if logger.handlers:
            return logger
        fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        try:
            file_handler = logging.FileHandler(self.log_path, encoding="utf-8")
            file_handler.setFormatter(fmt)
            logger.addHandler(file_handler)
        except Exception:
            pass  # file sink is best-effort; console + DB are the real sinks
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(fmt)
        logger.addHandler(console_handler)
        return logger

    def _write(self, sql: str, params: tuple) -> None:
        """Executes a write. strict=True raises on failure (bot halts —
        the original repo's policy); strict=False logs loudly and moves
        on."""
        try:
            with self._lock:
                conn = self._connect()
                conn.execute(sql, params)
        except Exception as exc:
            self.logger.error("AUDIT WRITE FAILED: %s (sql: %s)", exc, sql[:80])
            self._print(f"[AUDIT WRITE FAILED] {exc}")
            if self.strict:
                raise

    # -- events (the primary audit trail) --------------------------------

    def log(self, event: BotEvent) -> None:
        """EventLogger-compatible: the state machine calls .log(BotEvent).
        Writes to BOTH sinks. Raises if the DB write fails and strict."""
        self.logger.info("[%s] %s | %s", event.event_type.value, event.message, event.payload)
        self._print(f"[{event.event_type.value}] {event.message} | {event.payload}")
        self._write(
            """
            INSERT INTO events (id, timestamp, event_type, message, payload_json, signal_id, trade_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                _iso(event.timestamp),
                event.event_type.value,
                event.message,
                json.dumps(event.payload, default=str),
                event.signal_id,
                event.trade_id,
            ),
        )

    # -- per-object writers ----------------------------------------------

    def record_signal(self, signal: Signal) -> None:
        self._write(
            """
            INSERT OR IGNORE INTO signals
                (id, timestamp, symbol, direction, entry_price, stop_loss,
                 take_profit, strategy_name, confidence, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.id,
                _iso(signal.timestamp),
                signal.symbol,
                signal.direction.value,
                signal.entry_price,
                signal.stop_loss,
                signal.take_profit,
                signal.strategy_name,
                signal.confidence,
                json.dumps(signal.metadata, default=str) if signal.metadata else None,
            ),
        )

    def record_rejected_signal(self, rejected: RejectedSignal) -> None:
        # ensure the parent signal row exists (rejected rows reference it)
        self.record_signal(rejected.signal)
        self._write(
            """
            INSERT OR IGNORE INTO rejected_signals (id, signal_id, reason, detail, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """,
            (rejected.id, rejected.signal.id, rejected.reason.value, rejected.detail, _iso(rejected.timestamp)),
        )

    def record_trade(self, trade: Trade) -> None:
        self._write(
            """
            INSERT OR REPLACE INTO trades
                (id, symbol, direction, lot_size, entry_price, exit_price, stop_loss,
                 take_profit, opened_at, closed_at, pnl, pnl_percent, exit_reason,
                 commission, slippage_points)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.id,
                trade.symbol,
                trade.direction.value,
                trade.lot_size,
                trade.entry_price,
                trade.exit_price,
                trade.stop_loss,
                trade.take_profit,
                _iso(trade.opened_at),
                _iso(trade.closed_at),
                trade.pnl,
                trade.pnl_percent,
                trade.exit_reason.value,
                trade.commission,
                trade.slippage_points,
            ),
        )

    def upsert_position(self, position: Position, *, is_open: bool) -> None:
        self._write(
            """
            INSERT INTO positions
                (id, symbol, direction, lot_size, entry_price, stop_loss, take_profit,
                 opened_at, broker_ticket, is_open)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                stop_loss = excluded.stop_loss,
                take_profit = excluded.take_profit,
                is_open = excluded.is_open,
                updated_at = datetime('now')
            """,
            (
                position.id,
                position.symbol,
                position.direction.value,
                position.lot_size,
                position.entry_price,
                position.stop_loss,
                position.take_profit,
                _iso(position.opened_at),
                position.broker_ticket,
                1 if is_open else 0,
            ),
        )

    def record_snapshot(self, account: AccountState, *, ts: Optional[datetime] = None) -> None:
        import uuid

        self._write(
            """
            INSERT INTO account_snapshots
                (id, timestamp, equity, balance, free_margin, used_margin, peak_equity)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                _iso(ts),
                account.equity,
                account.balance,
                account.free_margin,
                account.used_margin,
                account.peak_equity,
            ),
        )

    def upsert_daily_stats(
        self,
        *,
        trading_day: date,
        start_equity: float,
        end_equity: float,
        pnl: float,
        trades_count: int,
        daily_loss_limit_hit: bool,
    ) -> None:
        self._write(
            """
            INSERT INTO daily_stats
                (trading_day, start_equity, end_equity, pnl, trades_count, daily_loss_limit_hit)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(trading_day) DO UPDATE SET
                end_equity = excluded.end_equity,
                pnl = excluded.pnl,
                trades_count = excluded.trades_count,
                daily_loss_limit_hit = MAX(daily_loss_limit_hit, excluded.daily_loss_limit_hit),
                updated_at = datetime('now')
            """,
            (
                trading_day.isoformat(),
                start_equity,
                end_equity,
                pnl,
                trades_count,
                1 if daily_loss_limit_hit else 0,
            ),
        )

    # -- convenience ------------------------------------------------------

    def error(self, message: str, **payload) -> None:
        self.log(BotEvent(event_type=EventType.ERROR, message=message, payload=payload))
