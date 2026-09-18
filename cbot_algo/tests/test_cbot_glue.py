"""End-to-end cBot glue tests.

Runs the ACTUAL main module (XAUUSD cBot_main.py — loaded exactly as
cTrader would exec it) against the mock cTrader platform. These tests
exercise the full live loop that the external cbot process used to
provide: closed-candle entries through the real risk gate / filter
chain, broker-enforced SL/TP, mark-to-market monitoring on ticks, the
kill switch, position adoption at restart, trailing stops, history
preload, and the SQLite audit trail read back from disk.
"""

from datetime import datetime, timedelta, timezone

import pytest

from core_enums import ExitReason, SystemState
from strategy_interface import MarketState, Strategy


# ---------------------------------------------------------------------
# boot / validation
# ---------------------------------------------------------------------

class TestBootValidation:
    def test_clean_boot(self, cbot_main, params, tmp_path):
        from conftest import make_broker, start_cbot

        params.update({"Strategy": "placeholder_alternating_TEST_ONLY",
                       "PhInterval": 1, "PhStopDistance": 10.0, "PhTakeProfitDistance": 20.0})
        broker = make_broker(tmp_path, params=params, balance=10_000.0)
        bot = start_cbot(cbot_main, broker)
        assert not broker.api.stopped
        assert bot.sm is not None
        assert bot.sm.state == SystemState.IDLE
        assert bot.sm.history and len(bot.sm.history) == 60  # closed bars only
        from conftest import events_of_type

        assert len(events_of_type(bot, "boot")) >= 1  # BOOT events in the audit trail

    def test_invalid_risk_config_halts_before_any_trading(self, cbot_main, params, tmp_path):
        from conftest import make_broker, start_cbot

        params.update({"DefaultRiskPercent": 0.05})  # above the 0.5% band ceiling
        broker = make_broker(tmp_path, params=params)
        bot = start_cbot(cbot_main, broker)
        assert broker.api.stopped is True
        assert bot.sm is None
        assert any("CONFIG INVALID" in p for p in broker.api.printed)

    def test_timeframe_mismatch_halts(self, cbot_main, params, tmp_path):
        from conftest import make_broker, start_cbot
        from ctrader_api_mock import TimeFrame

        broker = make_broker(tmp_path, params=params)
        broker.api.TimeFrame = TimeFrame.H1  # parameter says 15m, instance is H1
        bot = start_cbot(cbot_main, broker)
        assert broker.api.stopped is True
        assert bot.sm is None
        assert any("TIMEFRAME MISMATCH" in p for p in broker.api.printed)

    def test_symbol_mismatch_halts(self, cbot_main, params, tmp_path):
        from conftest import make_broker, start_cbot

        params.update({"ExpectedSymbol": "EURUSD"})
        broker = make_broker(tmp_path, params=params)
        bot = start_cbot(cbot_main, broker)
        assert broker.api.stopped is True
        assert bot.sm is None


# ---------------------------------------------------------------------
# full trade lifecycle through the real risk gate + broker SL/TP
# ---------------------------------------------------------------------

class OneShotStrategy(Strategy):
    """Fires exactly once, when history reaches `at_history_len`, with a
    fixed stop distance (and optional TP distance) from the close. Used
    to test single-trade lifecycles without re-entry noise."""

    name = "oneshot_test_only"

    def __init__(self, at_history_len, direction="long", stop_distance=10.0,
                 take_profit_distance=20.0):
        self.at_history_len = at_history_len
        self.direction = direction
        self.stop_distance = stop_distance
        self.take_profit_distance = take_profit_distance
        self._fired = False

    def generate_signal(self, market_state: MarketState):
        from core_enums import Direction
        from core_models import Signal

        if self._fired or len(market_state.history) != self.at_history_len:
            return None
        self._fired = True
        close = market_state.current.close
        d = Direction.LONG if self.direction == "long" else Direction.SHORT
        if d == Direction.LONG:
            sl = close - self.stop_distance
            tp = close + self.take_profit_distance if self.take_profit_distance else None
        else:
            sl = close + self.stop_distance
            tp = close - self.take_profit_distance if self.take_profit_distance else None
        return Signal(
            symbol=market_state.symbol, direction=d, entry_price=close,
            stop_loss=sl, take_profit=tp, strategy_name=self.name,
        )


