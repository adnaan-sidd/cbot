# XAUUSD Trading Bot — Phase 0

**This is a foundation scaffold only. There is no trading logic here.**
No market data connection, no strategy, no order placement. That is by
design — see the architecture document's phased development plan (§11):
the risk engine, filters, and backtester are built and tested *before*
any strategy or execution code exists, so safety logic never ends up
implicitly coupled to a particular strategy.

## What Phase 0 actually contains

- `config/` — pydantic-validated config schema (`config_schema.py`) plus
  YAML files per environment (`backtest`, `paper`, `live`). Every risk
  boundary from the brief (0.25–0.5% default risk, 1% absolute max, 2%
  daily loss limit, 10% max drawdown, single open position, etc.) is
  enforced as a `pydantic.Field` constraint — an out-of-bounds value
  fails to load, it does not get silently clamped.
- `core/` — shared enums, data models (`Signal`, `Position`, `Trade`,
  `AccountState`, etc.), and the event envelope used for logging. Key
  invariant: a `Signal` or `Position` **cannot be constructed without a
  valid stop-loss** — this is enforced in `__post_init__`, not by
  convention.
- `persistence/` — SQLite schema (append-only audit trail: `signals`,
  `rejected_signals`, `trades`, `positions`, `events`, `daily_stats`,
  `account_snapshots`) and a minimal `DBManager` + `EventLogger`.
- `main.py` — boots the bot: loads config, validates it, initializes the
  database, logs a `BOOT` event, and exits. That's the entire Phase 0
  runtime behavior.
- `tests/unit/` — validates config boundary enforcement and model
  invariants (mandatory stop-loss, direction-consistent SL/TP, drawdown
  math, etc.).

## Account/broker context baked into config

**Broker/platform note:** this project originally targeted PU Prime
(MT5, Islamic cents account). It was migrated to **XM, cTrader, Raw
Spread standard account** — a genuine change to the cost model and
currency handling, not just a renamed config value. Both brokers'
numbers are visible in git history / prior versions of this file if
ever needed for comparison, but every module now reflects XM/cTrader.

Current scope: **XM, cTrader, Raw Spread standard (non-cents) account**:
- Contract size 100 oz/lot, digits=2 (point=0.01), min/max/step lot
  0.01 / 100 / 0.01.
- **Commission is priced per $1,000,000 of notional USD volume ($30/1M),
  not as a flat per-lot fee** — a structurally different model from the
  previous broker, requiring its own per-fill calculation
  (`backtesting/cost_model.py:CostModel.commission_for_notional()`)
  rather than a flat per-lot rate. This is charged per side (entry AND
  exit) as an ASSUMPTION pending verification against a real filled
  order.
- `account_currency_unit: usd`, `unit_scale_factor: 1.0` — a standard
  account, not cents. The normalization layer (`risk_engine/normalization.py`)
  is a no-op for this broker but is deliberately kept fully general, since
  a broker switch is exactly the kind of change that has already
  happened once in this project.
- **Swap is NOT assumed free.** This account's Islamic/swap-free status
  was unconfirmed at setup time (unlike the previous, confirmed-Islamic
  broker), so `swap_enabled` defaults to `false` — no swap cost is
  charged in backtests by default. The real swap values from cTrader's
  Symbol Info (`swap_long_points: -58.6`, `swap_short_points: 40.9`,
  tripled on Wednesday, weekend swaps disabled) are stored in
  `BrokerConfig` and modeled in `backtesting/swap_model.py`, ready to
  switch on with one config change once confirmed.

**Still placeholders, pending verification:**
- `filters.max_spread_points` (35 points, a starting guess) — replace
  with a value derived from observed live XM XAUUSD (cTrader) spreads
  before Phase 5 (paper/live).
