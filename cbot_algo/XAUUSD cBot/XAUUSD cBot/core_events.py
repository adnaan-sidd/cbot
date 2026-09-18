"""
A single, uniform event envelope used for everything written to the
events log/table (architecture §9: "every signal, rejection, execution,
SL/TP change and error is logged before it is acted on").

Modules never write directly to the database or log file — they construct
a BotEvent and hand it to persistence.logger.EventLogger. This keeps
"what gets logged" decoupled from "how it's stored".
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from core_enums import EventType


@dataclass
class BotEvent:
    event_type: EventType
    message: str
    payload: dict[str, Any] = field(default_factory=dict)
    signal_id: Optional[str] = None
    trade_id: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_row(self) -> tuple:
        """Serialize to a tuple matching the `events` table column order
        in persistence/db_schema.sql."""
        return (
            self.id,
            self.timestamp.isoformat(),
            self.event_type.value,
            self.message,
            json.dumps(self.payload, default=str),
            self.signal_id,
            self.trade_id,
        )