def _lifecycle_env(cbot_main, params, tmp_path, *, balance=10_000.0, **over):
    from conftest import make_broker, start_cbot

    p = dict(params)
    p.update({
        "Strategy": "placeholder_alternating_TEST_ONLY",
        "PhInterval": 1, "PhStopDistance": 10.0, "PhTakeProfitDistance": 20.0,
    })
    p.update(over)
    broker = make_broker(tmp_path, params=p, balance=balance)
    bot = start_cbot(cbot_main, broker)
    return broker, bot


class TestTradeLifecycle:
    def _env(self, cbot_main, params, tmp_path, **kw):
        return _lifecycle_env(cbot_main, params, tmp_path, **kw)

    def test_entry_fill_and_broker_stop_loss_exit_and_audit(self, cbot_main, params, tmp_path):
        from conftest import events_of_type, next_bar

        broker, bot = self._env(cbot_main, params, tmp_path)
        bot.sm.strategy = OneShotStrategy(len(bot.sm.history) + 1, "long", 10.0, 20.0)

        # bar 1: one-shot LONG signal, risk gate approves,
        # broker fills at ask with a protective stop-loss
        next_bar(cbot_main, broker, bot, ticks=())
        assert bot.sm.open_position is not None, "expected an open position after bar 1"
        pos = bot.sm.open_position
        assert pos.direction.value == "long"
        assert pos.stop_loss < pos.entry_price  # broker-confirmed protective stop
        live_pos = broker.api.Positions.FindAll(broker.api.Label)[0]
        assert live_pos.StopLoss == pytest.approx(pos.stop_loss)

        # bar 2: price runs through the stop; the BROKER enforces it
        next_bar(cbot_main, broker, bot, ticks=[2995.0, 2989.0])
        assert bot.sm.open_position is None
        assert len(bot.sm.trades) == 1
        trade = bot.sm.trades[0]
        assert trade.exit_reason == ExitReason.STOP_LOSS
        assert trade.pnl < 0
        # risk actually taken <= configured risk (0.35% of ~10k = 35):
        assert abs(trade.pnl) < 40.0

        # audit trail read back from the real SQLite file:
        db = bot.audit.db_path
        import sqlite3

        conn = sqlite3.connect(db)
        n_signals = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        n_trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        pos_rows = conn.execute("SELECT is_open FROM positions").fetchall()
        event_types = {r[0] for r in conn.execute("SELECT event_type FROM events")}
        n_snaps = conn.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0]
        n_daily = conn.execute("SELECT COUNT(*) FROM daily_stats").fetchone()[0]
        conn.close()
        assert n_signals == 1
        assert n_trades == 1
        assert pos_rows == [(0,)]
        assert {"signal_generated", "order_filled", "position_closed"} <= event_types
        assert n_snaps >= 1
        assert n_daily >= 1

    def test_take_profit_exit_records_win(self, cbot_main, params, tmp_path):
        from conftest import next_bar

        broker, bot = self._env(cbot_main, params, tmp_path)
        bot.sm.strategy = OneShotStrategy(len(bot.sm.history) + 1, "long", 10.0, 20.0)
        next_bar(cbot_main, broker, bot, ticks=())
        entry = bot.sm.open_position.entry_price

        # rally through the take-profit (entry + 20)
        next_bar(cbot_main, broker, bot, ticks=[entry + 10.0, entry + 20.5])
        assert bot.sm.open_position is None
        assert bot.sm.trades[0].exit_reason == ExitReason.TAKE_PROFIT
        assert bot.sm.trades[0].pnl > 0
        assert bot.sm.consecutive_loss_tracker.consecutive_losses == 0  # win resets streak

    def test_daily_loss_limit_halts_new_entries(self, cbot_main, params, tmp_path):
        from conftest import next_bar

        # 2% limit on a 2,000 account = 40; one stop-out at 0.01 lots /
        # 10 points costs ~10 -> use a 0.5% daily limit so ONE loss halts
        broker, bot = self._env(
            cbot_main, params, tmp_path,
            balance=2_000.0,
            DailyLossLimitPercent=0.005,
            DefaultRiskPercent=0.005,
            PhTakeProfitDistance=0.0,  # no TP: position exits only via SL
        )
        next_bar(cbot_main, broker, bot, ticks=())
        assert bot.sm.open_position is not None
        entry = bot.sm.open_position.entry_price
        sl = bot.sm.open_position.stop_loss

        # one stop-out (~10 loss = 0.5% of 2,000 -> at/over the limit)
        next_bar(cbot_main, broker, bot, ticks=[sl - 0.5])
        assert len(bot.sm.trades) == 1

        # next bar: strategy fires again (interval=1) but the risk gate
        # must reject it — daily loss limit hit
        next_bar(cbot_main, broker, bot, ticks=[3000.0])
        assert bot.sm.open_position is None
        assert len(bot.sm.rejected_signals) >= 1
        assert all(r.reason.value == "daily_loss_limit_hit" for r in bot.sm.rejected_signals)

        import sqlite3

        conn = sqlite3.connect(bot.audit.db_path)
        rej = conn.execute("SELECT reason FROM rejected_signals").fetchall()
        daily = conn.execute(
            "SELECT daily_loss_limit_hit FROM daily_stats"
        ).fetchall()
        conn.close()
        assert ("daily_loss_limit_hit",) in rej
        assert any(row[0] == 1 for row in daily)

    def test_duplicate_guard_blocks_resubmitted_signal_after_failed_order(self, cbot_main, params, tmp_path):
        from conftest import next_bar

        broker, bot = self._env(cbot_main, params, tmp_path)

        class _RepeatedSignalStrategy(Strategy):
            name = "repeated_test_only"

            def __init__(self, from_index):
                self.from_index = from_index

            def generate_signal(self, market_state: MarketState):
                if len(market_state.history) < self.from_index:
                    return None
                from core_enums import Direction
                from core_models import Signal

                return Signal(
                    symbol=market_state.symbol,
                    direction=Direction.LONG,
                    entry_price=3000.0,
                    stop_loss=2990.0,
                    take_profit=3020.0,
                    strategy_name=self.name,
                )

        bot.sm.config.filters.duplicate_order_debounce_seconds = 1000
        bot.sm.duplicate_guard.debounce_seconds = 1000
        bot.sm.strategy = _RepeatedSignalStrategy(len(bot.sm.history) + 1)
        broker.reject_next_order = True  # broker rejects the first attempt

        # first candle: order rejected by broker -> ORDER_FAILED, no position
        next_bar(cbot_main, broker, bot, ticks=())
        assert bot.sm.open_position is None
        assert not [t for t in bot.sm.trades]

        # second candle: identical signal -> duplicate guard rejects it
        next_bar(cbot_main, broker, bot, ticks=[])
        assert bot.sm.open_position is None
        assert len(bot.sm.rejected_signals) == 1
        assert bot.sm.rejected_signals[0].reason.value == "duplicate_order"


