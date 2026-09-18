"""XAUUSDcBot — main cBot entry point (cTrader Python cBot).

This is the cTrader-side counterpart of xauusd_bot's orchestrator/
bot_runner.py + market_data/ctrader_feed.py + execution/ctrader_executor.py.
In the external process those were separate classes; in cTrader the
platform IS the feed and the order gateway, so this file provides:

  - XAUUSDcBot — the algo class the cTrader engine instantiates. Its
    event handlers (on_start / on_bar_closed / on_tick / on_stop) drive
    the ported BotStateMachine: CLOSED candles go through on_candle()
    (strategy + filters + risk gate + entry), TICKS go through
    on_monitor_tick() (mark-to-market, kill switch, exits, trailing).
  - CTraderExecution — the ExecutionInterface adapter onto cTrader's
    API. The state machine code is identical to the repo's whether the
    executor is paper, the Open API client, or this adapter.

Everything else (risk engine, filters, monitors, strategies, audit)
lives in the sibling modules ported verbatim from xauusd_bot.
"""

import clr

clr.AddReference("cAlgo.API")

# Import cAlgo API types
from cAlgo.API import *  # noqa: F401,F403  (TradeType, TimeFrame, PositionCloseReason, ...)

# Import trading wrapper functions
from robot_wrapper import *  # noqa: F401,F403

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from audit import AuditTrail
from bot_state_machine import BotStateMachine
from config_schema import (
    BrokerConfig,
    BotConfig,
    ConfigValidationError,
    FilterConfig,
    KillSwitchConfig,
    RiskConfig,
)
from core_enums import Direction, EventType, ExitReason, SystemState
from core_events import BotEvent
from core_models import Candle, Position, Trade, TradeRequest, net_dt_to_utc, now_utc
from execution_interface import (
    BrokerConnectionError,
    ExecutionError,
    ExecutionInterface,
    OrderRejectedError,
    PositionNotFoundError,
)
from position_monitor import TrailingStopState, compute_unrealized_pnl
from strategies import STRATEGY_NAMES, build_strategy

# cBot chart-timeframe -> TimeFrame enum. The "Timeframe (minutes)"
# parameter must match the cBot instance's own timeframe, or the
# strategy's candle math is silently wrong — so boot fails loudly.
_TIMEFRAME_BY_MINUTES = {
    1: TimeFrame.M1,
    5: TimeFrame.M5,
    15: TimeFrame.M15,
    30: TimeFrame.M30,
    60: TimeFrame.H1,
    240: TimeFrame.H4,
    1440: TimeFrame.D1,
}

_HISTORY_LOAD_TIMEOUT_SECONDS = 180


# =====================================================================
# ExecutionInterface adapter onto cTrader
# =====================================================================

