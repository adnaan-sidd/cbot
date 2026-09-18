"""Mock cTrader platform for local testing of the XAUUSD cBot.

Emulates exactly the slice of the cAlgo.API surface the cBot uses:
Symbol (Bid/Ask/Spread/PipSize/Digits/quantity conversion), Bars
(oldest-first, LastBar, HistoryLoaded), Account (Equity/Balance/
FreeMargin/IsLive/Number), Positions (FindAll, Closed event),
Server.Time, and the order operations (ExecuteMarketOrder,
ClosePosition, ModifyPosition + price-based position modifiers).

The broker side is simulated faithfully enough for the safety logic:
market orders fill at the touch price, SL/TP are broker-enforced on
every tick (SL checked before TP — the same worst-case convention as
the repo's backtest engine), commissions follow the $/1M-notional
model, and account equity is marked to market on every tick.

Nothing here imports cTrader or pythonnet — pure Python, runs under
pytest anywhere.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional


# --- enums (mirror the real cAlgo.API values the cBot relies on) ---------

class TradeType:
    Buy = 1
    Sell = -1


class TimeFrame:
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"


class PositionCloseReason:
    Closed = 0
    StopLoss = 1
    StopOut = 2
    TakeProfit = 3


class ErrorCode:
    NoMoney = 100


TIMEFRAME_BY_MINUTES = {
    1: TimeFrame.M1,
    5: TimeFrame.M5,
    15: TimeFrame.M15,
    30: TimeFrame.M30,
    60: TimeFrame.H1,
    240: TimeFrame.H4,
    1440: TimeFrame.D1,
}


# --- market data -----------------------------------------------------------


class MockEvent:
    """Stand-in for a .NET event: supports `+=` / `-=` subscription the
    way cTrader's Positions.Closed / Bars.HistoryLoaded do."""

    def __init__(self):
        self._handlers = []

    def __iadd__(self, handler):
        self._handlers.append(handler)
        return self

    def __isub__(self, handler):
        if handler in self._handlers:
            self._handlers.remove(handler)
        return self

    def fire(self, args) -> None:
        for handler in list(self._handlers):
            handler(args)

class MockBar:
    def __init__(self, timestamp, open_, high, low, close, volume=10.0):
        self.Time = timestamp
        self.Open = open_
        self.High = high
        self.Low = low
        self.Close = close
        self.Volume = volume


class MockBars:
    """Oldest-first bar list with the last bar 'forming', mirroring
    cTrader's Bars collection during live running."""

    def __init__(self):
        self._closed: List[MockBar] = []
        self._forming: Optional[MockBar] = None
        self.HistoryLoaded = MockEvent()
        self.load_requested = False

    # cTrader-style accessors
    @property
    def Count(self) -> int:
        return len(self._closed) + (1 if self._forming is not None else 0)

    @property
    def LastBar(self) -> MockBar:
        return self._closed[-1] if (self._forming is None and self._closed) else self._forming

    def __getitem__(self, i: int) -> MockBar:
        all_bars = self._closed + ([self._forming] if self._forming is not None else [])
        return all_bars[i]

    # driver side
    def push_closed(self, bar: MockBar) -> None:
        self._closed.append(bar)

    def start_forming(self, timestamp, open_price) -> None:
        self._forming = MockBar(timestamp, open_price, open_price, open_price, open_price)

    def form_bar_to(self, timestamp, price) -> None:
        if self._forming is None:
            self.start_forming(timestamp, price)
            return
        b = self._forming
        b.Time = timestamp
        b.Close = price
        b.High = max(b.High, price)
        b.Low = min(b.Low, price)

    def close_forming(self, next_timestamp, next_open) -> None:
        if self._forming is not None:
            self._closed.append(self._forming)
            self._forming = None
        self.start_forming(next_timestamp, next_open)

    def LoadMoreHistoryAsync(self) -> None:
        self.load_requested = True

    def fire_history_loaded(self, count: int) -> None:
        class _Args:
            def __init__(self, n):
                self.Count = n
        self.HistoryLoaded.fire(_Args(count))


