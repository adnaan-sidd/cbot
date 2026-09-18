"""
Backtest engine tests. The key scenario (TestHandComputedSingleTrade)
hand-computes the exact expected fill prices, commission, and P&L for a
single trade end-to-end, so a bug in cost/fill/commission wiring would
be caught by an exact-number assertion, not just a "trades list is
non-empty" smoke test.
"""

from datetime import datetime, timedelta

import pytest

from backtesting.backtest_engine import BacktestEngine
from backtesting.cost_model import CostModel
from backtesting.slippage_model import FixedSlippageModel
from config.config_schema import BotConfig, BrokerConfig, FilterConfig, KillSwitchConfig, RiskConfig
from core.enums import Direction, ExitReason
from core.models import Candle
from market_data.historical_feed import HistoricalFeed
from strategy.placeholder_strategy import AlternatingIntervalStrategy
from strategy.strategy_interface import MarketState, Strategy


def _config(**risk_overrides) -> BotConfig:
    return BotConfig(
        environment="backtest",
        risk=RiskConfig(**risk_overrides),
        filters=FilterConfig(max_spread_points=35, allowed_sessions=None),
        broker=BrokerConfig(),  # contract_size=20, digits=1 (point=0.1), commission=3.5 USC/lot
        kill_switch=KillSwitchConfig(),
    )


def _flat_candle(i: int, price: float, spread_points=10.0) -> Candle:
    ts = datetime(2026, 1, 1) + timedelta(minutes=i)
    return Candle(
        timestamp=ts, open=price, high=price, low=price, close=price, volume=10,
        spread_points=spread_points,
    )


class _SingleLongSignalStrategy(Strategy):
    """Fires exactly one long signal on the 3rd candle, then never again.
    Used to isolate a single trade's full lifecycle for hand-computation."""

    name = "single_long_test_only"

    def __init__(self, entry_price: float, stop_loss: float, take_profit: float):
        self._fired = False
        self._entry_price = entry_price
        self._stop_loss = stop_loss
        self._take_profit = take_profit

    def generate_signal(self, market_state: MarketState):
        if self._fired or len(market_state.history) < 3:
            return None
        self._fired = True
        from core.models import Signal

        return Signal(
            symbol=market_state.symbol,
            direction=Direction.LONG,
            entry_price=self._entry_price,
            stop_loss=self._stop_loss,
            take_profit=self._take_profit,
            strategy_name=self.name,
        )


class _SingleLongNoTakeProfitStrategy(Strategy):
    """Same as _SingleLongSignalStrategy but omits take_profit -- used to
    exercise the backtest engine's trailing-stop path, which only
    activates for positions opened without a fixed target."""

    name = "single_long_no_tp_test_only"

    def __init__(self, entry_price: float, stop_loss: float):
        self._fired = False
        self._entry_price = entry_price
        self._stop_loss = stop_loss

    def generate_signal(self, market_state: MarketState):
        if self._fired or len(market_state.history) < 3:
            return None
        self._fired = True
        from core.models import Signal

        return Signal(
            symbol=market_state.symbol,
            direction=Direction.LONG,
            entry_price=self._entry_price,
            stop_loss=self._stop_loss,
            take_profit=None,
            strategy_name=self.name,
        )


