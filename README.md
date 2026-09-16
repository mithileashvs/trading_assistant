# XAU/USD Trading Assistant — Phases 1–10 (Full Pipeline)

A regime-adaptive, risk-controlled research platform for XAU/USD via MT5.

**Phase 1 (Foundation):** configuration, logging, the MT5 adapter
(mock + real), symbol discovery, account info, and OHLCV/tick retrieval.

**Phase 2 (Feature Engine):** trend (EMA/SMA/ADX/+DI/-DI, market
structure via swing highs/lows), momentum (RSI/MACD/ROC/momentum),
volatility (ATR/ATR%/Bollinger Bands & width/historical volatility/
range expansion), volume (relative tick volume — MT5 doesn't provide
centralized-exchange volume, so this is documented explicitly),
price structure (support/resistance, previous-day high/low, recent
range, breakout levels), and session detection (Asian/London/NY/
overlap). `compute_features()` in `app/features/engine.py` assembles
all of it into one JSON-serializable snapshot per timeframe.

**Phase 3 (Regime Detector):** deterministic, rule-based classification
into `TREND_BULLISH` / `TREND_BEARISH` / `RANGE` / `HIGH_VOLATILITY` /
`UNCERTAIN`, given a feature snapshot from Phase 2. Every call to
`detect_regime()` returns the `{regime, confidence, reasons}`
explainability object from section 7 of the spec. Thresholds are
pydantic Settings (env-prefixed `REGIME_`), not hard-coded constants.

**Phase 4 (Strategies + Selector + Scoring):** three independently
testable strategies — Trend Pullback, Volatility Breakout, Mean
Reversion — each returning a `Signal` (`NO_SIGNAL` is a first-class,
expected outcome). `StrategySelector` maps the H4 regime to which
strategies are allowed to run (section 11: `UNCERTAIN` → no strategies,
NO TRADE by design) and picks the best-scoring actionable signal.
`app/signals/scoring.py` implements the section-12 transparent
additive scoring system with configurable weights and interpretation
breakpoints (`NO_TRADE`/`WEAK`/`VALID`/`STRONG`).

**Phase 5 (Risk Engine + Guards + Kill Switch):** position sizing
derived from the broker's actual symbol spec (never a hard-coded lot
size), the full risk-guard suite from section 16 (daily/weekly loss,
trade/position limits, consecutive losses, spread, min equity,
connection/data-freshness checks), a file-backed kill switch that
persists across restarts and requires explicit, named reactivation,
and a `TradeValidator` that produces the exact `{symbol, direction,
strategy, regime, score, entry, stop_loss, take_profit, risk_reward,
risk_percent, spread_ok, news_ok, daily_loss_limit_ok,
position_limit_ok, market_data_fresh, approved}` object from section
20 before every (hypothetical) order. A stub `NewsFilter` honestly
reports itself as unavailable rather than pretending to filter news it
has no data for (section 18).

**Phase 6 (Backtesting Engine + Metrics):** a walk-forward simulation
engine (`app/backtesting/engine.py`) that reuses the exact same
feature engine, regime detector, strategy selector, scoring, position
sizing, and trade validator as live/paper trading — a backtest and a
live run differ only in where market data and fills come from. Every
decision is made from a bar's close and filled at the *next* bar's
open; H1/H4 context is derived using only fully-closed higher-timeframe
candles (`app/backtesting/resampling.py`) — both are verified by a
dedicated no-look-ahead test that reruns the engine on a truncated
dataset and checks trades before the truncation point are byte-for-byte
identical. Realistic spread/slippage/commission/swap costs
(`app/backtesting/costs.py`) and the full metrics suite from section 25
(`app/backtesting/metrics.py`) — win rate, profit factor, expectancy,
Sharpe/Sortino/Calmar, max drawdown (with duration), MAE/MFE, and
breakdowns by year/month/session/regime/strategy/direction — are
included. Partial exits and trailing stops are explicitly deferred
(see notes below) rather than faked.

**Phase 7 (Strategy Laboratory):** `app/strategy_lab/` — chronological
in-sample/out-of-sample splitting and walk-forward folds (section 26),
parameter sensitivity sweeps with fragility flagging (section 27),
Monte Carlo trade-sequence bootstrapping for drawdown/losing-streak
distributions (section 28), and single-strategy-at-a-time comparison
(section 29). No re-optimization happens anywhere — this system
doesn't fit parameters, so "walk-forward" here means checking whether
the same fixed rules hold up across sequential time windows, not
re-fitting per fold.

**Phase 8 (Paper Trading) + Phase 9 (MT5 Demo) + Phase 10 (Live):**
one unified `TradingLoop` (`app/runtime/loop.py`) runs identically
across PAPER, demo, and LIVE — the same code, only `ExecutionEngine`'s
fill path differs (simulated vs. a real `IMT5Client.submit_order`
call). New for this stage: `app/journal/journal.py` (the real
SQLite-backed Trade Journal from section 23), `app/execution/engine.py`
(duplicate-order protection, broker reconciliation), and
`app/positions/monitor.py` (breakeven, trailing stop, partial exit —
the position-management sophistication the backtest engine explicitly
deferred). `TradingLoop` refuses to construct in an unsafe LIVE
configuration and surfaces whether the connected account is DEMO or
REAL-MONEY at startup.

