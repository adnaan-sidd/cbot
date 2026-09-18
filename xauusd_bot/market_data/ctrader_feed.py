"""
Live cTrader market data feed — real implementation against the
installed `ctrader-open-api` package, not a structural placeholder.

=====================================================================
WHAT WAS ACTUALLY VERIFIED (by inspecting the installed package's
protobuf definitions directly):
  - ProtoOASubscribeSpotsReq fields (ctidTraderAccountId, symbolId as a
    repeated field, subscribeToSpotTimestamp).
  - ProtoOASpotEvent fields (symbolId, bid, ask, timestamp, trendbar) —
    confirms ticks arrive as bid/ask pairs, which this feed midpoints
    into a single price for candle aggregation.
  - Message routing via payloadType + Protobuf.extract(), same pattern
    verified for execution/ctrader_executor.py.

WHAT IS STILL UNVERIFIED — same connection/auth caveats as
ctrader_executor.py (see that module's docstring for the full list:
symbolId lookup, OAuth app credentials, etc.) — PLUS, specific to this
feed:
  - Whether `bid` in ProtoOASpotEvent is already a real price or needs
    scaling by the symbol's digits/pip position (cTrader's spot events
    commonly report prices as integers scaled by 10^digits — e.g.
    360012 for 3600.12 at digits=2 — this feed divides by 10**digits
    accordingly, but that specific convention needs confirming against
    a real received tick before trusting it).
  - Tick volume here is a simple per-bucket tick COUNT (see
    _emit_completed_bucket), not real traded volume — cTrader's spot
    stream doesn't carry per-tick volume in the fields inspected.
=====================================================================

Bridges cTrader's async (Twisted) callback model into the simple
synchronous `candles()` generator MarketDataFeed requires, via a
thread-safe queue — see the class docstring below for the mechanism.
Reconnection is deliberately NOT handled here (a disconnect stops the
generator rather than silently retrying); a supervising process
decides when/whether to reconnect, matching the "no silent retries"
rule used throughout this codebase.
"""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from core.models import Candle
from market_data.feed_interface import MarketDataFeed

try:
    from twisted.internet import reactor
    from ctrader_open_api import Client, EndPoints, Protobuf, TcpProtocol
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAAccountAuthReq,
        ProtoOAApplicationAuthReq,
        ProtoOASpotEvent,
        ProtoOASubscribeSpotsReq,
    )

    _CTRADER_SDK_AVAILABLE = True
except ImportError:
    _CTRADER_SDK_AVAILABLE = False