class MockSymbol:
    def __init__(self, name="XAUUSD", digits=2, pip_size=0.01, tick_size=0.01,
                 spread_points=20.0):
        self.Name = name
        self.Digits = digits
        self.PipSize = pip_size
        self.TickSize = tick_size
        self.Point = tick_size
        self._spread_points = spread_points
        self.Bid = 3000.0
        self.Ask = self.Bid + spread_points * tick_size

    @property
    def Spread(self) -> float:
        return self._spread_points

    def QuantityToVolumeInUnits(self, lots: float) -> float:
        # XAUUSD: 1 unit = 1 centilot (0.01 lot)
        return lots * 100.0

    def VolumeInUnitsToQuantity(self, units: float) -> float:
        return units / 100.0

    def NormalizePrice(self, price: float) -> float:
        factor = 10 ** self.Digits
        return round(price * factor) / factor


class MockServer:
    def __init__(self, now: Optional[datetime] = None):
        self._now = now or datetime(2026, 9, 18, 9, 0, 0, tzinfo=timezone.utc)

    @property
    def Time(self) -> datetime:
        return self._now

    def set_time(self, dt: datetime) -> None:
        self._now = dt


# --- positions / account ----------------------------------------------------

class MockPosition:
    _ids = itertools.count(1000)

    def __init__(self, symbol_name, trade_type, quantity, entry_price, label,
                 stop_loss=None, take_profit=None, entry_time=None):
        self.Id = next(MockPosition._ids)
        self.SymbolName = symbol_name
        self.TradeType = trade_type
        self.Quantity = quantity  # lots
        self.VolumeInUnits = int(quantity * 100)
        self.EntryPrice = entry_price
        self.StopLoss = stop_loss or 0.0
        self.TakeProfit = take_profit or 0.0
        self.EntryTime = entry_time or datetime.now(timezone.utc)
        self.Label = label
        self.Comment = ""
        self.CurrentPrice = entry_price
        self.GrossProfit = 0.0
        self.Commissions = 0.0
        self.Swap = 0.0
        self.NetProfit = 0.0
        self.IsClosed = False
        self._entry_commission = 0.0

    def ModifyStopLossPrice(self, price) -> None:
        self.StopLoss = price

    def ModifyTakeProfitPrice(self, price) -> None:
        self.TakeProfit = price


class MockCloseArgs:
    def __init__(self, position, reason):
        self.Position = position
        self.Reason = reason


class MockPositions:
    def __init__(self):
        self._open: List[MockPosition] = []
        self.Closed = MockEvent()
        self.Opened = MockEvent()

    def __iter__(self):
        return iter(list(self._open))

    @property
    def Count(self) -> int:
        return len(self._open)

    def FindAll(self, label):
        return [p for p in self._open if p.Label == label]

    def Find(self, label):
        found = self.FindAll(label)
        return found[0] if found else None

    def add(self, pos: MockPosition) -> None:
        self._open.append(pos)

        class _Args:
            def __init__(self, p):
                self.Position = p

        self.Opened.fire(_Args(pos))

    def remove(self, pos: MockPosition) -> None:
        if pos in self._open:
            self._open.remove(pos)

    def fire_closed(self, pos: MockPosition, reason) -> None:
        self.Closed.fire(MockCloseArgs(pos, reason))


class MockAccount:
    def __init__(self, balance=10_000.0, leverage=100, number=12345678, is_live=False):
        self.Balance = balance
        self.Equity = balance
        self.FreeMargin = balance
        self.Margin = 0.0
        self.PreciseLeverage = leverage
        self.Number = number
        self.IsLive = is_live
        self.BrokerName = "XM (mock)"
        self.AccountType = "Standard"


class MockTradeResult:
    def __init__(self, is_successful, error, position, is_executing=False):
        self.IsSuccessful = is_successful
        self.Error = error
        self.Position = position
        self.IsExecuting = is_executing


# --- the api object + broker driver -----------------------------------------

class MockApi:
    """The `api` global the cBot code runs against."""

    def __init__(self, broker: "MockBroker", params: dict):
        self._broker = broker
        self.Symbol = broker.symbol
        self.SymbolName = broker.symbol.Name
        self.Bars = broker.bars
        self.Account = broker.account
        self.Positions = broker.positions
        self.Server = broker.server
        self.InstanceId = "mock-instance-001"
        self.Label = str(params.get("Label", "XAUUSDcBot"))
        self.TimeFrame = TIMEFRAME_BY_MINUTES.get(int(params.get("TimeframeMinutes", 15)), TimeFrame.M15)
        self._prints: List[str] = []
        self.stopped = False
        for key, value in params.items():
            setattr(self, key, value)

    def Print(self, *args) -> None:
        self._prints.append(" | ".join(str(a) for a in args))

    @property
    def printed(self) -> List[str]:
        return self._prints

    def Stop(self) -> None:
        self.stopped = True

    # order operations
    def ExecuteMarketOrder(self, trade_type, symbol_name, volume, label=None,
                           sl_pips=None, tp_pips=None, *rest):
        return self._broker.execute_market_order(
            trade_type, symbol_name, volume, label, sl_pips, tp_pips
        )

    def ClosePosition(self, position, volume=None):
        return self._broker.close_position_now(position)

    def ModifyPosition(self, position, sl_price=None, tp_price=None, *rest):
        if sl_price is not None:
            position.ModifyStopLossPrice(sl_price)
        if tp_price is not None:
            position.ModifyTakeProfitPrice(tp_price)