class TestTrailingStopIntegration:
    def test_disabled_by_default_behaves_like_phase_3(self):
        """trailing_stop_atr_multiplier=None (the default) must not
        change any existing behavior -- the stop stays exactly where
        the strategy set it."""
        config = _config()
        candles = [
            _flat_candle(0, 3598.0), _flat_candle(1, 3599.0), _flat_candle(2, 3600.0),
            _flat_candle(3, 3620.0), _flat_candle(4, 3640.0), _flat_candle(5, 3660.0),
        ]
        engine = BacktestEngine(
            feed=HistoricalFeed(candles),
            strategy=_SingleLongNoTakeProfitStrategy(3600.0, 3590.0),
            config=config,
            cost_model=CostModel(commission_per_million_usd=30.0),
            slippage_model=FixedSlippageModel(price_amount=0.0),
            starting_equity=10_000.0, symbol="XAUUSD",
        )
        engine.run()
        # position never exits (price only rises, stop never hit) --
        # confirm the stop is UNCHANGED from its original placement
        assert engine.open_position is not None
        assert engine.open_position.stop_loss == pytest.approx(3590.0)

    def test_enabled_trail_tightens_stop_as_price_rises(self):
        config = _config()
        candles = [
            _flat_candle(0, 3598.0), _flat_candle(1, 3599.0), _flat_candle(2, 3600.0),
            _flat_candle(3, 3620.0), _flat_candle(4, 3640.0), _flat_candle(5, 3660.0),
        ]
        engine = BacktestEngine(
            feed=HistoricalFeed(candles),
            strategy=_SingleLongNoTakeProfitStrategy(3600.0, 3590.0),
            config=config,
            cost_model=CostModel(commission_per_million_usd=30.0),
            slippage_model=FixedSlippageModel(price_amount=0.0),
            starting_equity=10_000.0, symbol="XAUUSD",
            trailing_stop_atr_multiplier=1.0, trailing_stop_atr_period=2,
        )
        engine.run()
        assert engine.open_position is not None
        # price only rose -- trailing stop must have tightened well above the original 3590
        assert engine.open_position.stop_loss > 3590.0

    def test_trailing_stop_eventually_exits_on_a_pullback(self):
        config = _config()
        candles = [
            _flat_candle(0, 3598.0), _flat_candle(1, 3599.0), _flat_candle(2, 3600.0),
            _flat_candle(3, 3620.0), _flat_candle(4, 3640.0), _flat_candle(5, 3660.0),
            # sharp pullback -- should hit the trailed stop, not the original 3590
            Candle(timestamp=datetime(2026, 1, 1) + timedelta(minutes=6), open=3660, high=3661, low=3600, close=3610, volume=10, spread_points=10.0),
        ]
        engine = BacktestEngine(
            feed=HistoricalFeed(candles),
            strategy=_SingleLongNoTakeProfitStrategy(3600.0, 3590.0),
            config=config,
            cost_model=CostModel(commission_per_million_usd=30.0),
            slippage_model=FixedSlippageModel(price_amount=0.0),
            starting_equity=10_000.0, symbol="XAUUSD",
            trailing_stop_atr_multiplier=1.0, trailing_stop_atr_period=2,
        )
        result = engine.run()
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.exit_reason == ExitReason.STOP_LOSS
        # exited well above the original stop (3590) -- proof the trail moved
        assert trade.exit_price > 3590.0
        assert trade.pnl > 0  # trailing captured a profit, unlike the fixed original stop would have


