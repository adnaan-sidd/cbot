"""
Live cTrader executor — real implementation against the installed
`ctrader-open-api` package (v0.9.2), not a structural placeholder.

=====================================================================
WHAT WAS ACTUALLY VERIFIED (by installing the real package and
inspecting its protobuf message definitions directly, not from memory):
  - Every message field name used below (ProtoOAApplicationAuthReq,
    ProtoOAAccountAuthReq, ProtoOANewOrderReq, ProtoOAClosePositionReq,
    ProtoOAAmendPositionSLTPReq) — confirmed against the installed
    package's actual .proto-derived Python classes.
  - ProtoOAExecutionType enum values (ORDER_FILLED, ORDER_REJECTED,
    ORDER_PARTIAL_FILL, ORDER_ACCEPTED) — exact names confirmed.
  - ProtoOATradeSide (BUY/SELL) and ProtoOAOrderType (MARKET) — exact
    names confirmed.
  - ProtoOAOrder carries `clientOrderId` directly, confirming the
    fill-correlation approach used in `_on_message`.
  - `Client.send()` returns a Twisted Deferred; message routing uses
    `message.payloadType` + `Protobuf.extract()` (confirmed via source
    inspection, not documentation).
  - The demo/live hosts (`demo.ctraderapi.com` / `live.ctraderapi.com`,
    port 5035) are Spotware's shared Open API infrastructure, used
    across broker white-labels including XM — not broker-specific.

WHAT IS STILL GENUINELY UNVERIFIED (cannot be confirmed without a live
connection, which this environment does not have):
  1. Whether a market order's fill details arrive via the ExecutionEvent
     this code waits for (the documented, standard cTrader pattern) or
     via some other path for XM's specific server configuration.
  2. XM's exact `symbolId` for XAUUSD — MUST be looked up per-account via
     ProtoOASymbolsListReq before this class can be used; passing the
     wrong ID will silently trade the wrong instrument.
  3. Volume-unit convention (centilots = lots*100, the standard cTrader
     convention per `stepVolume`/`minVolume` semantics) for THIS specific
     symbol on XM — verify against a real small test fill before sizing
     up.
  4. cTrader OAuth app credentials (client ID/secret) are set up through
     cTrader's own developer portal — entirely separate from your XM
     login — and an access token/ctidTraderAccountId obtained through
     that app's auth flow.
  5. Partial fills (ORDER_PARTIAL_FILL) are treated as a rejection here
     rather than handled — deliberately conservative given "never
     silently mishandle an ambiguous fill" is a harder requirement than
     "handle every edge case."

Do not use this against a live account until you've validated points
1-4 above against a cTrader DEMO account with small test orders.
=====================================================================

Design choices consistent with the rest of the codebase:
  - No automatic retries anywhere. A failed/timed-out request raises;
    the caller (state machine) decides what happens next.
  - Every failure raises a specific ExecutionError subclass.
  - Live fills use the broker's OWN reported price/commission/volume
    (from the ProtoOADeal), never a simulated or estimated value —
    unlike PaperExecutor, which necessarily estimates.
  - The Twisted reactor runs in a dedicated background thread (Twisted
    is asyncio-incompatible with a simple synchronous call); the
    synchronous ExecutionInterface methods bridge into it via
    `threads.blockingCallFromThread`, blocking the CALLING thread with
    an explicit timeout rather than ever hanging indefinitely.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Optional

from config.config_schema import BrokerConfig
from core.enums import Direction, ExitReason
from core.models import Position, Trade, TradeRequest
from execution.execution_interface import (
    BrokerConnectionError,
    ExecutionInterface,
    OrderRejectedError,
    PositionNotFoundError,
)

try:
    from twisted.internet import reactor, threads as twisted_threads
    from ctrader_open_api import Client, EndPoints, Protobuf, TcpProtocol
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAAccountAuthReq,
        ProtoOAAmendPositionSLTPReq,
        ProtoOAApplicationAuthReq,
        ProtoOAClosePositionReq,
        ProtoOAErrorRes,
        ProtoOAExecutionEvent,
        ProtoOANewOrderReq,
        ProtoOAOrderErrorEvent,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAExecutionType,
        ProtoOAOrderType,
        ProtoOATradeSide,
    )

    _CTRADER_SDK_AVAILABLE = True
except ImportError:
    _CTRADER_SDK_AVAILABLE = False


class CTraderExecutor(ExecutionInterface):
    """Real implementation — see module docstring for exactly what was
    verified vs. what still needs your own demo-account testing."""

    def __init__(
        self,
        *,
        broker: BrokerConfig,
        client_id: str,
        client_secret: str,
        access_token: str,
        ctid_trader_account_id: int,
        symbol_id: int,
        use_demo_endpoint: bool = True,
        connect_timeout_seconds: float = 15.0,
        request_timeout_seconds: float = 10.0,
    ):
        if not _CTRADER_SDK_AVAILABLE:
            raise ImportError(
                "ctrader-open-api / twisted are not installed. Run: pip install ctrader-open-api"
            )

        self.broker = broker
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.ctid_trader_account_id = ctid_trader_account_id
        self.symbol_id = symbol_id
        self.host = EndPoints.PROTOBUF_DEMO_HOST if use_demo_endpoint else EndPoints.PROTOBUF_LIVE_HOST
        self.port = EndPoints.PROTOBUF_PORT
        self.connect_timeout_seconds = connect_timeout_seconds
        self.request_timeout_seconds = request_timeout_seconds

        self._client: Optional["Client"] = None
        self._connected = False
        self._connect_error: Optional[str] = None

        self._open_positions: dict[str, Position] = {}
        self._broker_position_ids: dict[str, int] = {}  # our Position.id -> cTrader's own positionId
        self._pending_fills: dict[str, "queue.Queue"] = {}  # clientOrderId -> queue awaiting a fill/rejection event

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
                raise BrokerConnectionError(self._connect_error)
            time.sleep(0.1)
        raise BrokerConnectionError(
            f"cTrader connection/auth did not complete within {self.connect_timeout_seconds}s"
        )

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
        self._connected = True

    def _on_auth_failure(self, failure) -> None:
        self._connect_error = f"cTrader auth failed: {failure}"
        self._connected = False

    def _on_disconnected(self, _client, reason) -> None:
        self._connected = False

    def _on_message(self, _client, message) -> None:
        """Routes ExecutionEvents to whichever place_order/close_position
        call is waiting on a matching clientOrderId. Every other message
        type is ignored here deliberately — tick/bar data for the feed
        is handled by CTraderFeed, not this class."""
        if message.payloadType == ProtoOAExecutionEvent().payloadType:
            event = Protobuf.extract(message)
            client_order_id = event.order.clientOrderId if event.HasField("order") else None
            if client_order_id and client_order_id in self._pending_fills:
                self._pending_fills[client_order_id].put(event)
        elif message.payloadType == ProtoOAOrderErrorEvent().payloadType:
            event = Protobuf.extract(message)
            # OrderErrorEvent doesn't carry clientOrderId in this package
            # version -- broadcast to every pending waiter so at least one
            # in-flight request surfaces the error rather than timing out
            # silently. Safe because in practice only one order is placed
            # at a time (max_open_positions=1 upstream).
            for q in self._pending_fills.values():
                q.put(event)

    # -- order operations -------------------------------------------------

    def place_order(self, trade_request: TradeRequest) -> Position:
        if not self._connected:
            raise BrokerConnectionError("cTrader connection is not established")
        signal = trade_request.signal
        if signal.symbol in self._open_positions:
            raise OrderRejectedError(f"a position is already open for {signal.symbol}")

        client_order_id = trade_request.id
        fill_queue: "queue.Queue" = queue.Queue()
        self._pending_fills[client_order_id] = fill_queue

        request = ProtoOANewOrderReq()
        request.ctidTraderAccountId = self.ctid_trader_account_id
        request.symbolId = self.symbol_id
        request.orderType = ProtoOAOrderType.MARKET
        request.tradeSide = ProtoOATradeSide.BUY if signal.direction == Direction.LONG else ProtoOATradeSide.SELL
        request.volume = round(trade_request.lot_size * 100)  # centilots -- VERIFY, see module docstring
        request.stopLoss = signal.stop_loss
        if signal.take_profit is not None:
            request.takeProfit = signal.take_profit
        request.clientOrderId = client_order_id

        try:
            twisted_threads.blockingCallFromThread(reactor, self._client.send, request)
        except Exception as exc:
            self._pending_fills.pop(client_order_id, None)
            raise BrokerConnectionError(f"order request failed to send: {exc}") from exc

        try:
            event = fill_queue.get(timeout=self.request_timeout_seconds)
        except queue.Empty:
            raise BrokerConnectionError(
                f"no fill confirmation received within {self.request_timeout_seconds}s "
                f"for order {client_order_id} -- broker/network state is now AMBIGUOUS, "
                f"reconcile manually before retrying"
            )
        finally:
            self._pending_fills.pop(client_order_id, None)

        if isinstance(event, ProtoOAOrderErrorEvent):
            raise OrderRejectedError(f"order rejected: {event.description}")

        if event.executionType == ProtoOAExecutionType.ORDER_PARTIAL_FILL:
            raise OrderRejectedError(
                f"order partially filled ({event.deal.filledVolume} of {event.deal.volume} centilots) -- "
                f"partial fills are not handled; broker state now needs manual reconciliation"
            )
        if event.executionType != ProtoOAExecutionType.ORDER_FILLED:
            raise OrderRejectedError(f"order not filled: executionType={event.executionType}")

        deal = event.deal
        position = Position(
            symbol=signal.symbol,
            direction=signal.direction,
            lot_size=deal.filledVolume / 100.0,
            entry_price=deal.executionPrice,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            opened_at=trade_request.approved_at,
            broker_ticket=str(deal.positionId),
        )
        self._open_positions[signal.symbol] = position
        self._broker_position_ids[position.id] = deal.positionId
        return position

    def close_position(self, position: Position, exit_reason: ExitReason, reference_price: float) -> Trade:
        if not self._connected:
            raise BrokerConnectionError("cTrader connection is not established")
        tracked = self._open_positions.get(position.symbol)
        if tracked is None or tracked.id != position.id:
            raise PositionNotFoundError(f"no tracked open position matching {position.id} for {position.symbol}")
        broker_position_id = self._broker_position_ids.get(position.id)
        if broker_position_id is None:
            raise PositionNotFoundError(f"no broker positionId recorded for {position.id}")

        client_order_id = f"close-{position.id}"
        fill_queue: "queue.Queue" = queue.Queue()
        self._pending_fills[client_order_id] = fill_queue

        request = ProtoOAClosePositionReq()
        request.ctidTraderAccountId = self.ctid_trader_account_id
        request.positionId = broker_position_id
        request.volume = round(position.lot_size * 100)

        try:
            twisted_threads.blockingCallFromThread(reactor, self._client.send, request)
        except Exception as exc:
            self._pending_fills.pop(client_order_id, None)
            raise BrokerConnectionError(f"close request failed to send: {exc}") from exc

        try:
            event = fill_queue.get(timeout=self.request_timeout_seconds)
        except queue.Empty:
            raise BrokerConnectionError(
                f"no close confirmation received within {self.request_timeout_seconds}s "
                f"for position {position.id} -- broker/network state is now AMBIGUOUS, "
                f"reconcile manually — the position may or may not actually be closed"
            )
        finally:
            self._pending_fills.pop(client_order_id, None)

        if event.executionType != ProtoOAExecutionType.ORDER_FILLED:
            raise OrderRejectedError(f"close order not filled: executionType={event.executionType}")

        deal = event.deal
        exit_price = deal.executionPrice
        commission = deal.commission / 100.0  # cTrader commission is typically reported in cents -- VERIFY

        price_diff = (
            exit_price - position.entry_price
            if position.direction == Direction.LONG
            else position.entry_price - exit_price
        )
        gross_pnl = price_diff * position.lot_size * self.broker.contract_size
        net_pnl = gross_pnl - commission  # broker's own reported commission, not our estimate

        trade = Trade(
            symbol=position.symbol,
            direction=position.direction,
            lot_size=position.lot_size,
            entry_price=position.entry_price,
            exit_price=exit_price,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            opened_at=position.opened_at,
            closed_at=_execution_timestamp_fallback(),
            pnl=net_pnl,
            pnl_percent=0.0,  # caller supplies equity context if needed
            exit_reason=exit_reason,
            commission=commission,
            slippage_points=0.0,  # real fill -- no simulated slippage to report
        )
        del self._open_positions[position.symbol]
        del self._broker_position_ids[position.id]
        return trade

    def modify_sl_tp(
        self, position: Position, *, new_stop_loss: Optional[float] = None, new_take_profit: Optional[float] = None
    ) -> Position:
        if not self._connected:
            raise BrokerConnectionError("cTrader connection is not established")
        tracked = self._open_positions.get(position.symbol)
        if tracked is None or tracked.id != position.id:
            raise PositionNotFoundError(f"no tracked open position matching {position.id} for {position.symbol}")
        broker_position_id = self._broker_position_ids.get(position.id)
        if broker_position_id is None:
            raise PositionNotFoundError(f"no broker positionId recorded for {position.id}")

        request = ProtoOAAmendPositionSLTPReq()
        request.ctidTraderAccountId = self.ctid_trader_account_id
        request.positionId = broker_position_id
        effective_stop = new_stop_loss if new_stop_loss is not None else tracked.stop_loss
        effective_target = new_take_profit if new_take_profit is not None else tracked.take_profit
        request.stopLoss = effective_stop
        if effective_target is not None:
            request.takeProfit = effective_target

        try:
            twisted_threads.blockingCallFromThread(reactor, self._client.send, request)
        except Exception as exc:
            raise BrokerConnectionError(f"amend SL/TP request failed: {exc}") from exc

        updated = Position(
            symbol=tracked.symbol,
            direction=tracked.direction,
            lot_size=tracked.lot_size,
            entry_price=tracked.entry_price,
            stop_loss=effective_stop,
            take_profit=effective_target,
            opened_at=tracked.opened_at,
            broker_ticket=tracked.broker_ticket,
            id=tracked.id,
        )
        self._open_positions[position.symbol] = updated
        return updated

    def get_open_position(self, symbol: str) -> Optional[Position]:
        return self._open_positions.get(symbol)


def _execution_timestamp_fallback():
    """UNVERIFIED placeholder: ProtoOADeal's execution timestamp field
    (`executionTimestamp`, epoch milliseconds) should be converted and
    used here instead of "now" -- named explicitly as a fallback rather
    than silently using the wrong timestamp, since trade-history timing
    matters for reporting."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