class CTraderExecution(ExecutionInterface):
    """Bridges the ported state machine onto cTrader's trading API.

    Conventions kept from xauusd_bot/execution/ctrader_executor.py:
      - every failure RAISES a specific ExecutionError subclass — never
        returns a sentinel, never retries silently;
      - the Position returned to the state machine carries the BROKER'S
        own reported values (fill price, stop-loss level, volume), never
        the values we sent;
      - a mandatory stop-loss: if the broker does not end up with a
        protective level on the position, that is an ExecutionError.
    """

    def __init__(self, api, symbol_name: str, label: str, contract_size: float):
        self.api = api
        self.symbol_name = symbol_name
        self.label = label
        self.contract_size = contract_size
        self.equity_at_entry: float = 0.0

    # -- volume / price helpers ------------------------------------------

    def _volume_in_units(self, lots: float) -> float:
        return self.api.Symbol.QuantityToVolumeInUnits(lots)

    def _floor_pips(self, price_distance: float) -> float:
        """Floor a price distance to a 2-decimal pip count. Flooring
        (never rounding up) keeps the broker's stop at least as close
        to entry as the strategy requested — live risk can only ever be
        less than or equal to the calculated risk."""
        return math.floor(price_distance * 100.0) / 100.0

    def _norm_price(self, price: float) -> float:
        f = getattr(self.api.Symbol, "NormalizePrice", None)
        if callable(f):
            try:
                return float(f(price))
            except Exception:
                pass
        digits = int(getattr(self.api.Symbol, "Digits", 2))
        factor = 10 ** digits
        return math.floor(price * factor + 0.5) / factor

    def _pips(self, entry_price: float, level_price: float) -> float:
        pip = float(self.api.Symbol.PipSize)
        if pip <= 0:
            raise ExecutionError("symbol PipSize is not positive — cannot express stop in pips")
        return self._floor_pips(abs(entry_price - level_price) / pip)

    # -- position mapping --------------------------------------------------

    def _position_from_live(self, live) -> Position:
        direction = Direction.LONG if live.TradeType == TradeType.Buy else Direction.SHORT
        stop_loss = float(live.StopLoss)
        if stop_loss <= 0:
            raise ExecutionError(
                "broker did not attach a stop-loss to the position — "
                "the mandatory-stop invariant is violated; refusing to track it"
            )
        raw_tp = getattr(live, "TakeProfit", 0.0)
        take_profit = float(raw_tp) if raw_tp and float(raw_tp) > 0 else None
        return Position(
            symbol=live.SymbolName,
            direction=direction,
            lot_size=float(live.Quantity),
            entry_price=float(live.EntryPrice),
            stop_loss=stop_loss,
            take_profit=take_profit,
            opened_at=net_dt_to_utc(live.EntryTime),
            broker_ticket=str(live.Id),
        )

    def find_live(self, position: Position):
        """The live cTrader Position object for one of ours, or None."""
        for live in self.api.Positions.FindAll(self.label):
            if str(live.Id) == str(position.broker_ticket):
                return live
        return None

    def _exit_price(self, live, position: Position, reference_price: float) -> float:
        """Best-effort exit price for audit records. cTrader does not
        expose a single 'exit price' field on a closed position, so
        derive it from the broker's own reported net P&L: the price at
        which that P&L would exactly hold. Consistent with the
        broker-reported pnl by construction; swap/commission effects
        are already inside the broker's NetProfit."""
        denom = float(live.Quantity) * self.contract_size
        if denom <= 0:
            return reference_price
        pnl = self._net_pnl(live)
        if live.TradeType == TradeType.Buy:
            return float(live.EntryPrice) + pnl / denom
        return float(live.EntryPrice) - pnl / denom

    @staticmethod
    def _net_pnl(live) -> float:
        value = getattr(live, "NetProfit", None)
        if value is not None:
            try:
                return float(value)
            except Exception:
                pass
        # fallback: gross - commissions + swap
        gross = float(getattr(live, "GrossProfit", 0.0) or 0.0)
        commissions = float(getattr(live, "Commissions", 0.0) or 0.0)
        swap = float(getattr(live, "Swap", 0.0) or 0.0)
        return gross - abs(commissions) + swap

    # -- ExecutionInterface ------------------------------------------------

    def place_order(self, trade_request: TradeRequest) -> Position:
        signal = trade_request.signal
        trade_type = TradeType.Buy if signal.direction == Direction.LONG else TradeType.Sell
        volume = self._volume_in_units(trade_request.lot_size)
        if volume <= 0:
            raise ExecutionError(f"volume converted to {volume} units — refusing to order")

        sl_pips = self._pips(signal.entry_price, signal.stop_loss)
        if sl_pips <= 0:
            raise ExecutionError(
                f"stop-loss distance {abs(signal.entry_price - signal.stop_loss)} is smaller than "
                "one pip — cannot attach a protective stop; refusing to open unprotected"
            )
        tp_pips = None
        if signal.take_profit is not None:
            tp_pips = self._pips(signal.entry_price, signal.take_profit)

        result = self.api.ExecuteMarketOrder(
            trade_type, self.symbol_name, volume, self.label, sl_pips, tp_pips
        )
        if getattr(result, "IsSuccessful", False) is not True:
            raise OrderRejectedError(f"broker rejected market order: {getattr(result, 'Error', 'unknown')}")
        live = getattr(result, "Position", None)
        if live is None or getattr(result, "IsExecuting", False):
            raise BrokerConnectionError(
                "order result ambiguous (still executing / no position returned) — "
                "not handling silently, per the no-silent-retries rule"
            )

        position = self._position_from_live(live)
        self.equity_at_entry = float(self.api.Account.Equity)
        return position

    def close_position(self, position: Position, exit_reason: ExitReason, reference_price: float) -> Trade:
        live = self.find_live(position)
        if live is None:
            raise PositionNotFoundError(
                f"broker has no open position {position.broker_ticket} for label {self.label!r} — "
                "it may have been closed externally; reconciliation will record it"
            )
        ok = self.api.ClosePosition(live)
        if ok is not True:
            raise OrderRejectedError("broker rejected the position close")

        pnl = self._net_pnl(live)
        commission = abs(float(getattr(live, "Commissions", 0.0) or 0.0))
        exit_price = self._exit_price(live, position, reference_price)
        closed_at = now_utc()
        equity_before = self.equity_at_entry if self.equity_at_entry > 0 else float(self.api.Account.Balance)

        return Trade(
            symbol=position.symbol,
            direction=position.direction,
            lot_size=position.lot_size,
            entry_price=position.entry_price,
            exit_price=exit_price,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            opened_at=position.opened_at,
            closed_at=closed_at,
            pnl=pnl,
            pnl_percent=(pnl / equity_before) if equity_before > 0 else 0.0,
            exit_reason=exit_reason,
            commission=commission,
            slippage_points=0.0,
        )

    def modify_sl_tp(
        self,
        position: Position,
        *,
        new_stop_loss=None,
        new_take_profit=None,
    ) -> Position:
        live = self.find_live(position)
        if live is None:
            raise PositionNotFoundError(
                f"cannot modify: broker has no position {position.broker_ticket} for label {self.label!r}"
            )
        if new_stop_loss is not None:
            live.ModifyStopLossPrice(self._norm_price(float(new_stop_loss)))
        if new_take_profit is not None:
            live.ModifyTakeProfitPrice(self._norm_price(float(new_take_profit)))
        return self._position_from_live(live)

    def get_open_position(self, symbol: str):
        for live in self.api.Positions.FindAll(self.label):
            if live.SymbolName == symbol:
                return self._position_from_live(live)
        return None

    def is_connected(self) -> bool:
        # cTrader has no separate API link to lose: the cBot lives inside
        # the terminal. The kill switch's disconnect trigger is therefore
        # inert here (the max-drawdown trigger is the live protection).
        return True