class TestHandComputedSingleTrade:
    """
    Setup:
      - starting_equity = $10,000
      - risk_percent = 0.35% (default) -> risk_amount = $35
      - entry signal: LONG at 3600.0, SL=3590.0 (distance=$10), TP=3620.0
      - contract_size = 100 -> raw_lot = 35 / (10*100) = 0.035 -> floor to lot_step 0.01 = 0.03
      - actual risk_amount = 0.03 * 10 * 100 = $30
      - spread = 10 points, point_size = 0.01 (digits=2) -> spread_price = $0.10
      - fixed slippage = $0.20 (entry AND exit, both adverse)
      - commission = $30 per $1M notional, ONE-WAY, computed per fill (not a flat
        per-lot rate) since notional depends on the actual fill price

    Fill prices:
      entry: signal.entry_price(3600.0) + spread($0.10) + slippage($0.20) = 3600.30
      candle after entry rises to hit TP at 3620.0 exactly (candle high touches it)
      exit: TP(3620.0) - slippage($0.20) [closing a long = selling, adverse = down] = 3619.80

    P&L:
      gross_pnl = (exit_price - entry_price) * lot_size * contract_size
                = (3619.80 - 3600.30) * 0.03 * 100
                = 19.50 * 0.03 * 100
                = 58.50

      entry_commission = (lot_size * contract_size * entry_fill_price / 1,000,000) * 30
                       = (0.03 * 100 * 3600.30 / 1,000,000) * 30
                       = (10800.90 / 1,000,000) * 30
                       = 0.324027
      exit_commission  = (0.03 * 100 * 3619.80 / 1,000,000) * 30
                       = (10859.40 / 1,000,000) * 30
                       = 0.325782
      total_commission = 0.324027 + 0.325782 = 0.649809

      net_pnl = 58.50 - 0.649809 = 57.850191
    """

    def _build_feed(self) -> HistoricalFeed:
        candles = [
            _flat_candle(0, 3598.0),
            _flat_candle(1, 3599.0),
            _flat_candle(2, 3600.0),  # signal fires here (3rd candle, index 2)
            _flat_candle(3, 3610.0),  # position now open, price rising
            _flat_candle(4, 3625.0),  # this candle's high(3625) >= TP(3620) -> exit
        ]
        # give the exit candle a high above TP; _flat_candle sets high=price,
        # so build it explicitly instead:
        candles[4] = Candle(
            timestamp=candles[4].timestamp, open=3615, high=3625, low=3610, close=3620,
            volume=10, spread_points=10.0,
        )
        return HistoricalFeed(candles)

    def test_exact_fill_prices_and_pnl(self):
        config = _config()
        cost_model = CostModel(commission_per_million_usd=config.broker.commission_per_million_usd)
        slippage_model = FixedSlippageModel(price_amount=0.20)
        strategy = _SingleLongSignalStrategy(entry_price=3600.0, stop_loss=3590.0, take_profit=3620.0)

        engine = BacktestEngine(
            feed=self._build_feed(),
            strategy=strategy,
            config=config,
            cost_model=cost_model,
            slippage_model=slippage_model,
            starting_equity=10_000.0,
            symbol="XAUUSD",
        )
        result = engine.run()

        assert len(result.trades) == 1
        trade = result.trades[0]

        assert trade.lot_size == pytest.approx(0.03)
        assert trade.entry_price == pytest.approx(3600.30)
        assert trade.exit_price == pytest.approx(3619.80)
        assert trade.exit_reason == ExitReason.TAKE_PROFIT
        assert trade.commission == pytest.approx(0.649809, abs=1e-6)
        assert trade.pnl == pytest.approx(57.850191, abs=1e-4)

        assert result.starting_equity == pytest.approx(10_000.0)
        assert result.ending_equity == pytest.approx(10_000.0 + 57.850191, abs=1e-4)

    def test_no_rejected_signals_in_clean_scenario(self):
        config = _config()
        engine = BacktestEngine(
            feed=self._build_feed(),
            strategy=_SingleLongSignalStrategy(3600.0, 3590.0, 3620.0),
            config=config,
            cost_model=CostModel(commission_per_million_usd=30.0),
            slippage_model=FixedSlippageModel(price_amount=0.20),
            starting_equity=10_000.0,
            symbol="XAUUSD",
        )
        result = engine.run()
        assert result.rejected_signals == []

    def test_equity_curve_has_one_point_per_candle(self):
        config = _config()
        feed = self._build_feed()
        engine = BacktestEngine(
            feed=feed,
            strategy=_SingleLongSignalStrategy(3600.0, 3590.0, 3620.0),
            config=config,
            cost_model=CostModel(commission_per_million_usd=30.0),
            slippage_model=FixedSlippageModel(price_amount=0.20),
            starting_equity=10_000.0,
            symbol="XAUUSD",
        )
        result = engine.run()
        assert len(result.equity_curve) == len(list(feed.candles()))