- `broker.commission_per_million_usd`'s per-side (vs. round-trip)
  charging assumption, and `broker.lot_step` (0.01, not explicitly shown
  in cTrader's Symbol Info panel) — both should be confirmed against a
  real filled order before paper/live use.
- `broker.swap_enabled` — confirm the account's actual swap-free status
  on XM and flip this on (or leave off) accordingly.

## Running Phase 0

```bash
pip install -r requirements.txt

# Run the boot sequence (loads config, validates, initializes DB, logs boot event)
python main.py --config config/environments/backtest.yaml

# Run the test suite
pytest -v
```

## What's next (Phase 1)

Position sizing (`risk_engine/position_sizer.py`), margin calculation,
and the daily-loss/drawdown/consecutive-loss limit tracking — built and
unit-tested in isolation, with no strategy or market data feed yet. See
the architecture document, §11, for the full phase plan.

---

# Phase 1 — Risk Engine

**Status: complete.** Built and tested in total isolation — no market
data feed, no strategy, no execution/order-placement code exists yet.
Every function here takes plain values or `AccountState`/`Signal`
objects as input; nothing talks to a broker.

## What Phase 1 adds

- `risk_engine/normalization.py` — the single point where raw,
  broker-reported cents-account values are converted into the bot's
  internal base currency (USD). No other module is allowed to divide by
  `unit_scale_factor` directly.
- `risk_engine/position_sizer.py` — `calculate_lot_size()`: the core
  risk% → lot-size formula (architecture §5.1), including the
  min-lot-exceeds-risk rejection and a `floor_to_step()` helper that
  uses `Decimal` internally to avoid binary-float rounding errors. Lot
  size is **floored, never rounded up** — the actual risk taken can only
  ever be less than or equal to what was requested, never more.
- `risk_engine/margin_calculator.py` — `calculate_required_margin()`
  and `check_margin_sufficient()`. Deliberately two separate functions:
  margin is checked *after* sizing and can only reject a trade, never
  resize one upward.
- `risk_engine/risk_limits.py` — pure functions for the daily-loss and
  max-drawdown checks, plus `ConsecutiveLossTracker`, a small stateful
  class (state is explicit and injected, not a hidden global) that
  enforces a cooldown window once the configured consecutive-loss count
  is hit.
- `risk_engine/risk_gate.py` — `evaluate_signal()`: the orchestrator.
  This is the **only** function allowed to turn a `Signal` into a
  `TradeRequest`. Check order is deliberate: account-level halts (open
  positions, daily loss, drawdown, consecutive-loss cooldown) are
  evaluated *before* any sizing or margin math runs.

## Test coverage (57 new tests, 97 total)

`tests/unit/test_position_sizer.py`, `test_margin_calculator.py`,
`test_risk_limits.py`, `test_normalization.py`, `test_risk_gate.py`.
Notable adversarial cases actually exercised:

- Property-style check that actual risk % never exceeds requested risk %
  across a grid of equity/stop-distance combinations (flooring can only
  reduce risk, never increase it).
- Minimum-lot-exceeds-risk rejection, and the narrow case where a
  configured tolerance makes it acceptable.
- Zero/negative equity, zero stop-loss distance, requested risk % above
  the configured max.
- Drawdown measured from all-time peak vs. daily-loss measured from
  day-start — a slow multi-day bleed is caught by drawdown even when no
  single day's loss looks alarming.
- Consecutive-loss cooldown timing (starts, persists, expires, resets on
  a win).
- Cents-account normalization: a $10,000 real deposit reported as
  1,000,000 by the broker produces identical drawdown/risk percentages
  as if the account were natively USD.
- Full risk-gate pipeline check-order: an account that's already halted
  rejects on the halt reason even when the signal would *also* fail
  sizing, proving halts are evaluated first.

Run everything: `pytest -v` (97 passed, 0 warnings as of this phase).

## What's next (Phase 2)

Trade filters — spread filter, duplicate-order guard, session filter —
composed into a filter chain that runs alongside the risk gate before a
signal reaches execution. Still no market data feed or strategy.

---

# Phase 2 — Trade Filters

**Status: complete.** Still no market data feed, strategy, or execution
code — filters are exercised entirely with mocked spread values and
constructed timestamps, exactly as the architecture's phase plan
specifies ("unit tests with mocked market states").

## What Phase 2 adds

- `trade_filters/spread_filter.py` — `check_spread()`: one comparison
  against `FilterConfig.max_spread_points`. Deliberately the simplest
  filter — no state, nothing to get wrong.
- `trade_filters/session_filter.py` — parses simple session strings
  (`"MON-FRI:01:00-23:58"`, `"FRI:01:00-23:57"`) into weekday+time
  windows, all interpreted as UTC. `allowed_sessions: null` (the current
  default in every config file) means no restriction at all — this
  filter is opt-in, matching the brief's "optional" framing for session
  restrictions. Multiple windows are OR'd together, so Mon–Thu and
  Friday can have different hours in the same list.
- `trade_filters/duplicate_order_guard.py` — `DuplicateOrderGuard`, a
  small stateful class (same shape as `ConsecutiveLossTracker`) that
  blocks a matching symbol/direction/entry-price signal from being
  resubmitted within a debounce window. Important design choice:
  `check()` is read-only — it never records automatically. Recording
  only happens via an explicit `record()` call, which the orchestrator
  will invoke once a signal is *fully* approved (passed both filters
  and the risk gate) — so a signal that fails the risk gate doesn't
  permanently block a legitimate retry of the same idea.
- `trade_filters/filter_chain.py` — `run_filter_chain()`: runs
  spread → session → duplicate in that order, short-circuiting on the
  first failure. Kept as a fully separate module from `risk_gate`
  because they answer different questions: the risk gate asks "is this
  trade sized/margined safely for this account", the filter chain asks
  "is right now/this exact idea okay to trade at all."

## Test coverage (37 new tests, 134 total)

Notable cases:

- Spread filter boundary behavior (exactly at the limit passes; anything
  over rejects; negative spread raises rather than silently passing).
- Session string parsing, including a wrapping weekday range
  (`FRI-MON`) and confirming a signal outside every configured window on
  a Sunday is rejected while `allowed_sessions: null` never restricts
  anything.
- Duplicate guard: exact-match vs. price-tolerance matching, debounce
  expiry, and the record-vs-check separation (checking twice without
  recording never trips a false duplicate).
- `test_combined_pipeline.py` — filters and the risk gate composed
  together in the order the eventual state machine will call them,
  including a case that would fail on *both* a filter and a risk check,
  proving the filter chain runs first and short-circuits before the
  risk gate is ever invoked.

Run everything: `pytest -v` (134 passed, 0 warnings, 0 skips as of this
phase).

## What's next (Phase 3)

The event-driven backtesting engine and historical data feed — built
first against a trivial placeholder strategy to validate the engine's
mechanics (fills, spread/commission/slippage costs, and reuse of the
exact same `risk_gate`/`filter_chain` code built in Phases 1–2)
independent of any real strategy's quality.

---

# Phase 3 — Backtesting Engine

**Status: complete.** Still no real strategy — signals in every test come
from either a fully deterministic placeholder or a single-shot test
strategy built specifically to isolate one trade's lifecycle for
hand-computation. The engine reuses `risk_engine.risk_gate.evaluate_signal()`
and `trade_filters.filter_chain.run_filter_chain()` from Phases 1–2
completely unmodified — this is checked explicitly by
`TestRiskGateRejectionSurfacesInBacktest`, which forces a daily-loss
breach and confirms the backtest engine actually blocks the trade rather
than running some simplified copy of the risk logic.

## What Phase 3 adds

- `market_data/feed_interface.py` + `historical_feed.py` — a minimal
  feed abstraction (just "yield Candles in order") and a `HistoricalFeed`
  that replays a fixed candle list or loads one from CSV. This is the
  only broker/data-source-specific piece the engine touches; a live cTrader
  feed (Phase 5) implements the same interface without the engine
  changing at all.
- `strategy/strategy_interface.py` — the `Strategy` abstract base and
  `MarketState` context object. No import path exists from this module
  to `risk_engine` — a strategy literally cannot see account equity or
  touch position sizing.
- `strategy/placeholder_strategy.py` — `AlternatingIntervalStrategy`,
  explicitly labeled FOR ENGINE VALIDATION ONLY in its own docstring.
  Real strategy work is Phase 4, untouched by this.
- `backtesting/cost_model.py` — commission calculation and pure
  functions for spread/slippage price application. Modeling
  simplifications (spread charged only at entry, slippage always
  adverse at both ends) are documented in the module docstring rather
  than left implicit. **Rewritten during the XM/cTrader broker
  migration** — commission is now calculated per-fill from notional
  volume (`commission_for_notional()`), not as a flat per-lot rate,
  since that's how XM actually prices it.
- `backtesting/slippage_model.py` — `FixedSlippageModel` and
  `VolatilitySlippageModel` (scales with a candle's own high-low range
  as a lightweight volatility proxy).
- `backtesting/swap_model.py` — **added during the XM/cTrader migration**,
  not part of the original Phase 3 scope. Calculates overnight financing
  cost from cTrader's real swap point values (Wednesday tripling,
  weekend skipped), gated behind `BrokerConfig.swap_enabled` (default
  `False` — see "Account/broker context" above for why swap isn't
  assumed either way).