class MockBroker:
    """The test driver: market state + broker-side order/SL/TP mechanics."""

    def __init__(
        self,
        *,
        starting_balance: float = 10_000.0,
        price: float = 3000.0,
        spread_points: float = 20.0,
        commission_per_million: float = 30.0,
        leverage: int = 100,
        contract_size: float = 100.0,
        is_live: bool = False,
        start_time: Optional[datetime] = None,
        params: Optional[dict] = None,
    ):
        self.params = params or {}
        self.symbol = MockSymbol(spread_points=spread_points)
        self.bars = MockBars()
        self.account = MockAccount(balance=starting_balance, leverage=leverage, is_live=is_live)
        self.positions = MockPositions()
        self.server = MockServer(start_time or datetime(2026, 9, 18, 9, 0, 0, tzinfo=timezone.utc))
        self.contract_size = contract_size
        self.commission_per_million = commission_per_million
        self.reject_next_order = False
        self.orders_placed = 0
        self.symbol.Bid = price
        self.symbol.Ask = price + spread_points * self.symbol.TickSize
        self.api = MockApi(self, self.params)

    # -- driver: market movement ------------------------------------------

    @property
    def _point(self) -> float:
        return self.symbol.TickSize

    def _mid(self) -> float:
        return (self.symbol.Bid + self.symbol.Ask) / 2.0

    def tick(self, bid: float, ask: Optional[float] = None) -> None:
        if ask is None:
            ask = bid + self.symbol._spread_points * self._point
        self.symbol.Bid = bid
        self.symbol.Ask = ask
        self.server.set_time(self.server.Time + timedelta(milliseconds=250))
        mid = self._mid()
        self.bars.form_bar_to(self.server.Time, mid)
        self._check_sl_tp(mid)
        self._update_account()

    def close_bar(self, next_open: Optional[float] = None) -> None:
        """Finalize the forming bar and start the next one. The cBot's
        on_bar_closed is driven separately by the test."""
        t = self.server.Time
        t_next = t + timedelta(minutes=15)
        self.server.set_time(t_next)
        self.bars.close_forming(t_next, next_open if next_open is not None else self.symbol.Bid)

    def set_price(self, price: float) -> None:
        self.tick(price)

    # -- broker-side order mechanics ---------------------------------------

    def _commission_for(self, lots: float, fill_price: float) -> float:
        notional = lots * self.contract_size * fill_price
        return notional / 1_000_000.0 * self.commission_per_million

    def execute_market_order(self, trade_type, symbol_name, volume, label=None,
                             sl_pips=None, tp_pips=None) -> MockTradeResult:
        if self.reject_next_order:
            self.reject_next_order = False
            return MockTradeResult(False, ErrorCode.NoMoney, None)
        lots = volume / 100.0
        if lots <= 0:
            return MockTradeResult(False, "InvalidVolume", None)
        if trade_type == TradeType.Buy:
            entry = self.symbol.Ask
        else:
            entry = self.symbol.Bid

        pip = self.symbol.PipSize
        stop_loss = None
        take_profit = None
        if sl_pips:
            stop_loss = entry - sl_pips * pip if trade_type == TradeType.Buy else entry + sl_pips * pip
        if tp_pips:
            take_profit = entry + tp_pips * pip if trade_type == TradeType.Buy else entry - tp_pips * pip

        # broker margin check (fail like a real broker when short)
        required = lots * self.contract_size * entry / self.account.PreciseLeverage
        if required > self.account.FreeMargin:
            return MockTradeResult(False, ErrorCode.NoMoney, None)

        pos = MockPosition(
            symbol_name=symbol_name,
            trade_type=trade_type,
            quantity=lots,
            entry_price=entry,
            label=label,
            stop_loss=stop_loss,
            take_profit=take_profit,
            entry_time=self.server.Time,
        )
        entry_commission = self._commission_for(lots, entry)
        pos.Commissions = -entry_commission
        pos._entry_commission = entry_commission
        pos.CurrentPrice = self._mid()
        self.positions.add(pos)
        self.orders_placed += 1
        self._update_account()
        return MockTradeResult(True, None, pos)

    def close_position_now(self, position: MockPosition) -> bool:
        self._settle_close(position, self._mid(), PositionCloseReason.Closed)
        return True

    def _check_sl_tp(self, mid: float) -> None:
        for pos in list(self.positions._open):
            if pos.IsClosed:
                continue
            if pos.TradeType == TradeType.Buy:
                hit_sl = pos.StopLoss > 0 and self.symbol.Bid <= pos.StopLoss
                hit_tp = pos.TakeProfit > 0 and self.symbol.Bid >= pos.TakeProfit
                # worst-case convention (repo): if both touched in one tick,
                # assume the stop-loss hit first
                if hit_sl:
                    self._settle_close(pos, pos.StopLoss, PositionCloseReason.StopLoss)
                elif hit_tp:
                    self._settle_close(pos, pos.TakeProfit, PositionCloseReason.TakeProfit)
            else:
                hit_sl = pos.StopLoss > 0 and self.symbol.Ask >= pos.StopLoss
                hit_tp = pos.TakeProfit > 0 and self.symbol.Ask <= pos.TakeProfit
                if hit_sl:
                    self._settle_close(pos, pos.StopLoss, PositionCloseReason.StopLoss)
                elif hit_tp:
                    self._settle_close(pos, pos.TakeProfit, PositionCloseReason.TakeProfit)

    def _settle_close(self, pos: MockPosition, exit_price: float, reason) -> None:
        if pos.IsClosed:
            return
        if pos.TradeType == TradeType.Buy:
            gross = (exit_price - pos.EntryPrice) * pos.Quantity * self.contract_size
        else:
            gross = (pos.EntryPrice - exit_price) * pos.Quantity * self.contract_size
        exit_commission = self._commission_for(pos.Quantity, exit_price)
        total_commission = pos._entry_commission + exit_commission
        net = gross - total_commission + pos.Swap
        pos.GrossProfit = gross
        pos.Commissions = -total_commission
        pos.NetProfit = net
        pos.CurrentPrice = exit_price
        pos.IsClosed = True
        self.account.Balance += net
        self.positions.remove(pos)
        self.positions.fire_closed(pos, reason)
        self._update_account()

    def _unrealized(self) -> float:
        mid = self._mid()
        total = 0.0
        for pos in self.positions._open:
            if pos.TradeType == TradeType.Buy:
                total += (mid - pos.EntryPrice) * pos.Quantity * self.contract_size
            else:
                total += (pos.EntryPrice - mid) * pos.Quantity * self.contract_size
        return total

    def _used_margin(self) -> float:
        total = 0.0
        for pos in self.positions._open:
            total += pos.Quantity * self.contract_size * pos.EntryPrice / self.account.PreciseLeverage
        return total

    def _update_account(self) -> None:
        self.account.Equity = self.account.Balance + self._unrealized()
        used = self._used_margin()
        self.account.Margin = used
        self.account.FreeMargin = self.account.Equity - used

    # -- test helpers --------------------------------------------------------

    def preload_history(self, n_closed: int, start_price: float = 3000.0,
                        start_time: Optional[datetime] = None, step_cents: float = 0.5) -> None:
        """Push n closed M15 bars (small deterministic range) and start
        the forming bar — the state the cBot expects at startup."""
        t0 = start_time or self.server.Time
        price = start_price
        for i in range(n_closed):
            ts = t0 - timedelta(minutes=15 * (n_closed - i))
            o = price
            c = price + (step_cents if i % 2 == 0 else -step_cents)
            h = max(o, c) + 0.5
            lo = min(o, c) - 0.5
            self.bars.push_closed(MockBar(ts, o, h, lo, c))
            price = c
        self.bars.start_forming(t0, self.symbol.Bid)

    def open_position_for_adopt_test(self, direction=TradeType.Buy, lots=0.02,
                                     stop_distance=10.0, label=None) -> MockPosition:
        """Seed an open position (simulates a restart while in a trade)."""
        label = label if label is not None else self.api.Label
        trade_type = TradeType.Buy if direction == TradeType.Buy else TradeType.Sell
        pos = self.execute_market_order(trade_type, self.symbol.Name, lots * 100, label,
                                        stop_distance / self.symbol.PipSize).Position
        return pos