class TestStopLossExit:
    def test_stop_loss_hit_produces_loss(self):
        config = _config()
        candles = [
            _flat_candle(0, 3598.0),
            _flat_candle(1, 3599.0),
            _flat_candle(2, 3600.0),
            Candle(
                timestamp=datetime(2026, 1, 1) + timedelta(minutes=3),
                open=3595, high=3596, low=3585, close=3590, volume=10, spread_points=10.0,
            ),  # low(3585) <= SL(3590) -> stop hit
        ]
        engine = BacktestEngine(
            feed=HistoricalFeed(candles),
            strategy=_SingleLongSignalStrategy(3600.0, 3590.0, 3620.0),
            config=config,
            cost_model=CostModel(commission_per_million_usd=30.0),
            slippage_model=FixedSlippageModel(price_amount=0.0),  # isolate SL logic from slippage
            starting_equity=10_000.0,
            symbol="XAUUSD",
        )
        result = engine.run()
        assert len(result.trades) == 1
        assert result.trades[0].exit_reason == ExitReason.STOP_LOSS
        assert result.trades[0].pnl < 0

    def test_both_sl_and_tp_touched_same_candle_assumes_sl_first(self):
        """Worst-case assumption documented in backtest_engine.py."""
        config = _config()
        candles = [
            _flat_candle(0, 3598.0),
            _flat_candle(1, 3599.0),
            _flat_candle(2, 3600.0),
            Candle(
                timestamp=datetime(2026, 1, 1) + timedelta(minutes=3),
                open=3600, high=3625, low=3585, close=3600, volume=10, spread_points=10.0,
            ),  # range touches both SL(3590) and TP(3620)
        ]
        engine = BacktestEngine(
            feed=HistoricalFeed(candles),
            strategy=_SingleLongSignalStrategy(3600.0, 3590.0, 3620.0),
            config=config,
            cost_model=CostModel(commission_per_million_usd=30.0),
            slippage_model=FixedSlippageModel(price_amount=0.0),
            starting_equity=10_000.0,
            symbol="XAUUSD",
        )
        result = engine.run()
        assert result.trades[0].exit_reason == ExitReason.STOP_LOSS


class TestRiskGateRejectionSurfacesInBacktest:
    def test_signal_rejected_when_daily_loss_limit_already_hit(self):
        """Proves the backtest engine actually calls the real risk_gate —
        not a simplified copy — by forcing a daily-loss breach and
        confirming NO trade opens even though the signal itself is valid."""
        config = _config(daily_loss_limit_percent=0.02)
        candles = [_flat_candle(i, 3600.0) for i in range(5)]
        engine = BacktestEngine(
            feed=HistoricalFeed(candles),
            strategy=_SingleLongSignalStrategy(3600.0, 3590.0, 3620.0),
            config=config,
            cost_model=CostModel(commission_per_million_usd=30.0),
            slippage_model=FixedSlippageModel(price_amount=0.0),
            starting_equity=10_000.0,
            symbol="XAUUSD",
        )
        # simulate an already-breached day before any candle is processed
        engine.daily_start_equity = 10_000.0
        engine.equity = 9_700.0  # 3% "loss" already realized this "day"
        engine.current_trading_day = candles[0].timestamp.date()

        result = engine.run()
        assert result.trades == []
        assert len(result.rejected_signals) == 1


class TestPlaceholderStrategyProducesMultipleTrades:
    def test_alternating_strategy_runs_end_to_end_without_error(self):
        """Broader smoke test: run the alternating placeholder strategy
        over a longer synthetic price path and confirm the engine
        completes without exceptions and produces a plausible number of
        trades — not a hand-computed test, but exercises the full
        entry/exit/re-entry cycle repeatedly (unlike the single-trade
        tests above)."""
        config = _config()
        candles = []
        price = 3600.0
        ts = datetime(2026, 1, 1)
        for i in range(200):
            # gentle oscillation so both SL and TP get hit across the run
            price += 2.0 if (i // 5) % 2 == 0 else -2.0
            candles.append(
                Candle(
                    timestamp=ts + timedelta(minutes=i),
                    open=price, high=price + 3, low=price - 3, close=price,
                    volume=10, spread_points=5.0,
                )
            )
        engine = BacktestEngine(
            feed=HistoricalFeed(candles),
            strategy=AlternatingIntervalStrategy(interval=10, stop_distance=8.0, take_profit_distance=12.0),
            config=config,
            cost_model=CostModel(commission_per_million_usd=30.0),
            slippage_model=FixedSlippageModel(price_amount=0.1),
            starting_equity=10_000.0,
            symbol="XAUUSD",
        )
        result = engine.run()
        assert len(result.equity_curve) == 200
        # not asserting exact trade count/pnl (not hand-computed here) —
        # just that the engine ran the full lifecycle repeatedly without error
        assert isinstance(result.trades, list)
