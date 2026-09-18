"""Test harness for the XAUUSD cBot.

Two layers:
  1. Pure-logic tests — the ported risk engine / filters / monitors /
     strategies run as plain Python modules (same as in xauusd_bot).
  2. cBot glue tests — the main module (XAUUSD cBot_main.py) runs against
     the mock cTrader platform (ctrader_api_mock.py), exercising the
     full loop: closed-candle entries, tick monitoring, broker-enforced
     SL/TP, kill switch, audit trail.

The main module does `import clr` / `from cAlgo.API import *` at the
top, which only exist inside cTrader — so we stub those modules here
BEFORE loading it, exactly mirroring the names the mock platform
provides.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PROJECT_DIR = HERE.parent / "XAUUSD cBot" / "XAUUSD cBot"
MAIN_PY = PROJECT_DIR / "XAUUSD cBot_main.py"

# --- make the flat cBot modules importable -------------------------------
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

# --- stub the cTrader-only modules ----------------------------------------
if "clr" not in sys.modules:
    _clr = types.ModuleType("clr")
    _clr.AddReference = lambda *a, **k: None
    sys.modules["clr"] = _clr

from ctrader_api_mock import TimeFrame, TradeType, PositionCloseReason, ErrorCode  # noqa: E402

_calgo = types.ModuleType("cAlgo")
_calgo_api = types.ModuleType("cAlgo.API")
_calgo_api.TradeType = TradeType
_calgo_api.TimeFrame = TimeFrame
_calgo_api.PositionCloseReason = PositionCloseReason
_calgo_api.ErrorCode = ErrorCode
_calgo_api.__all__ = ["TradeType", "TimeFrame", "PositionCloseReason", "ErrorCode"]
sys.modules.setdefault("cAlgo", _calgo)
sys.modules.setdefault("cAlgo.API", _calgo_api)

_robot_wrapper = types.ModuleType("robot_wrapper")
sys.modules.setdefault("robot_wrapper", _robot_wrapper)

# --- default cBot parameters (mirror XAUUSDcBot.cs defaults) ---------------
DEFAULT_PARAMS = {
    # Runtime
    "TimeframeMinutes": 15,
    "Label": "XAUUSDcBot",
    "AuditDir": "",
    "StrictPersistence": True,
    "HistoryPreloadCandles": 0,
    # Risk
    "DefaultRiskPercent": 0.0035,
    "MaxRiskPercent": 0.01,
    "DailyLossLimitPercent": 0.02,
    "MaxDrawdownPercent": 0.10,
    "MaxConsecutiveLosses": 3,
    "ConsecutiveLossCooldownHours": 24,
    "MarginUtilizationCap": 0.5,
    "MinLotRiskTolerance": 0.0,
    # Filters
    "MaxSpreadPoints": 35,
    "DuplicateOrderDebounceSeconds": 5,
    "AllowedSessions": "",
    # Broker
    "ExpectedSymbol": "XAUUSD",
    "Leverage": 100,
    "ContractSize": 100.0,
    "Digits": 2,
    "LotStep": 0.01,
    "MinLot": 0.01,
    "MaxLot": 100.0,
    "MarginRate": 1.0,
    "CommissionPerMillionUsd": 30.0,
    "AccountCurrencyUnit": "usd",
    "UnitScaleFactor": 1.0,
    "SwapEnabled": False,
    "SwapLongPoints": -58.6,
    "SwapShortPoints": 40.9,
    # Kill switch
    "ManualTriggerEnabled": False,
    "ManualRearmRequired": True,
    "TriggerOnBrokerDisconnect": True,
    "TriggerOnMaxDrawdown": True,
    # Strategy
    "Strategy": "trend_pullback_breakout_v2",
    "TrailingStopEnabled": False,
    "TrailingStopAtrMultiplier": 3.0,
    "TrailingStopAtrPeriod": 14,
    "PhInterval": 10,
    "PhStopDistance": 10.0,
    "PhTakeProfitDistance": 20.0,
    "TpHtfMinutes": 60, "TpHtfFastEma": 50, "TpHtfSlowEma": 200, "TpLtfFastEma": 20,
    "TpAtrPeriod": 14, "TpAtrStopMult": 1.5, "TpRewardRisk": 2.0, "TpRsiPeriod": 14,
    "TpRsiLongMin": 40.0, "TpRsiLongMax": 75.0, "TpRsiShortMin": 25.0, "TpRsiShortMax": 60.0,
    "TpMinAtrPrice": 0.0,
    "T2HtfMinutes": 60, "T2HtfFastEma": 50, "T2HtfSlowEma": 200, "T2HtfAtrPeriod": 14,
    "T2MinTrendStrengthAtr": 1.0, "T2LtfFastEma": 20, "T2MinPullbackBars": 2,
    "T2MaxPullbackBars": 8, "T2AtrPeriod": 14, "T2StopBufferAtrMult": 0.3,
    "T2RewardRisk": 2.0, "T2RsiPeriod": 14, "T2RsiLongMin": 30.0, "T2RsiLongMax": 75.0,
    "T2RsiShortMin": 25.0, "T2RsiShortMax": 70.0, "T2UseTrailingStop": False,
    "MrEmaPeriod": 20, "MrAtrPeriod": 14, "MrEntryThresholdAtr": 2.0, "MrStopAtrMult": 1.5,
    "MrHtfMinutes": 0, "MrHtfFastEma": 50, "MrHtfSlowEma": 200, "MrHtfAtrPeriod": 14,
    "MrMaxHtfTrendStrength": 0.0,
}


@pytest.fixture
def cbot_main():
    """Freshly loads the cBot main module (as cTrader would exec it)."""
    spec = importlib.util.spec_from_file_location("cbot_main_under_test", MAIN_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def params():
    return dict(DEFAULT_PARAMS)


def make_broker(tmp_path, params=None, *, balance=10_000.0, price=3000.0,
                spread_points=20.0, n_closed=60, **broker_overrides):
    from ctrader_api_mock import MockBroker

    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)
    p["AuditDir"] = str(tmp_path / "audit")
    broker = MockBroker(starting_balance=balance, price=price,
                        spread_points=spread_points, params=p, **broker_overrides)
    broker.preload_history(n_closed, start_price=price)
    return broker


def start_cbot(cbot_main, broker, tmp_path=None):
    """Runs the cBot's on_start against the mock platform. The cTrader
    engine injects the robot into `builtins.api` before exec'ing the
    main module; the harness mirrors that here."""
    import builtins

    builtins.api = broker.api
    bot = cbot_main.XAUUSDcBot()
    bot.on_start()
    return bot


def next_bar(cbot_main, broker, bot, *, open_price=None, ticks=()):
    """Simulates one full 15-minute bar: ticks during the bar (driving
    on_tick), then bar close (driving on_bar_closed)."""
    from ctrader_api_mock import TimeFrame  # noqa: F401

    t = broker.server.Time
    for price in ticks:
        broker.tick(price)
        bot.on_tick()
    broker.close_bar(open_price)
    bot.on_bar_closed()


def events_of_type(bot, event_type_value):
    """Read the audit DB back (the real source of truth)."""
    import sqlite3

    db = Path(bot.audit.db_path)
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT event_type, message, payload_json FROM events WHERE event_type = ?",
        (event_type_value,),
    ).fetchall()
    conn.close()
    return rows
