-- XAUUSD bot database schema (architecture doc §4).
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

-- Mirrors broker's currently-open position(s). Reconciled against the
-- broker on boot and on every monitor tick — this table should never
-- silently diverge from what the broker actually reports.
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

-- Append-only event log. Every signal, rejection, order attempt, fill,
-- SL/TP change, state transition and error goes here BEFORE being acted
-- on (architecture §9). This is the primary audit trail.
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

-- Periodic equity/margin snapshots, independent of trade events — this is
-- what drawdown tracking (§5.4) is computed from, so it must exist even
-- on days/periods with zero trades.
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