- `backtesting/backtest_engine.py` — `BacktestEngine`, the event-driven
  simulation loop. Its own docstring states the Phase-3 simplifications
  plainly: equity updates only on trade close (no intrabar floating
  P&L/mark-to-market yet — that's Phase 5's `position_monitor`), and if
  one candle's range touches both SL and TP the stop-loss is assumed
  hit first (worst case).
- `backtesting/performance_report.py` — profit factor, expectancy, max
  drawdown (amount and %), win rate, average win/loss, longest losing
  streak, total trades, total return.
- `backtesting/monte_carlo.py` — resamples the realized trade P&L
  sequence with replacement to produce 5th/50th/95th percentile
  final-equity and max-drawdown outcomes.
- `backtesting/stress_tests.py` — `SpreadMultipliedFeed`,
  `ScaledSlippageModel`, and `GapRiskSlippageModel` wrap the existing
  feed/slippage interfaces rather than reimplementing the engine.
  Predefined scenarios: `SPREAD_BLOWOUT`, `SLIPPAGE_SHOCK`,
  `GAP_THROUGH_STOP`, `COMBINED_STRESS`.

## Test coverage (70 new tests, 204 total)

The centerpiece is `test_backtest_engine.py::TestHandComputedSingleTrade`:
a single long trade's entire lifecycle — entry fill price, exit fill
price, commission, and net P&L — computed by hand in the test's own
docstring (spread=$0.10, slippage=$0.20 each way, commission=$0.649809
round trip, net P&L=$57.850191) and asserted to the exact cent. A bug
anywhere in the fill/cost wiring would break an exact-number assertion,
not a loose "trades list is non-empty" check.

