"""
Historical feed for backtesting (architecture doc, market_data module table).

Replays a fixed, pre-loaded sequence of Candles. This is the ONLY
broker/data-source-specific piece the backtest engine touches — strategy
and risk_engine code is identical between backtest and (eventual) live
running, per the architecture's "backtest and live share the same
risk/strategy code" rule.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from core.models import Candle
from market_data.feed_interface import MarketDataFeed


class HistoricalFeed(MarketDataFeed):
    def __init__(self, candles: list[Candle]):
        if candles != sorted(candles, key=lambda c: c.timestamp):
            raise ValueError("HistoricalFeed candles must be in chronological order")
        self._candles = candles

    def candles(self) -> Iterator[Candle]:
        yield from self._candles

    def __len__(self) -> int:
        return len(self._candles)

    @classmethod
    def from_csv(
        cls,
        path: str | Path,
        *,
        timestamp_format: Optional[str] = None,
    ) -> "HistoricalFeed":
        """Load candles from a CSV with columns:
        timestamp,open,high,low,close,volume[,spread_points]

        `timestamp_format` is a strptime format string; if omitted,
        ISO-8601 (`datetime.fromisoformat`) is assumed.
        """
        path = Path(path)
        rows: list[Candle] = []
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            required = {"timestamp", "open", "high", "low", "close", "volume"}
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"CSV {path} missing required columns: {missing}")

            for row in reader:
                ts_raw = row["timestamp"]
                ts = (
                    datetime.strptime(ts_raw, timestamp_format)
                    if timestamp_format
                    else datetime.fromisoformat(ts_raw)
                )
                spread_raw = row.get("spread_points")
                rows.append(
                    Candle(
                        timestamp=ts,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                        spread_points=float(spread_raw) if spread_raw not in (None, "") else None,
                    )
                )
        return cls(rows)