class CTraderFeed(MarketDataFeed):
    """Real implementation — see module docstring for what was verified
    vs. what still needs your own demo-account testing."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        access_token: str,
        ctid_trader_account_id: int,
        symbol_id: int,
        digits: int,
        use_demo_endpoint: bool = True,
        timeframe_minutes: int = 15,
        connect_timeout_seconds: float = 15.0,
        queue_maxsize: int = 1000,
    ):
        if not _CTRADER_SDK_AVAILABLE:
            raise ImportError(
                "ctrader-open-api / twisted are not installed. Run: pip install ctrader-open-api"
            )

        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.ctid_trader_account_id = ctid_trader_account_id
        self.symbol_id = symbol_id
        self.digits = digits
        self.host = EndPoints.PROTOBUF_DEMO_HOST if use_demo_endpoint else EndPoints.PROTOBUF_LIVE_HOST
        self.port = EndPoints.PROTOBUF_PORT
        self.timeframe_minutes = timeframe_minutes
        self.connect_timeout_seconds = connect_timeout_seconds

        self._client: Optional["Client"] = None
        self._connected = False
        self._connect_error: Optional[str] = None

        self._queue: "queue.Queue[Optional[Candle]]" = queue.Queue(maxsize=queue_maxsize)
        self._current_bucket_start: Optional[datetime] = None
        self._current_bucket_ticks: list[tuple[datetime, float]] = []

    # -- connection lifecycle --------------------------------------------

    def connect(self) -> None:
        from market_data.ctrader_reactor import ensure_reactor_running

        ensure_reactor_running()

        self._client = Client(self.host, self.port, TcpProtocol)
        self._client.setConnectedCallback(self._on_connected)
        self._client.setDisconnectedCallback(self._on_disconnected)
        self._client.setMessageReceivedCallback(self._on_message)
        reactor.callFromThread(self._client.startService)

        deadline = time.time() + self.connect_timeout_seconds
        while time.time() < deadline:
            if self._connected:
                return
            if self._connect_error:
                raise ConnectionError(self._connect_error)
            time.sleep(0.1)
        raise ConnectionError(f"cTrader connection/auth did not complete within {self.connect_timeout_seconds}s")

    def is_connected(self) -> bool:
        return self._connected

    def _on_connected(self, client) -> None:
        auth_req = ProtoOAApplicationAuthReq()
        auth_req.clientId = self.client_id
        auth_req.clientSecret = self.client_secret
        client.send(auth_req).addCallbacks(self._on_app_auth_success, self._on_auth_failure)

    def _on_app_auth_success(self, _response) -> None:
        acc_req = ProtoOAAccountAuthReq()
        acc_req.ctidTraderAccountId = self.ctid_trader_account_id
        acc_req.accessToken = self.access_token
        self._client.send(acc_req).addCallbacks(self._on_account_auth_success, self._on_auth_failure)

    def _on_account_auth_success(self, _response) -> None:
        subscribe_req = ProtoOASubscribeSpotsReq()
        subscribe_req.ctidTraderAccountId = self.ctid_trader_account_id
        subscribe_req.symbolId.append(self.symbol_id)
        self._client.send(subscribe_req).addCallbacks(
            lambda _r: setattr(self, "_connected", True), self._on_auth_failure
        )

    def _on_auth_failure(self, failure) -> None:
        self._connect_error = f"cTrader auth/subscribe failed: {failure}"
        self._connected = False

    def _on_disconnected(self, _client, reason) -> None:
        self._connected = False
        # Deliberately no reconnect attempt here -- see module docstring.
        # Unblock any caller waiting in candles() rather than hanging forever.
        self._queue.put(None)

    def _on_message(self, _client, message) -> None:
        if message.payloadType != ProtoOASpotEvent().payloadType:
            return
        event = Protobuf.extract(message)
        if event.symbolId != self.symbol_id or not event.HasField("bid") or not event.HasField("ask"):
            return
        # UNVERIFIED price scaling convention -- see module docstring.
        price = ((event.bid + event.ask) / 2.0) / (10 ** self.digits)
        timestamp = datetime.fromtimestamp(event.timestamp / 1000.0, tz=timezone.utc)
        self._on_tick(timestamp, price)

    # -- tick-to-candle aggregation (pure, fully tested) -----------------

    def _on_tick(self, timestamp: datetime, price: float) -> None:
        """Aggregates ticks into `timeframe_minutes` candles, pushing a
        completed candle onto the queue the moment a tick from the NEXT
        bucket arrives — mirroring strategy/resample.py's look-ahead-bias
        rule: a bucket is only considered closed once something from the
        next bucket proves it."""
        bucket_start = self._floor_to_bucket(timestamp)

        if self._current_bucket_start is None:
            self._current_bucket_start = bucket_start
            self._current_bucket_ticks = [(timestamp, price)]
            return

        if bucket_start == self._current_bucket_start:
            self._current_bucket_ticks.append((timestamp, price))
            return

        self._emit_completed_bucket()
        self._current_bucket_start = bucket_start
        self._current_bucket_ticks = [(timestamp, price)]

    def _emit_completed_bucket(self) -> None:
        if not self._current_bucket_ticks:
            return
        prices = [p for _, p in self._current_bucket_ticks]
        candle = Candle(
            timestamp=self._current_bucket_start,
            open=prices[0],
            high=max(prices),
            low=min(prices),
            close=prices[-1],
            volume=float(len(prices)),  # tick count, not real volume -- see module docstring
            spread_points=None,
        )
        self._queue.put(candle)

    def _floor_to_bucket(self, timestamp: datetime) -> datetime:
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        minutes_since_epoch = int((timestamp - epoch).total_seconds() // 60)
        bucket_index = minutes_since_epoch // self.timeframe_minutes
        return epoch + timedelta(minutes=bucket_index * self.timeframe_minutes)

    def candles(self) -> Iterator[Candle]:
        """Blocks waiting for candles pushed by `_on_tick`/`_on_message`.
        Stops when a `None` sentinel is pushed — either a clean shutdown
        or an unexpected disconnect (see `_on_disconnected`), so a caller
        distinguishing the two should check `is_connected()` after the
        generator ends rather than assuming a clean stop."""
        while True:
            candle = self._queue.get()
            if candle is None:
                return
            yield candle
