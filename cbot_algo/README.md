# XAUUSD cBot — cTrader Algo (native Python) port of `xauusd_bot`

This folder contains a **complete, logic-for-logic port** of the standalone
`xauusd_bot` engine (Python process + cTrader Open API client) to a
**cTrader Algo cBot written in Python** — i.e. the "Algo → Python" option in
cTrader, with the external process removed entirely. The strategy, risk,
filter, monitoring, state-machine and audit code runs *inside* cTrader; the
platform itself is the data feed and the order gateway.

```
cbot_algo/
├── XAUUSD cBot/                    # Visual Studio solution (net6.0)
│   ├── XAUUSD cBot.sln
│   └── XAUUSD cBot/
│       ├── XAUUSD cBot.csproj      # AlgoLanguage=Python, embeds every .py
│       ├── EmbeddedResources.manifest.json
│       ├── XAUUSDcBot.cs           # [Robot] class + all [Parameter] declarations
│       ├── Engine.cs / EngineHelper.cs / PythonHooks.cs /
│       │   PythonManifest.cs / RobotBridge.cs / SafeExecuteMethodProxy.cs
│       │   EmbeddedResourceProvider.cs      # cTrader Python-algo engine (from
│       │                                    # Spotware's official sample)
│       ├── XAUUSD cBot_main.py     # ENTRY POINT — XAUUSDcBot class, CTraderExecution
│       └── audit.py  bot_state_machine.py  config_schema.py  core_enums.py
│           core_events.py  core_models.py  execution_interface.py
│           indicators.py  position_monitor.py  resample.py  risk_engine.py
│           robot_wrapper.py  strategies.py  strategy_interface.py
│           trade_filters.py  requirements.txt
└── tests/                          # pytest suite — 291 tests
    ├── ctrader_api_mock.py         # full in-memory cTrader API mock
    ├── conftest.py                 # cAlgo stubs + cBot boot fixture
    └── test_*.py                   # 24 ported unit suites + 13 e2e glue tests
```

## What is identical to the repo

| Repo (`xauusd_bot/`) | Here | Notes |
|---|---|---|
| `config/config.yaml` + `config_schema.py` | `XAUUSDcBot.cs` `[Parameter]`s + `config_schema.py` (ported) | Every default and bound mirrored; Python re-validates at boot and `Stop()`s on any violation |
| `engine/` state machine | `bot_state_machine.py` (ported) | Same states, same entry pipeline (strategy → filters → risk gate → sizing → entry), same kill-switch / exit logic |
| `risk/` | `risk_engine.py` (ported) | Position sizer, risk gates, loss limits, peak/trough tracking |
| `filters/` | `trade_filters.py` (ported) | Spread, session, news, duplicate-order debounce — same order, same semantics |
| `strategies/` | `strategies.py` (ported) | Placeholder, trend pullback, trend pullback-breakout, mean reversion |
| `monitor/` trailing stop | `position_monitor.py` (ported) | ATR/percent trailing, cooldown, step rules |
| `audit/` | `audit.py` (ported) | SQLite `signals / rejected_signals / trades / positions / events`, strict-persistence mode |
| `market_data/ctrader_feed.py` + `execution/ctrader_executor.py` | `XAUUSD cBot_main.py` (`XAUUSDcBot` + `CTraderExecution`) | The platform replaces both: closed bars from `api.Bars`, orders via `api.ExecuteMarketOrder` / `api.ClosePosition` |

### The one deliberate behavioural difference

In the external process the bot computed SL/TP fills itself from candle
ranges. **In cTrader the broker enforces StopLoss/TakeProfit server-side**,
and the state machine is built with `broker_enforced_exits=True`: it no
longer self-closes on candle range touching SL/TP (which would be wrong with
mid-bar trailing, since a trailed stop can sit below the bar's low), and the
trade is recorded from the broker's `Positions.Closed` event. Kill-switch
closes (immediate market order), duplicate/rejection handling, filters,
adoption of a pre-existing open position, history preload and audit are all
unchanged. The repo's unit tests run the state machine with the old
`DeterministicExecutor` path (default), so both behaviours are covered.

## Running the test suite (any OS)

The 291 tests run pure Python against a mock of the cTrader API — no cTrader
installation needed:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r "XAUUSD cBot/requirements.txt" pytest
pytest tests/ -q
```

Layout:

* **241 ported unit tests** — indicators, resampling, risk, filters,
  strategies, position monitor, audit, config schema, state machine
  (deterministic executor path).
* **13 end-to-end glue tests** (`tests/test_cbot_glue.py`) — boot a real
  `XAUUSDcBot` against the mock API and assert the full lifecycle:
  config validation halts, entry → SL/TP close → audit rows, win/loss
  streaks, daily-loss halt, duplicate-order guard, broker-rejection
  resubmission, max-drawdown kill switch, position adoption, trailing-stop
  updates, history preload gating, clean shutdown.

## Building the cBot (Windows + cTrader)

cTrader builds/runs on Windows (and in the cTrader Algo editor), so this step
happens on your machine — it cannot be executed in this Linux sandbox.

1. **cTrader ≥ 4.4** (Algo support) installed, **Algo mode enabled**
   (Settings → Automation).
2. Install **Visual Studio 2022** (or just the .NET 6 SDK) — any workload
   that can build a `net6.0` class library works.
3. Open `XAUUSD cBot/XAUUSD cBot.sln` in Visual Studio.
   NuGet restore pulls `cTrader.Automate` (API types) and `pythonnet 3.0.5`
   (the Python runtime the engine drives).
4. **Build** (Release). If any `.py` file is added/renamed, add it to
   `EmbeddedResources.manifest.json` (`PythonFiles`) — the engine loads
   modules from the manifest at runtime and the build embeds every `.py`.
5. Output: `bin/Release/net6.0/XAUUSD cBot.dll`.
6. In cTrader: **Algo → Manage → Import**, pick the `.dll` (or open the
   `.sln` project directly in the cTrader Algo editor for source-level
   development — the editor rebuilds it the same way).
7. Attach the cBot to an **XAUUSD** chart whose timeframe equals the
   *Timeframe (minutes)* parameter (default 15) — boot fails loudly if they
   disagree. Audit files land in
   `Documents/XAUUSDcBot-audit/<account-number>/<label>/` (override with the
   *Audit dir* parameter), same schema as the repo's SQLite audit.
8. Test in the **Strategy Tester** first, then live. Note: the tester
   replays bars; the `Positions.Closed`-driven SL/TP recording follows the
   broker's simulated fills, so tester results reflect the same exit path as
   live.

## Honest limitations

* The **C# engine files are from Spotware's official Python-algo sample**
  (see `spotware/ctrader-python-algo-samples`) — unmodified except the
  `[Robot]` class/parameter block. The C# side has **not been compiled here**
  (no Windows/cTrader in this sandbox); it is template-verified, not
  build-verified.
* Python behaviour **is** verified: 291/291 tests, including 13 e2e tests
  driving the real `XAUUSDcBot` class through the full lifecycle against a
  mock that mirrors the documented cTrader API surface (verified against
  Spotware's samples: `api.Bars` oldest-first indexing, `api.ExecuteMarketOrder`,
  `Positions.Opened/Closed/Modified` events, `ModifyTrailingStop`, `NetProfit`,
  `LoadMoreHistoryAsync`/`HistoryLoaded`, …).
* `api.Symbol.QuantityToVolumeInUnits`-style conversions and margin checks
  use the mock's simplifications (pip-based, $/1M commission) — fine for
  logic tests, not a live-margin model.