Also notable:

- `test_performance_report.py::TestHandComputedToyExample` — a 5-trade
  toy sequence with every metric (profit factor, expectancy, win rate,
  drawdown, losing streak) computed by hand in a docstring and checked
  against the function's output, plus a proof that shuffling the input
  trade order doesn't change the result (internal sort by `closed_at`).
- `test_monte_carlo.py` — determinism under a fixed seed, and a sanity
  check that an all-winning trade sequence never produces a nonzero
  simulated drawdown.
- `test_stress_tests.py` — confirms each stress wrapper actually
  modifies only what it claims to (spread blowout never touches
  slippage, slippage shock never touches spread) and that gap-risk
  slippage is probabilistic but seeded/reproducible.

Run everything: `pytest -v` (204 passed, 0 warnings, 0 skips as of this
phase).

## What's next (Phase 4)

Real strategy development — scratch-built, plugging into the same
`Strategy` interface with no changes needed to the engine, risk gate, or
filter chain. This is the first phase where the bot's actual trading
logic gets designed.

---

# Phase 4 — Strategy: Trend-Pullback (H1 bias, M15 execution)

**Status: complete.** A real, reasoned strategy — not a placeholder —
plugging into the exact `Strategy` interface from Phase 3 with zero
changes to the engine, risk gate, or filters.

## Design

- **H1 trend filter** (EMA50 vs EMA200, resampled from the M15 feed):
  only trade with the higher-timeframe trend. Gold trends hard during
  macro/rate moves; fighting the trend is where most retail losses
  come from.
- **M15 pullback entry**: wait for price to dip to the M15 EMA20 against
  the trend, then enter on a confirmed close back through it with a
  same-direction candle — buying dips in an uptrend rather than chasing
  breakouts at their worst average price.