# ---------------------------------------------------------------------
# kill switch
# ---------------------------------------------------------------------

class TestKillSwitch:
    def _env(self, cbot_main, params, tmp_path, **kw):
        return _lifecycle_env(cbot_main, params, tmp_path, **kw)

    def test_drawdown_breach_force_closes_open_position(self, cbot_main, params, tmp_path):
        from conftest import next_bar

        broker, bot = self._env(cbot_main, params, tmp_path)
        bot.sm.strategy = OneShotStrategy(len(bot.sm.history) + 1, "long", 10.0, 20.0)
        # prime the account 9.995% under its peak (constructed at 10,000);
        # the day start moves with it so the DAILY loss limit (2%) does
        # not trip before the max-drawdown kill switch (10%)
        bot.sm.account_tracker.balance = 9_000.5
        bot.sm.account_tracker.daily_start_equity = 9_000.5
        next_bar(cbot_main, broker, bot, ticks=())
        assert bot.sm.open_position is not None
        pos = bot.sm.open_position
        assert pos.stop_loss < 2990.0 + 1  # sanity: stop is ~10 below entry

        # a small adverse move breaches the 10% drawdown BEFORE the stop
        next_bar(cbot_main, broker, bot, ticks=[pos.entry_price - 0.5])
        assert bot.sm.state == SystemState.KILL_SWITCH_ACTIVE
        assert bot.sm.open_position is None
        assert bot.sm.kill_switch.armed is False
        assert len(bot.sm.trades) == 1
        assert bot.sm.trades[0].exit_reason == ExitReason.KILL_SWITCH

        # no new entries ever again
        for _ in range(3):
            next_bar(cbot_main, broker, bot, ticks=[3000.0])
        assert bot.sm.open_position is None
        assert len(bot.sm.trades) == 1


# ---------------------------------------------------------------------
# restart reconciliation (position adoption)
# ---------------------------------------------------------------------

