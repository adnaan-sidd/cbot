"""
Paper executor (architecture doc §10 — "Support demo/paper trading
before live deployment").

Implements ExecutionInterface with simulated fills, reusing the EXACT
cost/slippage model classes from Phase 3's backtester — the same
apply_entry_spread/apply_slippage/CostModel code, not a reimplementation.
The only thing that's different from backtesting is WHEN market data
arrives: a backtest owns its feed and iterates it; a paper (or live)
executor receives price updates pushed to it externally via
update_market_price(), since it's meant to sit behind a real-time state
machine reacting to a live-arriving feed.
"""

from __future__ import annotations

from typing import Optional

from backtesting.cost_model import CostModel, apply_entry_spread, apply_slippage
from backtesting.slippage_model import SlippageModel
from backtesting.swap_model import calculate_swap_cost
from config.config_schema import BrokerConfig
from core.enums import ExitReason
from core.models import Candle, Position, Trade, TradeRequest
from execution.execution_interface import (
    BrokerConnectionError,
    ExecutionInterface,
    OrderRejectedError,
    PositionNotFoundError,
)


class PaperExecutor(ExecutionInterface):
    def __init__(self, *, broker: BrokerConfig, cost_model: CostModel, slippage_model: SlippageModel):
        self.broker = broker
        self.cost_model = cost_model
        self.slippage_model = slippage_model
        self._current_candle: Optional[Candle] = None
        self._open_positions: dict[str, Position] = {}
        self._entry_commissions: dict[str, float] = {}  # keyed by position.id

    def update_market_price(self, candle: Candle) -> None:
        """Called by the state machine on every new candle/tick — paper
        fills always use the most recently pushed price, never a price
        from the future relative to when this is called."""
        self._current_candle = candle

    def place_order(self, trade_request: TradeRequest) -> Position:
        if self._current_candle is None:
            raise BrokerConnectionError("no market price available yet — cannot simulate a fill")

        signal = trade_request.signal
        if signal.symbol in self._open_positions:
            # Defensive: risk_gate/max_open_positions should already
            # prevent this, but the executor never trusts an upstream
            # invariant blindly for something this consequential.
            raise OrderRejectedError(f"a position is already open for {signal.symbol}")

        spread_points = self._current_candle.spread_points or 0.0
        entry_price = apply_entry_spread(
            signal.entry_price, signal.direction, spread_points, self.broker.point_size
        )
        slippage = self.slippage_model.get_slippage_price(self._current_candle)
        entry_price = apply_slippage(entry_price, signal.direction, is_entry=True, slippage_amount=slippage)

        position = Position(
            symbol=signal.symbol,
            direction=signal.direction,
            lot_size=trade_request.lot_size,
            entry_price=entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            opened_at=self._current_candle.timestamp,
            broker_ticket=f"paper-{trade_request.id}",
        )
        self._open_positions[signal.symbol] = position
        self._entry_commissions[position.id] = self.cost_model.commission_for_notional(
            lot_size=position.lot_size, fill_price=entry_price, contract_size=self.broker.contract_size
        )
        return position

    def close_position(self, position: Position, exit_reason: ExitReason, reference_price: float) -> Trade:
        if self._current_candle is None:
            raise BrokerConnectionError("no market price available yet — cannot simulate a close")
        tracked = self._open_positions.get(position.symbol)
        if tracked is None or tracked.id != position.id:
            raise PositionNotFoundError(f"no tracked open position matching {position.id} for {position.symbol}")

        slippage = self.slippage_model.get_slippage_price(self._current_candle)
        exit_price = apply_slippage(reference_price, position.direction, is_entry=False, slippage_amount=slippage)

        entry_commission = self._entry_commissions.pop(position.id, 0.0)
        exit_commission = self.cost_model.commission_for_notional(
            lot_size=position.lot_size, fill_price=exit_price, contract_size=self.broker.contract_size
        )
        total_commission = entry_commission + exit_commission

        swap_cost = calculate_swap_cost(
            direction=position.direction,
            lot_size=position.lot_size,
            opened_at=position.opened_at,
            closed_at=self._current_candle.timestamp,
            broker=self.broker,
        )

        price_diff = (
            exit_price - position.entry_price
            if position.direction.value == "long"
            else position.entry_price - exit_price
        )
        gross_pnl = price_diff * position.lot_size * self.broker.contract_size
        net_pnl = gross_pnl - total_commission + swap_cost

        trade = Trade(
            symbol=position.symbol,
            direction=position.direction,
            lot_size=position.lot_size,
            entry_price=position.entry_price,
            exit_price=exit_price,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            opened_at=position.opened_at,
            closed_at=self._current_candle.timestamp,
            pnl=net_pnl,
            pnl_percent=0.0,  # caller (position_monitor/state_machine) knows equity context; left neutral here
            exit_reason=exit_reason,
            commission=total_commission,
            slippage_points=slippage,
        )
        del self._open_positions[position.symbol]
        return trade

    def modify_sl_tp(
        self, position: Position, *, new_stop_loss: Optional[float] = None, new_take_profit: Optional[float] = None
    ) -> Position:
        tracked = self._open_positions.get(position.symbol)
        if tracked is None or tracked.id != position.id:
            raise PositionNotFoundError(f"no tracked open position matching {position.id} for {position.symbol}")

        updated = Position(
            symbol=tracked.symbol,
            direction=tracked.direction,
            lot_size=tracked.lot_size,
            entry_price=tracked.entry_price,
            stop_loss=new_stop_loss if new_stop_loss is not None else tracked.stop_loss,
            take_profit=new_take_profit if new_take_profit is not None else tracked.take_profit,
            opened_at=tracked.opened_at,
            broker_ticket=tracked.broker_ticket,
            id=tracked.id,
        )
        self._open_positions[position.symbol] = updated
        return updated

    def get_open_position(self, symbol: str) -> Optional[Position]:
        return self._open_positions.get(symbol)

    def is_connected(self) -> bool:
        return True  # paper trading has no real connection to lose