**Honest limitation**: none of Phases 8-10 have been exercised against
a real MT5 terminal or broker connection — this sandbox can't provide
one (see Phase 1's note on `RealMT5Client`). Everything is tested
against `MockMT5Client`, which implements the exact same `IMT5Client`
interface `RealMT5Client` does, so the code paths are structurally
exercised, but genuine demo-account behavior (execution latency,
requotes, real broker rejections) is untested. Treat Phases 9-10 as
"ready to try on a demo account," not "verified on one."

**Also now built**: a real, working `CalendarNewsFilter`
(`app/news/calendar.py`) — the section 18 stub remains the default
because this sandbox has no network path to a live calendar API, but
once you supply a CSV/JSON economic-calendar export it does real
blackout-window filtering, not a simulation. An **AI layer**
(`app/ai/`) with deterministic trade explainability (section 32 — no
LLM needed), natural-language config translation with a hard-coded
field allowlist that can never touch `trading_mode` or `kill_switch`
(section 39), deterministic market/backtest/performance summarization,
and structured trade-history querying. And a **FastAPI dashboard**
(`app/api/`, section 33) covering account/market/signal/positions/
risk/performance, backed by the exact same components the trading
loop uses — plus a lightweight single-page HTML frontend.

## Why a mock MT5 client exists

The `MetaTrader5` Python package only works alongside a running MT5
terminal (Windows, or Wine on Linux/Mac) connected to a broker. It
cannot be used from a headless Linux container. `MockMT5Client`
provides deterministic synthetic OHLCV/tick data behind the exact same
`IMT5Client` interface, so the rest of the system can be built and
tested without a live connection. Switching to the real client is one
setting: `MT5_USE_MOCK=false` in `.env`, plus installing `MetaTrader5`
on a machine where the terminal is actually installed.

**`MT5_USE_MOCK=true` must never be used for LIVE trading** —
`Settings.validate_live_safety()` refuses to start in LIVE mode while
it's set, and `main.py` calls that check before doing anything else.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env                # edit if you want non-default settings
python main.py                      # Phase 1 smoke test (mock data by default)
pytest                              # run the test suite
```

Default `.env` runs entirely on mock data — no MT5 terminal or broker
account required to try it.

To use a real MT5 terminal:
1. Install MT5 and log into a **demo account first**.
2. `pip install MetaTrader5` (Windows, or Wine on Linux/Mac).
3. In `.env`: set `MT5_USE_MOCK=false`, `MT5_LOGIN`, `MT5_PASSWORD`,
   `MT5_SERVER`, and optionally `MT5_TERMINAL_PATH`.
4. Run `python main.py` again — it will connect for real, run symbol
   discovery against your broker's actual symbol names, and fetch
   live account info / OHLCV / tick data. It still places no orders.

## Trading modes

`TRADING_MODE` is `PAPER` by default. `LIVE` requires, simultaneously:
`MT5_USE_MOCK=false`, valid `MT5_LOGIN`/`MT5_PASSWORD`/`MT5_SERVER`,
and `KILL_SWITCH=false`. Missing any of these causes a hard refusal to
start, with the specific reason logged. (Order placement itself isn't
implemented until the execution engine in Phase 9/10 — this guard is
in place now so the safety pattern is established from day one.)

## Project layout

```text
app/
  config/        Settings (pydantic), trading modes, risk config      [Phase 1]
  mt5/           IMT5Client interface, Mock + Real implementations,    [Phase 1]
                 factory, symbol discovery
  market_data/   Symbol resolution, OHLCV/tick fetch, freshness checks [Phase 1]
  logging_config.py   Structured JSON logging, event taxonomy         [Phase 1]
  indicators/    trend/momentum/volatility/volume/structure functions [Phase 2]
  features/      FeatureEngine (compute_features) + session detection [Phase 2]
  regimes/       Regime detector (TREND_BULLISH/BEARISH/RANGE/         [Phase 3]
                 HIGH_VOLATILITY/UNCERTAIN) + configurable thresholds
  strategies/    Trend Pullback, Breakout, Mean Reversion + Selector   [Phase 4]
                 (context.py bundles H4/H1/M15 data for all of them)
  signals/       Signal model + section-12 scoring system              [Phase 4]
  risk/          Position sizing, risk guards, kill switch,            [Phase 5]
                 TradeValidator (the pre-trade approval gate)
  news/          NewsFilter stub — honestly reports itself unavailable [Phase 5]
                 rather than pretending to filter news it has no data for
  execution/     ExecutionEngine — the only code allowed to submit      [Phase 9]
                 real orders (PAPER simulates fills, LIVE submits for real)
  positions/     PositionMonitor — breakeven, trailing stop,            [Phase 9]
                 partial-exit decisions for open positions
  journal/       TradeJournal — SQLite-backed signal/trade persistence  [Phase 8]
                 (implements TradeHistoryProvider; supersedes the
                 in-memory precursor in app/risk/history.py for live/paper use)
  backtesting/   Walk-forward engine, no-lookahead resampling,          [Phase 6]
                 execution costs, section-25 metrics suite
  strategy_lab/  OOS/walk-forward splits, parameter sensitivity,        [Phase 7]
                 Monte Carlo, single-strategy comparison
  runtime/       TradingLoop — the unified PAPER/demo/LIVE orchestrator [Phase 8-10]
                 tying market data, strategy, risk, execution, and
                 the journal into one continuously-runnable cycle
  ai/            Deterministic explainability (section 32), NL config  [done]
                 translation with a hard safety allowlist, summarization,
                 trade-history querying, optional LLM client
  api/           FastAPI dashboard: account/market/signal/positions/   [done]
                 risk/performance endpoints + a lightweight HTML frontend
tests/           pytest suite (all currently run against MockMT5Client)
main.py          Phase 1 smoke test / entry point
```

## Roadmap

All of sections 1-33 of the master build prompt are now implemented
and tested end-to-end — Foundation, Feature Engine, Regime Detector,
Strategies, Risk Engine, Backtesting, Strategy Lab, Paper Trading, MT5
Demo readiness, Live gating, the AI layer, and the dashboard. The one
thing genuinely outside what this sandbox can deliver: **verified
behavior against a real MT5 terminal / broker connection** — that
requires hardware and network access this environment doesn't have.
Everything else has been built, and just as importantly, tested for
real: run the full suite and check the notes below for the bugs that
testing actually caught (not a hypothetical list — things that were
genuinely wrong and got fixed).

## Running the dashboard

```bash
PYTHONPATH=. python scripts/run_dashboard.py
# open http://localhost:8000
```

Read-only except the kill-switch activate/deactivate buttons. Backed
by the same `MarketDataEngine` / `StrategySelector` / `TradeValidator`
/ `TradeJournal` the trading loop uses — run `scripts/run_paper_trading.py`
in one terminal and the dashboard in another (pointed at the same
`DATABASE_URL`) to watch a paper session live.

## Using the AI layer

```python
from app.ai.explain import explain_trade
from app.ai.nl_config import translate, apply_proposal
from app.ai.summarize import summarize_backtest

# Deterministic explainability -- no LLM needed:
print(explain_trade(signal, regime_decision, validation).to_text())

# Natural-language config (rule-based by default; set ANTHROPIC_API_KEY
# to enable LLM-assisted extraction -- see app/ai/llm_client.py):
proposal = translate("Only trade gold when risk is less than 0.3% per trade")
new_settings = apply_proposal(settings, proposal)  # validated, never mutates the original

# Summaries work with zero configuration:
print(summarize_backtest(compute_metrics(backtest_result)))
```

`trading_mode` and `kill_switch` are permanently outside what the NL
translator can touch — see `app/ai/nl_config.py`'s `ALLOWED_*` sets and
the tests in `tests/test_ai_nl_config.py` that specifically verify an
instruction like "switch to LIVE and turn off the kill switch" cannot
produce those fields, however it's phrased.

## Configuring the news filter

```bash
# .env
NEWS_CALENDAR_PATH=/path/to/calendar.csv  # columns: time,name,impact,currency
NEWS_BLACKOUT_MINUTES_BEFORE=30
NEWS_BLACKOUT_MINUTES_AFTER=30
```

Without a configured (or valid) calendar file, `NewsFilter` falls back
to `UnavailableNewsFilter` automatically — it never pretends to filter
news it has no data for. See `app/news/calendar.py` for the CSV/JSON
format and `build_news_filter()`'s fallback behavior.

## Safety audit (external review pass)

An external safety-focused audit was conducted against this codebase,
explicitly prioritizing correctness/safety/fail-closed behavior over
feature velocity. Its process: full repository inspection, run the
existing suite as a baseline, implement exactly one safety phase at a
time, re-test, report, and stop for review before continuing — rather
than pushing through every finding in one pass. This section records
what's been done under that process so far.

**Phase 1 — Multi-timeframe data consistency (implemented).** The
audit correctly identified that `MockMT5Client.get_ohlcv()` generated
H4, H1, and M15 as independently-seeded random walks (seeded by
`hash((symbol, timeframe))`) — they did not represent the same
underlying market timeline. This is a real safety issue for any
multi-timeframe strategy: H4 trend and M15 entry timing could
legitimately disagree not because the market actually showed
conflicting signals, but because the mock's H4 and M15 series had no
relationship to each other at all. **Fixed**: the mock now generates a
single coherent M15 series per symbol (seeded by symbol only) and
derives H1/H4 by resampling that same series — verified by tests that
aggregate the returned M15 bars and assert byte-for-byte equality
against the returned H1/H4 bars, not just statistical similarity. This
did not affect `app/backtesting/engine.py`, which already resampled
correctly from a single M15 window (built in an earlier phase); it
also does not affect `RealMT5Client`, where a real broker's own
timeframes are inherently coherent.

Fixing this **surfaced two further pre-existing latent bugs** it had
been masking:

1. `MarketDataEngine`'s data-freshness check used one flat staleness
   threshold for every timeframe, which only ever worked because the
   old (buggy) mock artificially anchored every timeframe's last bar
   to within a minute of "now" regardless of realism. Once H1/H4 bars
   had realistic bucket timestamps (a bar's timestamp is its OPEN
   time — legitimately up to a full bar-period old), the flat
   threshold began rejecting perfectly fresh H1/H4 data as stale,
   which would have blocked all multi-timeframe trading. **Fixed**:
   the freshness check is now timeframe-aware (`bar_period +
   configured_buffer`), with unrecognized timeframes defaulting to the
   *strictest* known bar period (fail closed — more likely to reject,
   never more likely to silently accept stale data).
2. The mock's random-walk seeding used Python's built-in `hash()` on
   the symbol string. Python randomizes string hashing per process by
   default (`PYTHONHASHSEED`, a security feature) — so the "same" seed
   silently produced a *different* price path every fresh process run,
   even though it stayed internally consistent within one process's
   lifetime. This was invisible with the old short, independently-reseeded-
   per-call walks (little accumulated drift), but the coherence fix's
   20,000-bar shared base series accumulates enough drift that the
   resulting "current price" could swing by hundreds of dollars
   between process runs — which caused a real, intermittently-failing
   test (`test_position_closes_on_stop_loss_hit_and_journal_updated`,
   caught by running it as 10 independent fresh processes, not just
   in-process reruns). **Fixed**: seeding now uses `zlib.crc32`
   (process-stable) instead of `hash()`. Verified by a regression test
   that spawns three genuinely separate Python processes and asserts
   they all produce the identical price path.

Both original fixes are in `app/mt5/mock_client.py` and
`app/market_data/engine.py`; the hash-seeding fix is also in
`app/mt5/mock_client.py`. Tests added in `tests/test_mock_mt5_client.py`
and `tests/test_market_data_engine.py`. Full suite: **313 tests
passing, 0 regressions, confirmed stable across two independent
fresh-process full-suite runs** (baseline before this pass: 298).

**Phase 3 — Independent Safety Gate (implemented).** Per the audit's
architecture diagram (`RISK ENGINE -> SAFETY GATE -> EXECUTION SAFETY
GATE -> MT5`) and the "two independent validations" principle: a
second, **structurally separate** approval layer now sits above the
risk engine. `app/safety/gate.py`'s `SafetyGate` does not import or
call into `app.risk` at all — enforced by a test that AST-parses the
module and asserts no such import exists, not just a docstring
promise. It re-derives its own verdict on the same class of hard
constraints (kill switch, account/symbol trade permission, spread,
daily loss, position limits, stop-loss presence and directional
validity, broker minimum stop distance, take-profit direction, volume
against broker min/max/step) from its own code, so a bug in one layer
is unlikely to also exist in the other.

`TradingLoop` now calls `SafetyGate.evaluate()` immediately before
every order submission, using **freshly re-gathered** account/tick/
news/position state (not reused from the risk-engine validation a
moment earlier) — per section 19's "do not assume conditions remain
unchanged since signal generation." A trade proceeds only if BOTH the
risk engine and the safety gate independently approve; either
rejecting blocks the trade, and any internal error inside the gate is
itself treated as a rejection (fail closed, never a pass-through).

**Concretely demonstrated, not just asserted**: I constructed a
scenario where the risk engine approves a trade — because
`TradeValidator`/`RiskGuardEngine` never check the broker's minimum
stop distance (`stops_level_points`) at all — and confirmed
`SafetyGate` independently catches and rejects it, blocking the trade
end-to-end through the real `TradingLoop`. This is exactly the
property section 18 requires, verified as actual behavior rather than
assumed from the code reading correctly. The corresponding regression
test is
`tests/test_trading_loop.py::test_safety_gate_can_reject_even_when_risk_engine_approves`.

**A real design bug caught and fixed while wiring this in**: the first
wiring attempt gave `TradingLoop` two separate attributes that both
needed to hold "the configured news filter" (one on the validator, one
believed-shared with the safety gate) — reassigning one in a test
silently left the other stale, so the safety gate was checking a
different news filter than intended. This is exactly the kind of
subtle bug independent validation is supposed to catch, and it
surfaced during this phase's own development. Fixed by making
`news_filter` a property on `TradingLoop` that delegates to the
validator's single stored instance, so there is only ever one
canonical value to set.

Files changed/added: `app/safety/gate.py` (new), `app/safety/__init__.py`
(new), `app/runtime/loop.py` (SafetyGate wired in, `news_filter`
property fix), `app/logging_config.py` (`SAFETY_REJECT`/`RISK_REJECT`
event types added), `tests/test_safety_gate.py` (new, 26 tests),
`tests/test_trading_loop.py` (+2 tests: the news-filter-property
regression test and the risk-engine-approves-but-safety-gate-rejects
integration test). Full suite: **346 tests passing, 3 pre-existing
skips, 0 regressions** (baseline before this phase: 318).

**Phase 4 — Execution Safety Gate (implemented).** Per section 56
("NO DIRECT STRATEGY -> MT5 ACCESS") and the architecture diagram's
third gate (`SAFETY GATE -> EXECUTION SAFETY GATE -> MT5`): Phase 3's
`SafetyGate` is only enforced because `TradingLoop`'s orchestration
logic happens to call it before deciding to submit. That left a real
gap — any OTHER caller of `ExecutionEngine.submit_market_order()`
(a future "manual trade" UI button, a bug, a different orchestrator)
would completely bypass both the risk engine and `SafetyGate`, since
nothing forced callers to check first.

**Fixed**: `app/execution/execution_safety_gate.py`'s
`ExecutionSafetyGate` is now called from *inside*
`ExecutionEngine.submit_market_order()` itself — unconditionally,
before that function ever touches `client.submit_order()` or
fabricates a paper fill. This makes bypass structurally impossible
rather than merely against convention: every present and future caller
of that one function inherits the check automatically, because there
is no other path to MT5 submission. It re-checks kill switch, account/
symbol trade permission, direction validity, stop-loss presence,
market-data freshness, and volume against broker min/max/step, using
state gathered fresh at the moment of the call.

**Concretely demonstrated**: I called `ExecutionEngine.submit_market_order()`
*directly*, with an active kill switch, completely bypassing
`TradeValidator` and `SafetyGate` — and confirmed it was still
correctly rejected (`EXECUTION_SAFETY_GATE_REJECTED: Kill switch is
active.`, no position created). Same result for a missing stop-loss
and an out-of-range volume called directly. These are permanent
regression tests in `tests/test_execution_safety_gate.py`, not just
manual checks.

**Design note on the kill switch default**: `ExecutionEngine` now
accepts an optional `kill_switch` parameter. If none is provided, it
uses a deliberately inert `_NullKillSwitch` rather than defaulting to
a real file-backed `KillSwitch` — defaulting to a shared file path
would mean every standalone `ExecutionEngine` (tests, ad-hoc scripts)
silently inherits whatever kill-switch state happens to be on disk,
which is a correctness and test-isolation hazard, not a safety
improvement. `TradingLoop` (the actual production entry point) always
passes its own properly-scoped `KillSwitch` explicitly, so the real
deployment path is fully protected; this is documented plainly in the
code rather than left implicit.

Files changed/added: `app/execution/execution_safety_gate.py` (new),
`app/execution/engine.py` (gate wired in, `_NullKillSwitch` default),
`app/execution/__init__.py`, `app/runtime/loop.py` (passes its
`KillSwitch` into `ExecutionEngine`), `tests/test_execution_safety_gate.py`
(new, 19 tests). Full suite: **365 tests passing, 3 pre-existing
skips, 0 regressions** (baseline before this phase: 346).

**Confirmed, not yet fixed:**
- **No consolidated StartupSafetyCheck** module.
- **Unknown MT5 order results** aren't yet triaged into
  `ORDER_NOT_SENT` / `ORDER_SENT_UNKNOWN_RESULT` / `ORDER_CONFIRMED`.

This system has not undergone the audit's full review, and —
regardless of test count — **is not represented as ready for live
trading**. See "No false safety claims" in the Philosophy section
below.

**Phase 2 — News state semantics (implemented).** The audit correctly
identified a dangerous ambiguity: `NewsStatus` exposed two independent
booleans (`available`, `blackout_active`), and `TradeValidator`
derived safety as `news_ok = not blackout_active`. An unavailable news
filter reports `available=False`, and `blackout_active` defaults to
`False` — so `news_ok` silently evaluated to `True`. **This was a
live, exploitable bug**, not a hypothetical: with the *default*
configuration (no calendar file set — which is the out-of-the-box
state), every trade validation up to this point in the project would
have proceeded as if news were confirmed clear, when in fact nothing
had confirmed anything.

**Fixed**: `NewsStatus` now carries an explicit `NewsState` enum
(`CLEAR` / `BLOCKED` / `UNAVAILABLE` / `UNKNOWN`) instead of two
booleans, with exactly one state (`CLEAR`) permitting a new trade —
enforced via a `permits_new_trade` property rather than a boolean
callers recompute themselves, specifically so this bug class can't
silently reappear the next time someone touches this code.
`CalendarNewsFilter` also now tracks its own coverage window and
returns `UNKNOWN` (not `CLEAR`) for any query outside the time range
its loaded events actually span — "no events listed near this time"
is a different claim from "the calendar confirms nothing is scheduled
near this time," and a calendar file that simply hasn't been refreshed
far enough into the future can't honestly make the second claim.

**Major, deliberate behavioral consequence**: with this fix, the
system's out-of-the-box default configuration (no `NEWS_CALENDAR_PATH`
set) now **permanently blocks every new trade** — `UnavailableNewsFilter`
reports `UNAVAILABLE`, which never permits approval. This is not a
regression; it is section 27's explicit rule ("UNAVAILABLE: no new
trade") working as specified. A real deployment must configure an
actual news calendar (`app/news/calendar.py`) before the system can
ever approve a trade. Every test and code path that previously
exercised "signal gets approved" now explicitly injects a stub
clear-news filter to test that path in isolation — see
`tests/test_trading_loop.py::test_default_news_filter_blocks_all_new_trades`
for the direct regression test proving the new default behavior, and
grep for `_AlwaysClear` across the test suite for how the "happy path"
tests now make that override explicit rather than relying on it being
the accidental default.

Files changed: `app/news/filter.py` (rewritten), `app/news/calendar.py`,
`app/risk/validator.py`, plus five test files updated to reflect the
corrected (stricter) default. Full suite: **318 tests passing, 3
pre-existing skips, 0 regressions** (baseline before this phase: 313).



1. Get a real MT5 terminal running (Windows, or Wine) with a **demo
   account** logged in first. Never start with a real-money account.
2. `pip install MetaTrader5`.
3. In `.env`: `MT5_USE_MOCK=false`, `MT5_LOGIN`, `MT5_PASSWORD`,
   `MT5_SERVER`, `TRADING_MODE=PAPER` (still simulates fills, but now
   against real market data and a real connected account).
4. Run `PYTHONPATH=. python scripts/run_paper_trading.py` and check
   the logged `account_safety_note` — confirms DEMO vs REAL-MONEY.
5. Only after real testing on a demo account, and only with deliberate
   intent, set `TRADING_MODE=LIVE`. `Settings.validate_live_safety()`
   and `TradingLoop.__init__` both refuse to proceed unless
   `MT5_USE_MOCK=false`, credentials are set, and `KILL_SWITCH=false`
   — there is no path to LIVE trading by accident.

### Feature engine notes (Phase 2)

- `compute_features(df, timeframe)` needs at least 210 bars (for
  EMA200) and raises `InsufficientDataError` otherwise rather than
  silently returning `NaN`-filled results.
- Market structure classification only uses **confirmed** swings — the
  most recent `swing_lookback` bars are excluded so it never looks
  ahead into bars that don't have both left- and right-side
  confirmation yet.
- Tick volume from MT5 is not centralized-exchange volume; volume
  functions are named/documented accordingly (`relative_volume`,
  `volume_expansion`, etc.) so downstream code isn't misled.
- Session windows (Asian/London/New York/overlap) are UTC-hour based
  and currently hard-coded in `app/features/sessions.py` — promote to
  `Settings` later if per-deployment tuning is ever needed.

### Regime detector notes (Phase 3)

- `detect_regime(features, thresholds=None)` in `app/regimes/detector.py`
  takes one timeframe's feature snapshot and returns a `RegimeDecision`
  (`.to_dict()` matches the `{regime, confidence, reasons}` shape from
  the spec exactly).
- Trend classification requires ALL of: EMA alignment, ADX above
  threshold, and +DI/-DI separation confirming direction — a single
  strong signal (e.g. high ADX alone, or EMA alignment alone) is
  deliberately insufficient, matching "the first version should be
  explainable" and avoiding over-eager trend calls.
- Priority order when signals conflict: a **confirmed** trend (all
  mandatory conditions met) wins even under elevated volatility, since
  trending markets are often more volatile than ranges. Otherwise,
  volatility triggers (ATR%, BB width, range expansion) classify
  `HIGH_VOLATILITY` before falling through to `RANGE`/`UNCERTAIN`.
- `UNCERTAIN` is a legitimate, expected output — not an error state —
  matching "NO TRADE is a valid and important signal" (section 11).
- Thresholds live in `app/regimes/thresholds.py` as pydantic Settings
  (env prefix `REGIME_`), so they can be tuned per-deployment without
  code changes, and tests confirm changing them changes the outcome.

### Strategy / selector / scoring notes (Phase 4)

- Every strategy (`app/strategies/trend_pullback.py`, `breakout.py`,
  `mean_reversion.py`) takes one `StrategyContext` (H4/H1/M15 OHLCV +
  features + the H4 `RegimeDecision`) and returns exactly one `Signal`.
  `NO_SIGNAL` is the common, expected case — strategies never raise
  just because no setup was found.
- **Trend Pullback** requires ALL of: H4 confirmed trending, H1
  EMA20/EMA50 confirming direction, M15 price reaching a dynamic
  level (EMA20/50) within an ATR-scaled buffer, a genuine rejection
  candle (not just a touch), and MACD-histogram momentum recovery —
  never buys merely because price touches an EMA.
- **Breakout** requires a documented consolidation (tight Bollinger
  Band width in the recent window) before the move, a volatility
  expansion, a tick-volume confirmation, and rejects moves already too
  extended from EMA20 (avoids chasing). `HIGH_VOLATILITY` regime is
  allowed to trade breakout but is flagged `reduced_risk` in
  `signal.meta` per section 11's "optionally reduced-risk breakout."
- **Mean Reversion** only runs when H4 regime is `RANGE`, and is
  disabled outright if H1's own ADX suggests a strong trend has
  started — even before H4 reclassifies — matching "never blindly buy
  oversold conditions during a strong bearish trend" (section 10).
- **`StrategySelector`** maps H4 regime → allowed strategies exactly
  per section 11 (`UNCERTAIN` → no strategies run, NO TRADE by
  design), runs and scores every actionable signal, and returns the
  highest-scoring one. A high score never means the trade auto-fires —
  the risk engine (Phase 5) still has final say.
- **Scoring** (`app/signals/scoring.py`) is strategy-agnostic: it
  reads context features directly (H4/H1 alignment, M15 momentum,
  volatility band, session) plus two booleans each strategy sets on
  `signal.meta` (`entry_confirmed`, `quality_confirmed`) so the scorer
  never needs strategy-specific logic. Weights and the
  `NO_TRADE`/`WEAK`/`VALID`/`STRONG` breakpoints are pydantic Settings
  (env prefix `SCORE_`).
- **Testing note**: `tests/strategy_test_helpers.py` builds synthetic
  OHLCV fixtures for each strategy's happy path. Getting these to
  reliably clear multiple simultaneous conditions (e.g. Trend
  Pullback's RSI band AND MACD-histogram-improving check at once) took
  real tuning — documented inline in the helpers and test files.
  Along the way this surfaced a genuine Phase 2 bug: `recent_range`/
  `breakout_levels` were computed including the *current* candle in
  their own lookback window, which made breaking that candle's own
  high mathematically impossible — fixed to use only prior bars.

### Risk engine notes (Phase 5)

- `app/risk/position_sizing.py` — `calculate_position_size()` is the
  ONLY place lot size is computed, always from account equity × risk %
  ÷ (stop distance in ticks × tick value), using the broker's actual
  `SymbolSpec`. Rounds DOWN to the nearest `volume_step` (rounding up
  would silently exceed the configured risk) and clamps to
  `[volume_min, volume_max]`, flagging when the risk-based size can't
  even reach the broker's minimum lot.
- `app/risk/guards.py` — `RiskGuardEngine.check()` runs every guard
  from section 16 (kill switch, MT5 connection, broker trade
  permission, market-data freshness, min equity, daily/weekly loss,
  trades-per-day, open positions, consecutive losses, max spread) and
  fails closed: if ANY check fails, `passed=False` with every failing
  reason listed, not just the first.
- `app/risk/kill_switch.py` — file-backed (`KillSwitchState` as JSON),
  so an active kill switch survives a process restart instead of
  silently resetting. `deactivate()` always requires a named actor —
  there's no automatic or anonymous path to clearing it. Corrupted
  state files recover to inactive rather than crashing.
- `app/risk/history.py` — a minimal `TradeHistoryProvider` interface
  (`InMemoryTradeHistory` for now) supplying just what the guards need
  (recent trades for consecutive-loss counting, trades-since for
  daily/weekly windows). This is intentionally NOT the full Trade
  Journal from section 23 — that's Phase 6+, behind the same
  interface, so the guards won't need to change when it lands.
- `app/news/filter.py` — `UnavailableNewsFilter` is the only
  implementation until a real news-data provider exists. It never
  claims a blackout is or isn't active from data it doesn't have —
  `NewsStatus.available=False` is surfaced honestly in every
  `TradeValidation` rather than pretending the filter works.
- `app/risk/validator.py` — `TradeValidator.validate()` ties scoring +
  position sizing + guards + the news filter into the exact
  `{symbol, direction, strategy, regime, score, entry, stop_loss,
  take_profit, risk_reward, risk_percent, spread_ok, news_ok,
  daily_loss_limit_ok, position_limit_ok, market_data_fresh,
  approved}` object from section 20, plus `lots` and
  `rejection_reasons`. `NO_SIGNAL` is always `approved=False` with a
  clear reason, never silently skipped. This is the deterministic gate
  the future AI layer (Phase 10+) will never be allowed to bypass.

### Backtesting engine notes (Phase 6)

- `app/backtesting/resampling.py` — `resample_closed_only()` derives
  H1/H4 candles from an M15 window using ONLY fully-closed
  higher-timeframe periods, dropping any bar whose period isn't yet
  completely covered by the window. Verified by a growing-window test
  that already-closed candles never change value as more bars are
  appended.
- `app/backtesting/engine.py` — `BacktestEngine.run()` walks forward
  bar-by-bar. Decisions are made from a bar's CLOSE and filled at the
  NEXT bar's OPEN — never the same bar, which would be look-ahead.
  Verified by `test_no_lookahead_prefix_invariance`: running the
  engine on a truncated dataset reproduces byte-identical trades (up
  to the truncation point) as running it on the full dataset,
  confirming later bars cannot retroactively influence earlier
  decisions. The engine reuses the exact same feature engine, regime
  detector, strategy selector, scoring, position sizing, and
  `TradeValidator` as live/paper trading — nothing is duplicated or
  reimplemented for backtesting specifically.
- `app/backtesting/costs.py` — spread, slippage, commission, and swap
  are all modeled; a same-seed zero-cost run is verified to never
  underperform the realistic-cost run on identical decisions.
- `app/backtesting/metrics.py` — the full section-25 metric suite
  (win rate, profit factor, expectancy, Sharpe/Sortino/Calmar, max
  drawdown with duration, MAE/MFE, breakdowns by
  year/month/session/regime/strategy/direction). An empty result
  (zero trades) returns a clear `note` rather than dividing by zero.
- **Explicitly deferred, not faked**: trailing stops and partial
  exits (section 15/22/24 mention both) are NOT implemented in this
  baseline — every position is a single fixed SL/TP bracket, closed in
  one shot. Adding partial-lot tracking and trailing-stop state
  properly belongs with the Position Monitor (Phase 9); a half-built
  version here would misrepresent backtest results, so it's left out
  rather than approximated.
- **Reproducibility fix**: `MockMT5Client.get_ohlcv` (Phase 1) anchors
  bar timestamps to wall-clock "now" — fine for live/dev smoke tests,
  but it means the same seed run at a different time of day lands
  different bars in different trading sessions, which can flip
  session-dependent scoring and change trade counts between runs. All
  backtest tests use a new time-anchored synthetic data generator
  (`tests/strategy_test_helpers.synthetic_market_ohlcv`) instead, so
  results are 100% reproducible regardless of when the suite runs.
- **Performance note**: a full walk-forward run recomputes H4/H1/M15
  features from scratch at every evaluated bar, which is deliberately
  the same code path as live trading (no shortcuts that could hide a
  look-ahead bug) but is consequently slow — expect roughly 15-60
  seconds per few-thousand-bar run depending on `step` (how many M15
  bars between evaluations) and `resample_lookback_bars` (how much
  history is resampled each time). `scripts/run_backtest.py` exposes
  both as CLI flags for tuning.

### Strategy Laboratory notes (Phase 7)

- `app/strategy_lab/oos.py` — `train_test_split()` is strictly
  chronological (never shuffled, which would leak future data into
  training). `run_in_sample_out_of_sample()` runs the engine
  independently on each half and keeps the results clearly labeled.
  `walk_forward_folds()` splits into sequential, non-overlapping
  windows and reports per-fold metrics — since this system doesn't
  optimize hundreds of parameters, there's no re-fitting per fold;
  it's purely a consistency check on the same fixed rules.
- `app/strategy_lab/sensitivity.py` — sweeps one numeric parameter
  across a value grid and flags `fragile=True` when net profit's sign
  flips across more than half of adjacent steps — i.e. profitability
  depends on one narrow value rather than a stable region (section 27).
- `app/strategy_lab/monte_carlo.py` — bootstraps the ALREADY-CLOSED
  trades from one backtest run (shuffling trade order, not re-running
  the engine), producing distributions for max drawdown, losing
  streaks, and ending balance. This is standard trade-sequence Monte
  Carlo and stays fast even for large iteration counts. Never
  presented as a guarantee (section 28) — `summary()` returns
  percentiles, not a single verdict.
- `app/strategy_lab/compare.py` — runs the backtest with only one
  strategy enabled at a time (via `StrategySelector`'s `enabled` map)
  so Trend Pullback / Breakout / Mean Reversion metrics are directly
  comparable on identical data and costs.

### Paper trading / execution / position monitor notes (Phase 8-10)

- `app/journal/journal.py` — `TradeJournal` is the real, SQLite-backed
  implementation of section 23, and also implements
  `TradeHistoryProvider` so the Phase 5 risk guards can run against
  real persisted history in live/paper use (the in-memory
  `InMemoryTradeHistory` from Phase 5 remains useful for fast,
  isolated tests). Verified to persist across simulated restarts.
- `app/execution/engine.py` — `ExecutionEngine` is the only code
  allowed to submit real orders. `client_order_id`-based duplicate
  protection is enforced identically in both modes. PAPER mode is
  verified (via a monkeypatch spy in tests) to NEVER call
  `client.submit_order` — it only ever reads ticks from the client and
  simulates fills through the same cost model the backtest engine
  uses. `reconcile()` implements section 35's "MT5 broker state is
  authoritative" principle for LIVE mode.
- `app/positions/monitor.py` — `PositionMonitor` implements what the
  backtest engine deliberately deferred: breakeven-move, ATR-based
  trailing stops, and partial exits, each independently configurable
  and each firing at most once per position (verified by tests) so
  restarts or repeated evaluation calls can't double-apply an action.
- `app/runtime/loop.py` — `TradingLoop` is the single orchestrator for
  Phase 8/9/10: manage any open position, then (if flat) evaluate a
  new signal through the exact same regime/strategy/scoring/validator
  pipeline as backtesting, and execute through `ExecutionEngine`. It
  calls `Settings.validate_live_safety()` in its constructor, so an
  unsafe LIVE configuration fails immediately rather than partway
  through a running loop. `describe_account_safety()` surfaces
  whether the connected account is DEMO or REAL-MONEY (MT5's
  `trade_mode` field) — this is the concrete distinction between
  section 38's Phase 9 ("MT5 Demo") and Phase 10 ("Live").

### News, AI layer, and dashboard notes (final build pass)

- `app/news/calendar.py` — `CalendarNewsFilter` reads a real CSV/JSON
  economic-calendar export and blocks trading in a configurable window
  around HIGH-impact events only; MEDIUM/LOW impact never blocks.
  `build_news_filter()` falls back to `UnavailableNewsFilter` if no
  path is configured, the file is missing, or it's malformed — a bad
  calendar file degrades to "honestly unavailable," never a crash.
  Wired into `TradingLoop` and the dashboard via `NEWS_CALENDAR_PATH`.
- `app/ai/explain.py` — `explain_trade()` builds the exact WHY / WHY
  NOW / WHY THIS STRATEGY / ... / WHAT WOULD INVALIDATE IT structure
  from section 32, entirely from `Signal` + `RegimeDecision` +
  `TradeValidation` — no LLM call, ever, for this to work.
- `app/ai/nl_config.py` — natural-language instructions translate into
  a `ConfigProposal` (rule-based regex matching by default; optional
  LLM-assisted extraction if `ANTHROPIC_API_KEY` is set) that must
  pass pydantic validation via `apply_proposal()` before use, and can
  **only** ever touch a hard-coded field allowlist
  (`ALLOWED_RISK_FIELDS` / `ALLOWED_TOP_LEVEL_FIELDS`). `trading_mode`
  and `kill_switch` are permanently excluded — verified by a test that
  feeds the translator "switch to LIVE and turn off the kill switch"
  and confirms neither field is ever produced, regardless of whether
  the rule-based or (mocked) LLM path handles it.
- `app/ai/summarize.py` / `app/ai/query.py` — deterministic market/
  backtest/performance summaries and structured trade-history queries,
  all built from data the system already computes. `polish()` is an
  optional LLM rephrasing pass that falls back to the original
  deterministic text on any failure — a summary is never unavailable
  just because the optional LLM step failed.
- `app/api/main.py` — the FastAPI dashboard (account, market, signal,
  positions, risk, performance, kill-switch controls), backed by the
  same components the trading loop uses, plus a lightweight HTML/JS
  frontend at `/` with no build step.
- **Honesty note on the LLM path**: `AnthropicLLMClient` calls
  `api.anthropic.com` (allowed by this environment's network egress)
  via the `anthropic` SDK, but no `ANTHROPIC_API_KEY` is configured in
  this sandbox, so that live call path has never actually been
  exercised here — only `NullLLMClient` and the rule-based fallbacks
  are tested. Treat it the same as `RealMT5Client`: structurally
  correct against the documented API, not yet verified against the
  real service from this environment.

### Bugs actually found and fixed while finishing this pass

Documented here rather than glossed over, since "tested" should mean
something concrete:
- `app/api/performance.py`: `profit_factor` was computed as Python's
  `float("inf")` when a strategy had only winning trades — which isn't
  valid JSON and crashed `/api/performance` with a 500 the moment a
  losing trade hadn't happened yet. Fixed to serialize as `null`
  instead (the dashboard already renders that as "∞").
- `app/api/main.py`'s `/api/positions` (and `/api/signal`'s guard
  input): originally read PAPER-mode open positions from the
  dashboard's own `ExecutionEngine` instance, which tracks paper
  positions in-memory and PER-PROCESS. If the trading loop runs as a
  separate process from the dashboard (the normal deployment), the
  dashboard would always show zero open positions no matter what the
  loop had actually done. Fixed to read from `TradeJournal.open_trades()`
  in PAPER mode — the real cross-process source of truth — while LIVE
  mode still reads verified broker state via `ExecutionEngine`.
  Verified by a test that opens a journal trade directly (bypassing
  the dashboard's execution engine entirely) and confirms it shows up.
- `tests/test_ai_summarize_query.py`: a fixture generated trade
  timestamps relative to real wall-clock `datetime.now()`, so
  `trades_today()` could legitimately return fewer trades than
  expected if the suite happened to run within a few hours of
  midnight UTC — the exact same category of flakiness documented for
  `MockMT5Client` in the Phase 6 notes above. Fixed by anchoring the
  fixture to a fixed reference timestamp instead of real time.
- One earlier test-only bug from the same pass (not a product bug, but
  worth being honest about): a fake LLM client in a test used
  positional parameter names that didn't match `polish()`'s keyword
  arguments, silently triggering the fallback path instead of testing
  what it claimed to. Caught by the test suite itself and fixed before
  being called done.

## Philosophy

No strategy is assumed profitable before out-of-sample testing.
NO_TRADE is a first-class decision, not a failure state. Risk
management has priority over signal generation. The AI layer will
never be able to bypass the risk engine, trade validator, kill switch,
or daily loss limit — those are deterministic and final.

**No false safety claims.** This software is never described as
"safe," "guaranteed," or "risk-free" — absolute safety cannot be
guaranteed, and nothing here should be read as claiming it. Where a
component's checks pass, that means exactly one specific thing: the
defined deterministic checks passed at that moment. It does not mean
the trade is safe, or that the system is ready for real money. Trading
financial markets involves substantial risk of loss, this codebase is
an engineering exercise in a sandboxed environment with no verified
live-broker testing (see the MT5 and safety-audit sections above), and
"tests passing" is not evidence of live-trading readiness.
