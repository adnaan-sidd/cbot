"""
EventLogger: the single path every module uses to record what happened.

Two sinks, always both, always synchronous:
  1. Standard Python logging (human-readable, goes to file + console)
  2. SQLite `events` table (structured, queryable, permanent audit trail)

Nothing in this bot logs by calling `logging` or the DB directly elsewhere —
every module constructs a BotEvent and calls EventLogger.log(event).
This guarantees the "log before you act" rule (§9) is structurally true,
not just a convention someone can forget.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from core.events import BotEvent
from persistence.db_manager import DBManager


def _build_stdlib_logger(log_file: str | Path) -> logging.Logger:
    logger = logging.getLogger("xauusd_bot")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger  # avoid duplicate handlers if re-initialized

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    return logger


class EventLogger:
    def __init__(self, db_manager: DBManager, log_file: str | Path = "logs/bot.log"):
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        self.db = db_manager
        self.logger = _build_stdlib_logger(log_file)

    def log(self, event: BotEvent) -> None:
        """Write the event to both sinks. Raises if the DB write fails —
        per architecture policy, a broken persistence layer halts the bot
        rather than continuing to trade while blind."""
        self.logger.info("[%s] %s | %s", event.event_type.value, event.message, event.payload)

        conn = self.db.connect()
        conn.execute(
            """
            INSERT INTO events (id, timestamp, event_type, message, payload_json, signal_id, trade_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            event.to_row(),
        )

    def error(self, message: str, **payload) -> None:
        from core.enums import EventType

        self.log(BotEvent(event_type=EventType.ERROR, message=message, payload=payload))