- **RSI filter**: rejects entries at momentum extremes in the trade's
  own direction, avoiding the worst-quality subset of pullback entries
  (a "bounce" that's actually exhaustion).
- **ATR-adaptive stop/target**: stop = 1.5x ATR(14), target = 2x that
  distance (2:1 reward:risk). Ties stop distance to *current volatility*
  instead of a fixed dollar amount — the single change that most
  improves a stop-loss's real-world quality across gold's different
  volatility regimes.

None of this touches position sizing, margin, or account-level risk
limits — those stay exclusively in `risk_engine`, reached only through
`risk_gate.evaluate_signal()`.

## What Phase 4 adds

- `strategy/indicators.py` — `ema()`, `atr()`, `rsi()`. Pure functions,
  each hand-verified against manually computed sequences in
  `test_indicators.py` (Wilder smoothing for ATR/RSI, SMA-seeded EMA).
- `strategy/resample.py` — `resample_to_higher_timeframe()`, letting the
  strategy derive an H1 view from the engine's single M15 feed without
  a second data source. Its single most important property: **the
  currently-forming higher-timeframe bucket is never emitted** — every
  test in `test_resample.py` exists to catch a look-ahead-bias
  regression here, since including a still-forming bucket would let a
  backtest see information it couldn't have had live.
- `strategy/trend_pullback_strategy.py` — `TrendPullbackStrategy`, the
  actual strategy, fully documented with the reasoning behind each
  design choice inline.
- `scripts/run_demo_backtest.py` — an end-to-end demonstration: runs the
  real strategy through the full pipeline (risk gate, filters, cost
  model, slippage, performance report, Monte Carlo, one stress scenario)
  against **synthetic random-walk price data**, clearly labeled as such.
  Result: profit factor 0.85, -6.2% return over 12,000 candles — a
  losing result, and *correctly* so, since a trend-following strategy
  has no edge on data with no real trending structure. The point of this
  script isn't the P&L number; it's proof the full system runs correctly
  under sustained load: 155 trades executed, 519 signals correctly
  rejected (248 by the max-drawdown gate once equity crossed -10%, 271
  by the consecutive-loss cooldown), Monte Carlo and a spread-blowout
  stress scenario both completing without error. **The real test of this
  strategy is against actual XAUUSD M15 history — run this same script
  with `HistoricalFeed.from_csv()` pointed at real data before drawing
  any conclusion about edge.**

## Test coverage (49 new tests, 258 total)

- `test_indicators.py` — EMA/ATR/RSI hand-computed to the decimal,
  including a specific test that Wilder smoothing pulls a value toward
  a spike gradually rather than averaging it in equally like a simple
  moving average would.
- `test_resample.py` — bucket alignment to the clock (not stream start),
  multi-bucket aggregation, and the look-ahead-bias exclusion described
  above, including a case where the in-progress bucket has an almost
  full hour of candles and must still be dropped.
- `test_trend_pullback_strategy.py` — a bug caught during test-writing
  itself is worth noting: an early version of the test helper defaulted
  every candle's `open` to equal its `close`, which made the strategy's
  bullish/bearish confirmation check (`close` vs. `open`) permanently
  impossible to satisfy — every entry-side test failed until the helper
  was fixed to give candles realistic OHLC continuity (`open` = prior
  `close`). The final suite verifies trend-only signaling (never against
  the H1 trend), reward:risk ratio and ATR-multiple stop distance match
  configured values exactly, the RSI and minimum-ATR filters are
  provably load-bearing (compared directly against a permissive
  baseline that does produce signals on identical data), and a
  perfectly flat market produces no trend and no signal.

Run everything: `pytest -v` (258 passed, 0 warnings, 0 skips as of this
phase).

## What's next (Phase 5)

Execution layer (cTrader order placement) and paper trading — the first
phase involving a real broker connection. Before that, the strategy
above should be validated against real XAUUSD M15 history, not the
synthetic data used for the pipeline demonstration.

---

# Phase 5 — Execution, Position Monitoring, State Machine, Kill Switch

**Status: mixed.** Everything that can be run and tested in this
environment (no live network access to cTrader) is fully built and
tested. The two files that genuinely require a live broker connection
(`market_data/ctrader_feed.py`, `execution/ctrader_executor.py`) are
**structural placeholders, explicitly unverified** — every protocol-level
detail in them (message field names, symbol ID, volume units) needs your
own verification against a cTrader demo account before real use. See
each file's prominent module docstring for exactly what to check.

## What's fully built and tested

- `position_monitor/exit_logic.py` — SL/TP-hit detection and
  unrealized-P&L calculation, extracted from what Phase 3's backtest
  engine had inline, so live and backtest now share the exact same
  exit-condition code (`backtest_engine.py` was refactored to call this
  shared function too — re-ran the full Phase 3 suite after the
  refactor to confirm zero behavior change).
- `position_monitor/account_tracker.py` — `AccountTracker`, the real
  capability gap this phase closes: Phase 3's backtest engine only
  updated equity when a trade closed, so a drawdown breach could only
  block NEW trades, never react to a floating loss on one already open.
  `AccountTracker.get_live_account_state()` marks the open position to
  market on every call, giving the kill switch real-time equity that
  includes unrealized P&L.
- `position_monitor/kill_switch.py` — automatic-only (drawdown breach or
  broker disconnect, per the decision log), stays tripped until an
  explicit `rearm()` call that nothing in this codebase ever makes
  automatically.
- `execution/execution_interface.py` + `execution/paper_executor.py` —
  the abstract interface and a fully-simulated paper executor, reusing
  the exact `CostModel`/slippage/spread functions from Phase 3.
- `state_machine/bot_state_machine.py` — the real-time orchestration
  loop (architecture §6), reusing `strategy.generate_signal()`,
  `run_filter_chain()`, `evaluate_signal()`, and `check_stop_or_target()`
  completely unmodified. `KILL_SWITCH_ACTIVE` is checked first on every
  tick, from any prior state, exactly as the architecture's "global
  interrupt" design requires.
- `orchestrator/bot_runner.py` — wires config + strategy + execution +
  feed together per environment (`run_backtest`, `run_paper`,
  `build_live_state_machine`).
- `scripts/run_demo_paper.py` — paper-mode counterpart to Phase 4's
  backtest demo, replaying the same synthetic data through the real
  state machine. Result: kill switch correctly triggered at **10.11%**
  drawdown (vs. **10.25%** in the Phase 3 backtest demo on similar data)
  — the tighter number is a direct, visible consequence of Phase 5's
  mark-to-market equity actually force-closing the open position the
  moment the breach occurred, rather than only blocking new entries.

## cTrader integration: real implementation, not a placeholder

`market_data/ctrader_feed.py` and `execution/ctrader_executor.py` were
rewritten after installing the actual `ctrader-open-api` package and
inspecting its protobuf message definitions directly (not from memory
or documentation) — every message field name, enum value, and the
Deferred-based async pattern used below were confirmed against the
installed package's real Python classes.

**Genuinely verified this way:** ProtoOAApplicationAuthReq/AccountAuthReq
field names, ProtoOANewOrderReq/ClosePositionReq/AmendPositionSLTPReq
field names, ProtoOAExecutionType enum values (ORDER_FILLED,
ORDER_PARTIAL_FILL, ORDER_REJECTED), ProtoOATradeSide/OrderType enum
values, ProtoOASpotEvent's bid/ask/timestamp fields, message routing via
`payloadType` + `Protobuf.extract()`, and that `Client.send()` returns a
Twisted Deferred requiring a background reactor thread.

**A real bug found and fixed during testing, not just inspection:**
the first version had each `CTraderExecutor`/`CTraderFeed` instance
spawn its own thread calling `reactor.run()` — Twisted's reactor is a
process-wide singleton, so running a feed and an executor together (the
normal case for live/paper trading) would crash with
`ReactorAlreadyRunning`. Fixed with a small shared
`market_data/ctrader_reactor.py` that makes starting the reactor
idempotent across every instance. This was caught by actually running
the test suite, not by reading the code.

**Still genuinely unverifiable without a live connection** (this
environment has no network route to cTrader's servers): whether a
market order's fill arrives via the `ProtoOAExecutionEvent` this code
waits for or some other path on XM's specific server config; XM's exact
`symbolId` for XAUUSD (must be looked up per-account via
`ProtoOASymbolsListReq`); the centilot volume convention for this
specific symbol; and cTrader OAuth app credentials, which are set up
through cTrader's own developer portal, separate from your XM login.
Each file's module docstring lists these explicitly. Partial fills are
deliberately treated as a rejection rather than handled, since silently
mishandling an ambiguous fill is worse than stopping and asking for
help.

**Test coverage:** local logic (tick-to-candle aggregation, not-connected
error paths, the duplicate-position guard) is fully tested — 13 tests in
`test_ctrader_integration.py`. Actual `connect()` calls are tested only
for fail-closed behavior (a connection attempt that can't succeed must
raise, never silently report success), using a short timeout, since
this environment can't reach a real server to test protocol correctness
itself.

## Test coverage (76 new tests, 384 total)

- `test_exit_logic.py`, `test_account_tracker.py`, `test_kill_switch.py`
  — unit tests for each new building block in isolation.
- `test_paper_executor.py` — fill price/slippage correctness, duplicate
  rejection, close/modify error paths.
- `test_state_machine.py` — the integration suite. Notable case:
  `TestKillSwitchForcesPositionClosed` required working out, by hand,
  why a single trade's worst-case loss essentially can never breach
  account-level drawdown on its own (risk-based sizing caps it near the
  configured risk %, well below a 5-10% drawdown threshold) — so the
  test simulates an account already close to the boundary from prior
  activity, then shows a small floating loss (well within the position's
  own stop distance) tips it over, with an explicit assertion that the
  candle's low never actually reached the stop-loss — proving the close
  was forced by the kill switch, not the natural exit.
- `test_orchestrator.py`, `test_ctrader_placeholders.py` — wiring and
  not-connected/unimplemented-placeholder error paths.

Run everything: `pytest -v` (332 passed, 0 warnings, 0 skips as of this
phase).

## What's next (Phase 6 — Live Readiness Review)

Per the architecture's phase plan: verify all risk limits against the
running config, manually test the kill switch and daily/drawdown halts,
confirm logging/DB completeness, and manually simulate broker failure
scenarios. Phase 6 proceeds on the infrastructure built through Phase 5,
which is real and correct independent of which strategy eventually runs
on it — see the Strategy Research section below for why no strategy is
recommended for live capital yet.

---

# Strategy Research — Real-Data Validation (post-Phase-4)

**Status: no strategy from this research shows a demonstrated edge.**
This section exists so that finding is preserved as a real project
artifact, not lost in conversation history — anyone picking this up
later should not have to redo this work blind.

## What was tested

Real XAUUSD M15 OHLC data (2012–2022, ~230K candles) was sourced and
used — not synthetic data — for every result below. Two fundamentally
different paradigms, several exit mechanisms, and two timeframe pairs
were tested across three real market regimes: a choppy period (2019),
a strongly trending period (the 2020 COVID gold rally, $1543→$2026),
and the full 10-year dataset resampled to H4/D1 for a swing-timeframe
test.

| Approach | Timeframe | Period | Profit Factor |
|---|---|---|---|
| Trend-follow, EMA-cross pullback, fixed 2:1 target | M15/H1 | 2019 | 0.91 |
| same | M15/H1 | COVID rally | 0.83 |
| Trend-follow, structural breakout entry, fixed 2:1 | M15/H1 | 2019 | 0.82 |
| same | M15/H1 | COVID rally | 0.75 |
| same + wider stop buffer | M15/H1 | 2019 | 0.95 |
| same + wider stop buffer | M15/H1 | COVID rally | 0.77 |
| same + trailing stop (1.5×ATR) | M15/H1 | 2019 | 0.60 |
| same + trailing stop (1.5×ATR) | M15/H1 | COVID rally | 0.39 |
| same + trailing stop (2.0×ATR) | M15/H1 | COVID rally | 0.36 |
| Mean-reversion (fade the stretch) | M15 | 2019 | 0.50 |
| same | M15 | COVID rally | 0.75 |
| same + HTF trend filter | M15 | COVID rally | 0.76 |
| Trend-follow, swing entry, fixed 2:1 | H4/D1 | **full 10 years** | 0.58 |
| same + trailing stop | H4/D1 | full 10 years | 0.44 |

**Every result is below breakeven (PF < 1.0).** The pattern that matters
most: losses did not improve, and in several cases got worse, when
moving from a choppy period to a strongly trending one where a
trend-following approach should have its best chance. That rules out
"unlucky test window" and points at the entry/exit mechanics themselves
— classical technical indicators (EMA crosses, ATR-based stops, RSI
filters, pullback/breakout timing) did not show a discoverable edge on
real XAUUSD with the techniques available here.

## Reusable code from this research

- `strategy/trend_pullback_breakout_strategy.py` — the redesigned,
  more selective entry (multi-bar pullback + structural breakout of the
  pullback's own extreme, trend-strength filter, structure-based stop).
  Meaningfully reduced losses and drawdown vs. the original v1, even
  though it didn't reach profitability.
- `strategy/mean_reversion_strategy.py` — fade-the-stretch entry with
  an optional HTF trend filter.
- `position_monitor/trailing_stop.py` — a genuine trailing-stop
  capability (state tracking, ATR-based trail, stop-only-tightens
  guarantee), fully tested and integrated into `backtest_engine.py`,
  reusable by any future strategy that sets `take_profit=None`.
- `strategy/trend_pullback_strategy.py`'s `max_history_candles`
  windowing fix — recomputing indicators from a growing full history is
  O(n²) over a backtest and was estimated at **14+ hours** on the full
  10-year dataset before being fixed to a bounded, constant-cost window.
  This isn't just a backtest concern: an unbounded live bot would face
  the same growing per-tick cost over time.
- `scripts/param_test_harness.py` — the variant-comparison tool used to
  produce the table above; reusable for any future strategy research.

## Honest assessment and recommendation

This is consistent with a known reality in quantitative finance: liquid,
heavily-traded instruments tend to arbitrage away exactly the kind of
publicly-known technical patterns available to test here. Genuine edges
more often require proprietary data (order flow, sentiment), rigorous
walk-forward statistical validation at a scale beyond what a
conversation can produce, or considerably more sophisticated modeling.

No strategy from this repository should be pointed at real capital.
Recommended next steps, in order: (1) continue strategy research
separately, ideally with a longer validation methodology (proper
walk-forward, out-of-sample splits, more data) than fits in one session;
(2) once — and only once — a strategy shows genuine, robustly-validated
edge, run it through Phase 6's live-readiness checklist before ever
touching a live or even demo account.

---

# Phase 6 — Live Readiness Review

**Status: complete.** `scripts/phase6_readiness_check.py` runs the
architecture's readiness checklist as executable checks against the
actual running config and code — proof, not a manual claim. **17/17
passed.**

- **Risk limits config-verified** against `live.yaml`: default risk
  0.35% (in range), max risk capped at 1%, max_open_positions=1, daily
  loss limit 2%, max drawdown 10%, kill switch automatic-only with
  mandatory manual rearm. One flagged item carried over from Phase 0:
  `max_spread_points` is still the 35-point placeholder — replace with
  an observed live spread before real use.
- **Kill switch manually triggered and confirmed** for both triggers:
  drawdown breach (11% vs 10% limit) and broker disconnect (even with
  healthy equity) — both trip the switch, and it stays tripped until an
  explicit `rearm()`, never automatically.
- **Daily loss halt triggered and confirmed**: a 3% intraday loss
  against the 2% limit correctly blocks new trades.
- **Logging/DB completeness confirmed**: every required event type
  (boot, signal generated/rejected, order filled, position closed,
  error, kill switch triggered) persists to the database, and the
  schema contains no UPDATE/DELETE against the events table — append-only
  as designed.
- **Broker failure scenarios simulated and confirmed handled safely**:
  an order attempted before any market data raises
  `BrokerConnectionError` rather than failing silently; a duplicate
  order for an already-open symbol raises `OrderRejectedError` rather
  than silently overwriting position state; a broker disconnect
  mid-run correctly forces `KILL_SWITCH_ACTIVE` without crashing the
  state machine.

**Bottom line:** the infrastructure passes live-readiness review. No
strategy does yet (see Strategy Research above) — that remains the one
open item before this bot should touch real capital.