# =====================================================================
# The cBot
# =====================================================================

class XAUUSDcBot():
    # === Fields ===
    def on_start(self):
        self.sm = None
        self.audit = None
        self.execution = None
        self._history_ready = False
        self._history_deadline = None
        self._history_loaded_fired = False

        # 1. Validate EVERYTHING before trading anything.
        try:
            config = self._build_config()
        except ConfigValidationError as exc:
            api.Print(f"CONFIG INVALID — not starting: {exc}")
            api.Stop()
            return
        except Exception as exc:
            api.Print(f"CONFIG BUILD ERROR — not starting: {exc}")
            api.Stop()
            return

        # 2. Timeframe sanity: the strategy's candle math assumes the
        #    "Timeframe (minutes)" parameter matches this instance's TF.
        expected_tf = _TIMEFRAME_BY_MINUTES.get(int(api.TimeframeMinutes))
        if expected_tf is None:
            api.Print(f"TIMEFRAME {api.TimeframeMinutes} min is not supported — supported: "
                      f"{sorted(_TIMEFRAME_BY_MINUTES)}. Not starting.")
            api.Stop()
            return
        if api.TimeFrame != expected_tf:
            api.Print(f"TIMEFRAME MISMATCH — parameter says {api.TimeframeMinutes}m but this "
                      f"instance runs on {api.TimeFrame}. Set the cBot's chart timeframe to "
                      f"match. Not starting.")
            api.Stop()
            return

        # 3. Symbol sanity.
        if api.ExpectedSymbol and api.SymbolName.upper() != str(api.ExpectedSymbol).upper():
            api.Print(f"SYMBOL MISMATCH — parameter expects {api.ExpectedSymbol!r}, "
                      f"chart symbol is {api.SymbolName!r}. Not starting.")
            api.Stop()
            return

        # 4. Strategy (ported verbatim from xauusd_bot/strategy/).
        try:
            strategy = build_strategy(api.Strategy, self._params())
        except Exception as exc:
            api.Print(f"STRATEGY SETUP FAILED ({api.Strategy!r}): {exc}. Not starting.")
            api.Stop()
            return

        # 5. Audit trail (SQLite + file + console, same policy as the repo).
        audit_dir = Path(api.AuditDir) if api.AuditDir else self._default_audit_dir()
        try:
            audit = AuditTrail(audit_dir=audit_dir, print_fn=api.Print, strict=bool(api.StrictPersistence))
        except Exception as exc:
            api.Print(f"AUDIT TRAIL UNAVAILABLE: {exc}. Not starting — the repo's policy is "
                      f"to halt rather than trade while blind.")
            api.Stop()
            return

        # 6. Wire the state machine (identical code to xauusd_bot's).
        environment = "live" if api.Account.IsLive else "paper"
        starting_balance = float(api.Account.Equity)
        execution = CTraderExecution(api, api.SymbolName, api.Label, config.broker.contract_size)
        sm = BotStateMachine(
            strategy=strategy,
            config=config,
            execution=execution,
            starting_balance=starting_balance,
            symbol=api.SymbolName,
            event_logger=audit,
            trailing_stop_atr_multiplier=(
                float(api.TrailingStopAtrMultiplier) if api.TrailingStopEnabled else None
            ),
            trailing_stop_atr_period=int(api.TrailingStopAtrPeriod),
            hooks={
                "signal": lambda s: audit.record_signal(s),
                "rejection": lambda r: audit.record_rejected_signal(r),
                "trade": lambda t: audit.record_trade(t),
                "position_open": lambda p: audit.upsert_position(p, is_open=True),
                "position_close": lambda p: audit.upsert_position(p, is_open=False),
            },
            broker_enforced_exits=True,
        )

        audit.log(BotEvent(
            event_type=EventType.BOOT,
            message=(f"cBot started ({environment}) symbol={api.SymbolName} "
                     f"strategy={strategy.name} equity={starting_balance:.2f} "
                     f"audit={audit_dir}"),
            payload={"environment": environment, "equity": starting_balance,
                     "strategy": strategy.name},
        ))

        # 7. Startup reconciliation: adopt any open position this bot
        #    previously opened (the repo's "positions table is reconciled
        #    against the broker on boot" rule).
        existing = execution.get_open_position(api.SymbolName)
        if existing is not None:
            current_price = (float(api.Symbol.Bid) + float(api.Symbol.Ask)) / 2.0
            floating = compute_unrealized_pnl(existing, current_price, config.broker.contract_size)
            sm.account_tracker.balance = starting_balance - floating
            sm.account_tracker.roll_daily_if_needed(net_dt_to_utc(api.Server.Time))
            sm.open_position = existing
            sm.state = SystemState.MONITORING
            sm._trailing_stop_state = (
                TrailingStopState.initial(existing)
                if (sm.trailing_stop_atr_multiplier is not None and existing.take_profit is None)
                else None
            )
            audit.upsert_position(existing, is_open=True)
            audit.log(BotEvent(
                event_type=EventType.ERROR,
                message=(f"adopted existing position {existing.broker_ticket} "
                         f"({existing.direction.value} {existing.lot_size} @ {existing.entry_price}) "
                         f"at startup"),
            ))

        # 8. Broker close events (SL/TP hits arrive here, not through us).
        api.Positions.Closed += self.on_broker_position_closed

        # 9. History preload so strategies can warm up immediately.
        self.sm = sm
        self.audit = audit
        self.execution = execution
        self._config = config
        if int(api.HistoryPreloadCandles) > 0 and api.Bars.Count < int(api.HistoryPreloadCandles):
            api.Bars.HistoryLoaded += self.on_bars_history_loaded
            api.Bars.LoadMoreHistoryAsync()
            self._history_deadline = now_utc() + timedelta(seconds=_HISTORY_LOAD_TIMEOUT_SECONDS)
        else:
            self._finalize_history()

    # --- event handlers ----------------------------------------------------

    def on_bar_closed(self):
        if not self._core_ready():
            return
        if not self._history_ready:
            self._maybe_finalize_history()
            return
        try:
            candle = self._candle_from_bar(api.Bars.LastBar)
            self.sm.on_candle(candle)
            self._post_bar_audit(candle)
        except Exception as exc:
            self._log_error(f"on_bar_closed failed: {exc!r}")

    def on_tick(self):
        if not self._core_ready():
            return
        if not self._history_ready:
            self._maybe_finalize_history()
            return
        try:
            candle = self._forming_bar_candle()
            self.sm.reconcile_open_position(candle.close)
            self.sm.on_monitor_tick(candle, broker_connected=self.sm.execution.is_connected())
        except Exception as exc:
            self._log_error(f"on_tick failed: {exc!r}")

    def on_stop(self):
        if self.audit is not None:
            try:
                self.audit.log(BotEvent(
                    event_type=EventType.STATE_TRANSITION,
                    message="cBot stopped by user — open positions (if any) keep their "
                            "broker-side stop-loss/take-profit; nothing is auto-closed",
                ))
            except Exception:
                pass
            try:
                self.audit.close()
            except Exception:
                pass

    # --- broker event handlers ----------------------------------------------

    def on_broker_position_closed(self, args):
        """Positions.Closed event: SL/TP/stop-out/manual closes the broker
        performed on OUR positions. The state machine's own closes are
        already recorded synchronously, so handle_external_close()
        returns False for those — no double counting."""
        if not self._core_ready():
            return
        try:
            pos = args.Position
            if getattr(pos, "Label", None) != api.Label:
                return
            reason = getattr(args, "Reason", None)
            if reason == PositionCloseReason.StopLoss:
                exit_reason = ExitReason.STOP_LOSS
            elif reason == PositionCloseReason.TakeProfit:
                exit_reason = ExitReason.TAKE_PROFIT
            elif reason == PositionCloseReason.StopOut:
                exit_reason = ExitReason.KILL_SWITCH
            else:
                exit_reason = ExitReason.MANUAL

            pnl = CTraderExecution._net_pnl(pos)
            commission = abs(float(getattr(pos, "Commissions", 0.0) or 0.0))
            tracked = None
            if self.sm.open_position is not None:
                tracked = self.sm.open_position
            reference = (float(api.Symbol.Bid) + float(api.Symbol.Ask)) / 2.0
            exit_price = (
                (float(pos.EntryPrice) + pnl / (float(pos.Quantity) * self._config.broker.contract_size))
                if pos.TradeType == TradeType.Buy
                else (float(pos.EntryPrice) - pnl / (float(pos.Quantity) * self._config.broker.contract_size))
            ) if float(pos.Quantity) > 0 else reference

            handled = self.sm.handle_external_close(
                broker_ticket=str(pos.Id),
                exit_price=exit_price,
                exit_reason=exit_reason,
                pnl=pnl,
                commission=commission,
                closed_at=now_utc(),
            )
            if handled:
                if self.sm.trades:
                    self.audit.record_trade(self.sm.trades[-1])
                self.audit.upsert_position(tracked, is_open=False) if tracked else None
                self.audit.log(BotEvent(
                    event_type=EventType.POSITION_CLOSED,
                    message=f"broker close event consumed ({exit_reason.value}) pnl={pnl:.2f}",
                ))
        except Exception as exc:
            self._log_error(f"on_broker_position_closed failed: {exc!r}")

    def on_bars_history_loaded(self, args):
        self._history_loaded_fired = True

    # --- helpers ---------------------------------------------------------------

    def _core_ready(self) -> bool:
        return self.sm is not None and self.audit is not None

    def _params(self) -> dict:
        names = [
            "Strategy",
            # placeholder
            "PhInterval", "PhStopDistance", "PhTakeProfitDistance",
            # trend_pullback_h1_m15
            "TpHtfMinutes", "TpHtfFastEma", "TpHtfSlowEma", "TpLtfFastEma", "TpAtrPeriod",
            "TpAtrStopMult", "TpRewardRisk", "TpRsiPeriod", "TpRsiLongMin", "TpRsiLongMax",
            "TpRsiShortMin", "TpRsiShortMax", "TpMinAtrPrice",
            # trend_pullback_breakout_v2
            "T2HtfMinutes", "T2HtfFastEma", "T2HtfSlowEma", "T2HtfAtrPeriod",
            "T2MinTrendStrengthAtr", "T2LtfFastEma", "T2MinPullbackBars", "T2MaxPullbackBars",
            "T2AtrPeriod", "T2StopBufferAtrMult", "T2RewardRisk", "T2RsiPeriod", "T2RsiLongMin",
            "T2RsiLongMax", "T2RsiShortMin", "T2RsiShortMax", "T2UseTrailingStop",
            # mean_reversion_v1
            "MrEmaPeriod", "MrAtrPeriod", "MrEntryThresholdAtr", "MrStopAtrMult", "MrHtfMinutes",
            "MrHtfFastEma", "MrHtfSlowEma", "MrHtfAtrPeriod", "MrMaxHtfTrendStrength",
        ]
        return {name: getattr(api, name) for name in names}

    def _build_config(self) -> BotConfig:
        risk = RiskConfig(
            default_risk_percent=float(api.DefaultRiskPercent),
            max_risk_percent=float(api.MaxRiskPercent),
            max_open_positions=1,  # hard cap, enforced by the original schema too
            daily_loss_limit_percent=float(api.DailyLossLimitPercent),
            max_drawdown_percent=float(api.MaxDrawdownPercent),
            max_consecutive_losses=int(api.MaxConsecutiveLosses),
            consecutive_loss_cooldown_hours=int(api.ConsecutiveLossCooldownHours),
            margin_utilization_cap=float(api.MarginUtilizationCap),
            min_lot_risk_tolerance=float(api.MinLotRiskTolerance),
        )
        sessions = [s.strip() for s in str(api.AllowedSessions or "").split("|") if s.strip()]
        filters = FilterConfig(
            max_spread_points=float(api.MaxSpreadPoints),
            duplicate_order_debounce_seconds=int(api.DuplicateOrderDebounceSeconds),
            allowed_sessions=sessions if sessions else None,
        )
        broker = BrokerConfig(
            symbol=str(api.ExpectedSymbol or api.SymbolName),
            leverage=int(api.Leverage),
            contract_size=float(api.ContractSize),
            digits=int(api.Digits),
            lot_step=float(api.LotStep),
            min_lot=float(api.MinLot),
            max_lot=float(api.MaxLot),
            margin_rate=float(api.MarginRate),
            commission_per_million_usd=float(api.CommissionPerMillionUsd),
            account_currency_unit=str(api.AccountCurrencyUnit or "usd"),
            unit_scale_factor=float(api.UnitScaleFactor),
            swap_enabled=bool(api.SwapEnabled),
            swap_long_points=float(api.SwapLongPoints),
            swap_short_points=float(api.SwapShortPoints),
        )
        kill = KillSwitchConfig(
            manual_trigger_enabled=bool(api.ManualTriggerEnabled),
            manual_rearm_required=bool(api.ManualRearmRequired),
            trigger_on_broker_disconnect=bool(api.TriggerOnBrokerDisconnect),
            trigger_on_max_drawdown=bool(api.TriggerOnMaxDrawdown),
        )
        environment = "live" if api.Account.IsLive else "paper"
        return BotConfig(environment=environment, risk=risk, filters=filters,
                         broker=broker, kill_switch=kill)

    def _default_audit_dir(self) -> Path:
        account_number = str(getattr(api.Account, "Number", "unknown"))
        return Path.home() / "Documents" / "XAUUSDcBot-audit" / account_number / str(api.Label)

    def _server_now(self):
        return net_dt_to_utc(api.Server.Time)

    def _candle_from_bar(self, bar, close_price=None) -> Candle:
        symbol = api.Symbol
        close = float(close_price) if close_price is not None else float(bar.Close)
        spread_points = float(symbol.Spread)
        return Candle(
            timestamp=net_dt_to_utc(bar.Time),
            open=float(bar.Open),
            high=float(bar.High),
            low=float(bar.Low),
            close=close,
            volume=float(getattr(bar, "Volume", 0.0)),
            spread_points=spread_points,
        )

    def _forming_bar_candle(self) -> Candle:
        """The still-open bar as a Candle, mid-priced like the original
        cTrader feed (bid/ask midpoint)."""
        symbol = api.Symbol
        mid = (float(symbol.Bid) + float(symbol.Ask)) / 2.0
        return self._candle_from_bar(api.Bars.LastBar, close_price=mid)

    def _maybe_finalize_history(self):
        if self._history_loaded_fired or (
            self._history_deadline is not None and now_utc() >= self._history_deadline
        ):
            self._finalize_history()

    def _finalize_history(self):
        """Seed the state machine's history with CLOSED bars only (the
        last bar may still be forming, so it is excluded — it will be
        appended by on_bar_closed once it actually closes)."""
        bars = api.Bars
        n = bars.Count
        for i in range(0, max(0, n - 1)):
            self.sm.history.append(self._candle_from_bar(bars[i]))
        if n > 1:
            self.sm.account_tracker.roll_daily_if_needed(self._server_now())
        self._history_ready = True
        api.Print(f"History ready: {len(self.sm.history)} closed candles loaded")

    def _post_bar_audit(self, candle: Candle):
        tracker = self.sm.account_tracker
        account = tracker.get_live_account_state(
            open_position=self.sm.open_position,
            current_price=candle.close,
            broker=self._config.broker,
            consecutive_losses=self.sm.consecutive_loss_tracker.consecutive_losses,
        )
        if tracker.current_trading_day is None:
            return
        day = tracker.current_trading_day
        trades_today = sum(
            1 for t in self.sm.trades
            if t.closed_at is not None and t.closed_at.date() == day
        )
        self.audit.record_snapshot(account)
        self.audit.upsert_daily_stats(
            trading_day=day,
            start_equity=tracker.daily_start_equity,
            end_equity=account.equity,
            pnl=account.equity - tracker.daily_start_equity,
            trades_count=trades_today,
            daily_loss_limit_hit=(
                account.daily_loss_percent >= self._config.risk.daily_loss_limit_percent
            ),
        )

    def _log_error(self, message: str):
        # One bad tick must not take the bot down; but per the repo's
        # "never hide errors" rule it is recorded loudly to the audit.
        api.Print(f"[ERROR] {message}")
        if self.audit is not None:
            try:
                self.audit.error(message)
            except Exception:
                pass