class TestStartupAdoption:
    def test_adopts_open_position_at_startup(self, cbot_main, params, tmp_path):
        from conftest import make_broker, start_cbot

        p = dict(params)
        p.update({"Strategy": "placeholder_alternating_TEST_ONLY",
                  "PhInterval": 1000, "PhStopDistance": 10.0, "PhTakeProfitDistance": 20.0})
        broker = make_broker(tmp_path, params=p, balance=10_000.0)
        broker.open_position_for_adopt_test(direction=1, lots=0.02, stop_distance=10.0)

        bot = start_cbot(cbot_main, broker)
        assert not broker.api.stopped
        assert bot.sm.open_position is not None
        assert bot.sm.state == SystemState.MONITORING
        assert any("adopted existing position" in r[1] for r in
                   __import__("conftest").events_of_type(bot, "error"))

        import sqlite3

        conn = sqlite3.connect(bot.audit.db_path)
        rows = conn.execute("SELECT is_open FROM positions").fetchall()
        conn.close()
        assert rows == [(1,)]


# ---------------------------------------------------------------------
# trailing stop (live stop actually moves at the broker)
# ---------------------------------------------------------------------

class TestTrailingStop:
    def _env(self, cbot_main, params, tmp_path, **kw):
        return _lifecycle_env(cbot_main, params, tmp_path, **kw)

    def test_trail_moves_broker_stop_up_and_exits_on_trail(self, cbot_main, params, tmp_path):
        from conftest import next_bar

        broker, bot = self._env(
            cbot_main, params, tmp_path,
            TrailingStopEnabled=True,
            TrailingStopAtrMultiplier=3.0,
        )
        bot.sm.strategy = OneShotStrategy(len(bot.sm.history) + 1, "long", 10.0, None)
        next_bar(cbot_main, broker, bot, ticks=())
        assert bot.sm.open_position is not None
        assert bot.sm.open_position.take_profit is None
        initial_sl = bot.sm.open_position.stop_loss
        entry = bot.sm.open_position.entry_price

        # price rallies: each tick tightens the broker's stop upward
        next_bar(cbot_main, broker, bot, ticks=[entry + 5.0, entry + 8.0])
        assert bot.sm.open_position is not None
        trailed_sl = bot.sm.open_position.stop_loss
        assert trailed_sl > initial_sl + 2.0  # moved meaningfully in favor
        assert any("trailing stop moved" in r[1] for r in
                   __import__("conftest").events_of_type(bot, "sl_modified"))
        # the broker-side position carries the new stop
        live = broker.api.Positions.FindAll(broker.api.Label)[0]
        assert live.StopLoss == pytest.approx(trailed_sl, abs=0.01)

        # price falls back through the trailed stop -> broker closes it
        next_bar(cbot_main, broker, bot, ticks=[trailed_sl - 1.0])
        assert bot.sm.open_position is None
        assert len(bot.sm.trades) == 1
        assert bot.sm.trades[0].exit_reason == ExitReason.STOP_LOSS
        assert bot.sm.trades[0].pnl > 0  # locked in profit above entry


# ---------------------------------------------------------------------
# history preload
# ---------------------------------------------------------------------

class TestHistoryPreload:
    def test_no_trading_until_history_loaded(self, cbot_main, params, tmp_path):
        from conftest import make_broker, start_cbot, next_bar

        p = dict(params)
        p.update({"Strategy": "placeholder_alternating_TEST_ONLY",
                  "PhInterval": 1, "PhStopDistance": 10.0, "PhTakeProfitDistance": 20.0,
                  "HistoryPreloadCandles": 50})
        broker = make_broker(tmp_path, params=p, balance=10_000.0, n_closed=10)
        bot = start_cbot(cbot_main, broker)

        assert broker.bars.load_requested is True
        assert bot._history_ready is False

        # bars keep arriving — but nothing trades before history is ready
        next_bar(cbot_main, broker, bot, ticks=[])
        assert broker.orders_placed == 0
        assert bot._history_ready is False

        # the platform delivers the requested history
        broker.bars.fire_history_loaded(broker.bars.Count)
        bot.on_tick()
        assert bot._history_ready is True
        assert len(bot.sm.history) >= 10

        # now the strategy can fire
        next_bar(cbot_main, broker, bot, ticks=())
        assert broker.orders_placed == 1


# ---------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------

class TestShutdown:
    def test_on_stop_logs_and_closes_audit(self, cbot_main, params, tmp_path):
        from conftest import make_broker, start_cbot

        p = dict(params)
        p.update({"Strategy": "placeholder_alternating_TEST_ONLY", "PhInterval": 1000})
        broker = make_broker(tmp_path, params=p, balance=10_000.0)
        bot = start_cbot(cbot_main, broker)
        bot.on_stop()
        import sqlite3

        conn = sqlite3.connect(bot.audit.db_path)
        rows = conn.execute(
            "SELECT message FROM events WHERE event_type = 'state_transition'"
        ).fetchall()
        conn.close()
        assert any("stopped by user" in r[0] for r in rows)
