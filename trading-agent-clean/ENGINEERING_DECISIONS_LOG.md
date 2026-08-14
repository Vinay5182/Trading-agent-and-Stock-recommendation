# ENGINEERING DECISIONS LOG

> **Permanent architectural history for the Trading Agent & Stock Recommendation System.**
> Rules:
> - Never remove or overwrite existing entries.
> - Append new entries only.
> - Before implementing any feature, search this log for related decisions.
> - If a new decision conflicts with an existing one, stop and report the conflict.
> - Every entry that changes architecture MUST be appended here before code is committed.

---

## Format Reference

```
Decision ID : EDL-XXXX
Date        : YYYY-MM-DD
Feature     : [Feature or system area]
Problem     : [What problem was being solved]
Alternatives Considered : [What else was evaluated]
Final Decision : [What was chosen]
Reason      : [Why this was chosen]
Trade-offs  : [What was sacrificed or accepted]
Files Modified : [Exact file paths]
Database Changes : [Collection names and index changes]
API Changes : [Endpoints added, removed, or modified]
UI Changes  : [Frontend component changes]
Risks       : [Known risks at time of decision]
Future Notes : [Guidance for future engineers]
```

---

## EDL-0001

```
Decision ID : EDL-0001
Date        : 2026-07-29
Feature     : Paper Trading Execution Lock
Problem     : System evaluates real market data; a live broker order could be accidentally
              triggered by a misconfigured flag, causing real financial loss.
Alternatives Considered :
  1. Runtime guard checking PAPER_MODE at execution call site only.
  2. Separate codebase / feature branch for live vs paper.
  3. Hardcoded config-level lock with no live broker SDK imported at all.
Final Decision : Option 3. PAPER_MODE=True and LIVE_TRADING_ENABLED=False are
                 hardcoded Pydantic defaults in backend/config.py.
                 No broker SDK (e.g. Zerodha Kite Connect) is imported anywhere
                 in the codebase.
Reason      : Eliminates the possibility of live execution at the import level.
              A missing env var or misconfigured flag cannot enable live trading.
Trade-offs  : Requires a deliberate codebase change (not config change) to enable
              live trading in the future. This is intentional.
Files Modified :
  - backend/config.py (PAPER_MODE, LIVE_TRADING_ENABLED Pydantic fields)
Database Changes : None
API Changes : None
UI Changes  : None
Risks       : Low. Hardcoded safety. No runtime bypass path exists.
Future Notes : When implementing live trading, create a new dedicated module
               (e.g. backend/services/live_broker.py). Do NOT add live order
               logic to existing paper trading service files. Require a separate
               ADR approval before enabling LIVE_TRADING_ENABLED=True.
```

---

## EDL-0002

```
Decision ID : EDL-0002
Date        : 2026-07-29
Feature     : TradingView Chrome CDP WebSocket Concurrency Control
Problem     : TradingView chart data is fetched by connecting to Chrome via CDP
              WebSocket on port 9222. Multiple concurrent REST API requests
              attempting chart navigation simultaneously cause WebSocket frame
              corruption and partial/invalid candle data returned.
Alternatives Considered :
  1. Queue requests via a Redis task queue.
  2. Spawn a separate Chrome process per request.
  3. Single in-process async mutex serializing all CDP operations.
Final Decision : Option 3. A LoopSafeAsyncLock (asyncio.Lock wrapped for thread
                 safety) implemented in backend/services/tradingview_manager.py
                 serializes all CDP commands through a single run_sync() method.
Reason      : Simplest solution with zero external dependencies.
              Redis would add infrastructure overhead not justified for a single-machine system.
              Multiple Chrome processes would multiply memory consumption and
              CDP port conflict risk.
Trade-offs  : Throughput is sequential (one CDP call at a time).
              Batch TV confirmation is slower than parallel but produces correct data.
Files Modified :
  - backend/services/tradingview_manager.py (LoopSafeAsyncLock, run_sync)
  - backend/tv_client.py (CDP WebSocket connection)
  - backend/tv_confirmation.py (confirmation caller)
  - backend/routes/tv.py (route entry point)
Database Changes : None
API Changes : None
UI Changes  : None
Risks       : Medium. Chrome must remain open and unminimized during batch runs.
              Windows throttles background Chrome JS — this is a known OS-level issue.
Future Notes : If throughput becomes critical, consider connection pooling with N=2
               Chrome instances and a semaphore pool. Do NOT bypass run_sync() for
               any new CDP route — add all new TV routes through tradingview_manager.
```

---

## EDL-0003

```
Decision ID : EDL-0003
Date        : 2026-07-29
Feature     : Non-Lossy Setup Rejection Semantics
Problem     : Setups failing multi-timeframe alignment or risk structure checks
              need to be rejected from paper trading without discarding the
              technical diagnostic data that explains why they were rejected.
              This data is needed for ML feature analysis.
Alternatives Considered :
  1. Hard-delete rejected setups from the database.
  2. Move rejected setups to a separate rejected_candidates collection.
  3. Mark tv_status=REJECTED in place while preserving avoid_reason and
     paper_plan_reason fields on the same document.
Final Decision : Option 3. Rejection updates only tv_status to REJECTED.
                 The full diagnostic payload (avoid_reason, paper_plan_reason,
                 MTF structure data, ATR values) remains on the original document
                 in swing_tv_confirmations / momentum_tv_confirmations.
Reason      : Preserves 100% of signal data for ML training.
              Avoids collection sprawl (no separate rejected collection needed).
              Simplifies query logic — one collection, one query, filter by tv_status.
Trade-offs  : tv_confirmations collection grows larger over time with rejected docs.
              Queries must always filter by tv_status to avoid including rejected
              setups in scoring dashboards.
Files Modified :
  - backend/tv_confirmation.py (rejection status sync logic)
  - backend/services/paper_sync.py (downstream tv_status check)
Database Changes :
  - swing_tv_confirmations: tv_status field, avoid_reason field, paper_plan_reason field
  - momentum_tv_confirmations: same fields
API Changes : None
UI Changes  : None
Risks       : Low. Well-tested rejection paths with pytest coverage.
Future Notes : If a new rejection reason is introduced, add it to the avoid_reason
               enum list in tv_confirmation.py. Do NOT create a new collection for
               rejected setups — extend the existing rejection field instead.
```

---

## EDL-0004

```
Decision ID : EDL-0004
Date        : 2026-07-29
Feature     : Operator Intent Header Security Policy
Problem     : REST API endpoints on port 8011 are accessible to any local browser
              tab. Unintentional form submissions, CSRF payloads, or accidental
              triggers from browser extensions could mutate paper trade state.
Alternatives Considered :
  1. JWT authentication for every endpoint.
  2. API key in query parameter.
  3. Custom non-standard HTTP header (X-Trading-Agent-Intent) verified
     server-side for all state-mutating endpoints.
Final Decision : Option 3. All POST/PUT/DELETE endpoints that mutate state
                 (dry_run=false or save=true) require header:
                 X-Trading-Agent-Intent: operator-write-v1
                 Implemented in backend/security/operator_intent.py and enforced
                 via FastAPI dependency injection.
Reason      : Browsers never send custom non-standard headers cross-origin without
              explicit CORS preflight. This eliminates the CSRF risk class entirely.
              No session management overhead. No token rotation complexity.
              Simple deterministic check that fails closed on missing header.
Trade-offs  : Header must be added to all frontend API calls (handled centrally in
              frontend/src/api.js). Any new mutating route must include the
              dependency — this is a manual discipline requirement.
Files Modified :
  - backend/security/operator_intent.py (header verification dependency)
  - backend/main.py (global exception handler for intent errors)
  - frontend/src/api.js (central header injection for all mutation calls)
Database Changes : None
API Changes : All mutating routes gain require_operator_intent FastAPI dependency.
UI Changes  : None (handled transparently in api.js)
Risks       : Low. Fails safely — missing header returns 403, not silent pass.
              Risk: Engineer forgets to add dependency to a new mutating route.
Future Notes : Create a pytest fixture that checks every POST/PUT/DELETE route for
               the operator_intent dependency. Add to pre-commit checks.
```

---

## EDL-0005

```
Decision ID : EDL-0005
Date        : 2026-07-29
Feature     : Centralized Declarative MongoDB Index Registry
Problem     : Indexes were being created ad-hoc inside individual route files and
              service files during startup, leading to inconsistent index definitions,
              missing indexes after collection renames, and no single place to audit
              all constraints.
Alternatives Considered :
  1. Keep indexes inline inside each route or service file.
  2. Use a migration tool (e.g. Alembic equivalent for Mongo).
  3. Central registry module enforced at FastAPI lifespan startup.
Final Decision : Option 3. All MongoDB index definitions are declared in
                 backend/services/mongo_indexes.py as a list of structured
                 IndexSpec objects. The ensure_indexes() function is called once
                 during FastAPI lifespan startup in backend/database.py.
Reason      : Single file to audit all database constraints.
              Idempotent — safe to call on every restart.
              ensure_indexes() is a no-op if indexes already exist in MongoDB.
Trade-offs  : All new collections must have their indexes added to mongo_indexes.py
              manually. There is no automatic discovery of new collections.
Files Modified :
  - backend/services/mongo_indexes.py (IndexSpec list, ensure_indexes function)
  - backend/database.py (lifespan startup calls ensure_indexes)
Database Changes :
  - Compound UNIQUE index on paper_trades: {setup_identity: 1}
  - Compound index on scored_candidates: {symbol: 1, timestamp: -1}
  - Compound index on swing_tv_confirmations: {symbol: 1, scanned_at: -1}
  - Compound index on momentum_tv_confirmations: {symbol: 1, scanned_at: -1}
  - Compound index on ai_feature_snapshots: {symbol: 1, snapshot_at: -1}
API Changes : None
UI Changes  : None
Risks       : Low. Idempotent startup call — no risk of duplicate index creation.
Future Notes : When adding a new collection, add its indexes to mongo_indexes.py
               BEFORE the collection is written to by any service. Do NOT create
               indexes inside route files. Do NOT use raw PyMongo create_index()
               calls outside of mongo_indexes.py.
```

---

## EDL-0006

```
Decision ID : EDL-0006
Date        : 2026-07-29
Feature     : Stateless Scoring Engine Design
Problem     : Scoring logic for Swing and Momentum strategies must be deterministic,
              testable, and side-effect-free. Tight coupling to database or HTTP calls
              inside scoring functions makes unit testing impossible and creates
              risk of partial writes on scoring failures.
Alternatives Considered :
  1. Scoring logic embedded inside route handlers.
  2. Scoring as class methods with database injection.
  3. Pure stateless functions that accept a data dict and return a scored dict.
Final Decision : Option 3. score_swing_row() and score_momentum_row() in
                 backend/scoring.py are pure functions. They accept a raw market
                 data dictionary and return a scored candidate dictionary.
                 No database calls. No HTTP calls. No side effects.
Reason      : Pure functions are trivially unit-testable with no mocking.
              Deterministic — same input always produces same output.
              Route handlers own the DB write; scoring owns only the math.
Trade-offs  : Scores cannot be dynamically adjusted based on live market state
              (e.g. VIX-adjusted scoring thresholds) without passing that state
              explicitly as function arguments.
Files Modified :
  - backend/scoring.py (score_swing_row, score_momentum_row, score_market_data_row)
  - backend/routes/score.py (calls scoring functions, owns DB write)
Database Changes : None (DB writes live in routes/score.py, not in scoring.py)
API Changes : None
UI Changes  : None
Risks       : Low. Proven stable across 467 test cases.
Future Notes : New scoring indicators MUST be implemented as pure helper functions
               inside backend/scoring.py. Do NOT add database queries or HTTP calls
               to scoring.py. If dynamic thresholds are needed, pass them as
               function parameters, not as global state.
```

---

## EDL-0007

```
Decision ID : EDL-0007
Date        : 2026-07-29
Feature     : Setup Identity Deduplication Key Design
Problem     : Paper trading and TV confirmation pipelines process the same NSE universe
              (~500 symbols) repeatedly on each scan. Without a stable identity key,
              the same setup would be inserted multiple times on consecutive scan runs.
Alternatives Considered :
  1. Dedup by (symbol + timestamp) — too coarse, same symbol scanned multiple times per day.
  2. Dedup by MD5 hash of full document — unstable, any field change creates a new hash.
  3. Dedup by deterministic business key: (symbol + strategy + timeframe + signal_date).
Final Decision : Option 3. setup_identity is a deterministic composite string:
                 f"{symbol}_{strategy}_{timeframe}_{signal_date}" stored on every
                 confirmation and paper trade document. MongoDB unique index on
                 this field prevents duplicates at the database level.
Reason      : Survives partial pipeline re-runs — only new setups create new documents.
              Human-readable — engineers can directly read the identity string.
              Database-enforced — no application-level dedup logic needed.
Trade-offs  : setup_identity must be generated identically in paper_sync.py and
              in the TV confirmation writer. Any divergence in format causes missed
              dedup or phantom duplicates.
Files Modified :
  - backend/services/paper_sync.py (setup_identity generation and upsert logic)
  - backend/tv_confirmation.py (setup_identity injection into confirmation doc)
  - backend/services/mongo_indexes.py (unique index on setup_identity)
Database Changes :
  - paper_trades: UNIQUE index on {setup_identity: 1}
  - swing_tv_confirmations: index on {setup_identity: 1}
API Changes : None
UI Changes  : None
Risks       : Medium. If setup_identity format ever changes, existing documents
              cannot be matched against new format — must run a migration.
Future Notes : setup_identity format must be documented in PROJECT_CONVENTIONS.md.
               Any change to the format requires a migration script and a new
               EDL entry. Never compute setup_identity inline in a route — always
               use the shared builder function in paper_sync.py.
```

---

## EDL-0008

```
Decision ID : EDL-0008
Date        : 2026-07-29
Feature     : AI Feature Snapshot Temporal Leakage Guard
Problem     : ML features extracted for a setup at time T must not include data
              that was not available at time T (future leakage). Including post-signal
              data (e.g. next-day OHLC) in training features makes models overfit
              to future information and fail on live data.
Alternatives Considered :
  1. Filter features by current wall-clock time only.
  2. Always use the latest available data regardless of signal date.
  3. feature_as_of parameter: all feature queries filter strictly to
     data with timestamp <= signal_date.
Final Decision : Option 3. backend/ai/features.py accepts a feature_as_of
                 datetime parameter and all database queries for historical
                 indicators filter by {timestamp: {$lte: feature_as_of}}.
Reason      : Correctly simulates what data was available to a trader on signal_date.
              Prevents data leakage contaminating ML training sets.
              Enables retrospective feature extraction for historical setups.
Trade-offs  : feature_as_of must be passed correctly by callers. If caller passes
              wrong date, features silently include incorrect data range.
Files Modified :
  - backend/ai/features.py (feature_as_of parameter, all query filters)
  - backend/services/daily_dataset.py (passes signal_date as feature_as_of)
Database Changes : None (query filter only, no schema change)
API Changes :
  - POST /api/ai/features/save — accepts snapshot_date param
UI Changes  : None
Risks       : Medium. Incorrect feature_as_of passed by a caller causes silent
              data leakage — no runtime error, only ML model quality degradation.
Future Notes : Add a pytest test that verifies no feature query can return data
               with timestamp > feature_as_of for any snapshot. Treat any violation
               as a critical regression. Do NOT add features that query non-time-series
               data (e.g. static company metadata) without explicitly documenting
               the leakage exemption in this log.
```

---

## EDL-0009

```
Decision ID : EDL-0009
Date        : 2026-07-29
Feature     : NSE Data Provider Fallback Architecture
Problem     : NSE India web servers rate-limit aggressive scrapers (HTTP 429).
              If the primary NSE scraper fails, the entire market scan pipeline
              would fail for affected symbols with no fallback.
Alternatives Considered :
  1. Retry the NSE scraper with exponential backoff only.
  2. Use yfinance exclusively (slower, less accurate for NSE).
  3. Primary NSE scraper with automatic yfinance fallback on HTTP 429
     or connection error.
Final Decision : Option 3. backend/data_provider.py catches HTTP 429 and
                 connection errors from nse_client.py and transparently falls
                 back to yfinance for the affected symbols.
Reason      : Maximizes scan coverage — NSE scraper preferred for accuracy,
              yfinance as safety net.
              Zero pipeline failure on rate limiting.
              Fallback is transparent to the caller (route handler).
Trade-offs  : yfinance data may have minor timestamp or OHLC discrepancies
              compared to NSE direct data. Fallback source is logged but not
              surfaced in the UI.
Files Modified :
  - backend/data_provider.py (fallback logic)
  - backend/nse_client.py (primary scraper, raises on 429)
Database Changes : None
API Changes : None
UI Changes  : None
Risks       : Low. Both sources produce structurally identical quote dicts.
Future Notes : If a third data source is added (e.g. Databento, IIFL), add it
               as an additional fallback tier in data_provider.py — do NOT
               create a separate route or service for it.
```

---

## EDL-0010

```
Decision ID : EDL-0010
Date        : 2026-07-29
Feature     : Paper Trade State Machine Design
Problem     : Paper positions have a defined lifecycle: they are created when a signal
              fires, activated when entry price is hit, and closed when target or
              stop-loss is hit. Without a formal state machine, position updates
              become scattered across multiple services with no clear ownership.
Alternatives Considered :
  1. Ad-hoc status string updates directly in route handlers.
  2. Event-driven state machine with a message queue.
  3. Explicit finite state machine in paper_sync.py with defined legal
     state transitions enforced by the update logic.
Final Decision : Option 3. Formal FSM in backend/services/paper_sync.py.
                 Legal states: WAITING_FOR_ENTRY → ACTIVE → T1_HIT → SL_HIT → COMPLETED
                 Illegal transitions are rejected with an error log.
                 State transitions are the only path to updating paper trade status.
Reason      : Eliminates scattered status mutations across route files.
              Transition guards prevent impossible states (e.g. COMPLETED → ACTIVE).
              Single ownership of state logic in one file.
Trade-offs  : All paper position update logic must be routed through paper_sync.py.
              Direct MongoDB updates to paper_trades.status from a route are forbidden.
Files Modified :
  - backend/services/paper_sync.py (FSM, state transition functions)
  - backend/services/paper_automation.py (calls paper_sync transition functions)
  - backend/services/trade_journal.py (writes journal entry on terminal state)
Database Changes :
  - paper_trades: status field with values {WAITING_FOR_ENTRY, ACTIVE, T1_HIT,
    SL_HIT, COMPLETED}
API Changes : None (state transitions are internal, not directly exposed)
UI Changes  : Frontend reads status field for badge coloring (App.jsx)
Risks       : Low. State machine is well-tested.
Future Notes : If a new terminal state is needed (e.g. EXPIRED, MANUAL_CLOSE),
               add it to the FSM in paper_sync.py and create a new EDL entry.
               Do NOT add status update logic in route files. Do NOT bypass
               paper_sync.py for any state transition.
```

---

## EDL-0011

```
Decision ID : EDL-0011
Date        : 2026-07-29
Feature     : Centralized Frontend API Client with Intent Header Injection
Problem     : Frontend has ~30+ API calls across multiple components. Injecting the
              X-Trading-Agent-Intent header and handling 2MB body limits,
              120s timeouts, and error parsing in each call site would create
              massive duplication and inconsistent error handling.
Alternatives Considered :
  1. Axios instance with interceptors.
  2. Custom fetch wrapper in a single api.js module reused by all components.
  3. React Query with custom fetcher.
Final Decision : Option 2. frontend/src/api.js exports a single request() wrapper
                 that injects the intent header, enforces timeout, handles error
                 parsing, and is imported by all route/component files.
                 Named API methods (loadMarketData, runScore, etc.) are thin wrappers
                 around request().
Reason      : Zero external dependency (no Axios, no React Query).
              Single place to change auth, timeout, or base URL.
              Named methods provide IDE autocomplete and a discoverable API surface.
Trade-offs  : All frontend API changes must go through api.js.
              Direct fetch() calls inside components are forbidden.
Files Modified :
  - frontend/src/api.js (request wrapper, all named API methods)
  - frontend/src/App.jsx (imports api.js methods only)
Database Changes : None
API Changes : None (api.js mirrors the backend route surface)
UI Changes  : None (transparent to UI)
Risks       : Low. Single point of failure — a bug in request() affects all API calls.
              Mitigated by the simplicity of the wrapper (< 50 lines of logic).
Future Notes : Every new backend endpoint MUST have a corresponding named method
               added to api.js. Do NOT call fetch() directly inside any React
               component or page. Timeout (120s) is calibrated for batch TV
               confirmation — do not reduce without measuring batch completion times.
```

---

## EDL-0012

```
Decision ID : EDL-0012
Date        : 2026-07-29
Feature     : Engineering Investigation Mode & Decision Log Protocol
Problem     : Without a mandatory investigation gate, engineers and AI assistants
              write code immediately without tracing execution flow, risking
              duplicate logic, broken architecture, and undocumented decisions.
Alternatives Considered :
  1. Code review only (post-hoc review of changes).
  2. Verbal agreement to trace code before writing.
  3. Mandatory structured 8-step investigation protocol before every code change,
     with permanent append-only decision log (this file).
Final Decision : Option 3. The ENGINEERING_DECISIONS_LOG.md is the permanent
                 architectural history. Every architectural decision is appended
                 here. The 8-step Engineering Investigation Mode is the mandatory
                 workflow before any code change.
                 Rules enforced:
                 - Never remove entries.
                 - Append only.
                 - Conflict with existing entry → stop and report.
                 - No code written before investigation is complete.
Reason      : Creates institutional memory that survives model context resets,
              new AI assistant sessions, and engineer turnover.
              Prevents regression of architectural decisions.
Trade-offs  : Small overhead per change (investigation + log entry).
              This is intentional and acceptable.
Files Modified :
  - ENGINEERING_DECISIONS_LOG.md (this file — created 2026-07-29)
  - AI_PROJECT_INDEX.json (created 2026-07-29)
  - ARCHITECTURE_DECISIONS.md (created 2026-07-29)
  - BUG_DATABASE.md (created 2026-07-29)
  - FEATURE_HISTORY.md (created 2026-07-29)
Database Changes : None
API Changes : None
UI Changes  : None
Risks       : Low. Purely documentation — no runtime impact.
Future Notes : Before starting any task, search this file for the feature name.
               If a matching entry exists, follow its Future Notes. If the
               new implementation conflicts with an existing entry, do not
               proceed — explain the conflict to the user and wait for resolution.
```

---

*— End of bootstrapped entries. All future entries append below this line. —*

---

## EDL-0013

```
Decision ID : EDL-0013
Date        : 2026-07-29
Feature     : Daily TradingView Confirmed Counts Persistence (Phase 1)
Problem     : Data Collection UI required displaying daily confirmed counts for Swing
              and Momentum strategies per trade date without redundant database scans,
              extra analytics, charts, or heavy candidate accordions.
Alternatives Considered :
  1. Recalculate daily counts by querying MongoDB confirmation collections after every run.
  2. Compute counts on the fly during frontend render.
  3. Reuse existing tv_saved_results service to immediately upsert in-memory confirmed_count
     to daily_tradingview_counts (one document per trade_date) and serve via calendar API.
Final Decision : Option 3. Implemented persist_daily_tv_counts in backend/services/tv_saved_results.py.
                 At the end of Swing/Momentum TV runs (save=True), in-memory confirmed_count
                 is upserted directly into db.daily_tradingview_counts.
                 GET /api/ai/candidate-outcomes/calendar reads daily_tradingview_counts.
                 Frontend AiDatasetDayPanel component in App.jsx renders exclusively:
                 "TradingView Results" -> Swing <value>, Momentum <value>.
Reason      : Zero database re-scanning after TV runs.
              Atomic MongoDB upsert ensures exactly 1 document per trade_date.
              Extends existing tv_saved_results.py service without creating new service files.
              Clean UI meeting Phase 1 strict specification.
Trade-offs  : Phase 1 deliberately omits candidate tables, win rates, and pending counts.
              This is strictly per user specification.
Files Modified :
  - backend/services/tv_saved_results.py (added persist_daily_tv_counts)
  - backend/services/mongo_indexes.py (added unique index spec on daily_tradingview_counts.trade_date)
  - backend/routes/swing.py (called persist_daily_tv_counts when save=True)
  - backend/routes/momentum.py (called persist_daily_tv_counts when save=True)
  - backend/routes/ai.py (updated GET /api/ai/candidate-outcomes/calendar to read daily_tradingview_counts)
  - frontend/src/App.jsx (simplified AiDatasetDayPanel to display strictly Swing & Momentum counts)
  - ENGINEERING_DECISIONS_LOG.md (this entry)
Database Changes :
  - Collection: daily_tradingview_counts
  - Compound UNIQUE Index: {trade_date: 1}
  - Document Schema: {trade_date: str, swing_confirmed: int, momentum_confirmed: int, updated_at: str}
API Changes :
  - GET /api/ai/candidate-outcomes/calendar — returns daily_map with swing_confirmed & momentum_confirmed
UI Changes  :
  - AiDatasetDayPanel in App.jsx now displays exclusively "TradingView Results" with Swing and Momentum counts.
Risks       : Low. Fails safely and maintains backward-compatibility.
Future Notes : Phase 2 additions must preserve the daily_tradingview_counts trade_date unique index constraint.
```

---

## EDL-0014

```
Decision ID : EDL-0014
Date        : 2026-07-29
Feature     : Phase 2 – Daily TradingView Validation Summary
Problem     : Data Collection UI required displaying complete daily TradingView validation
              summary for Swing and Momentum strategies (Selected, Confirmed, Rejected)
              per trade date without extra charts, tables, or aggregation queries.
Alternatives Considered :
  1. Recalculate selected and rejected counts via post-run database scans.
  2. Create a separate collection for Phase 2 detailed counts.
  3. Extend existing persist_daily_tv_counts in tv_saved_results.py to update
     swing_selected, swing_confirmed, swing_rejected and momentum_selected,
     momentum_confirmed, momentum_rejected fields atomically in daily_tradingview_counts.
Final Decision : Option 3. Extended persist_daily_tv_counts in backend/services/tv_saved_results.py.
                 At pipeline completion when save=True, in-memory pipeline counts
                 (len(rows), confirmed_count, rejected_count) are passed and upserted.
                 GET /api/ai/candidate-outcomes/calendar exposes all 6 fields.
                 Frontend AiDatasetDayPanel in App.jsx renders:
                 TradingView Results
                 SWING: Selected <val>, Confirmed <val>, Rejected <val>
                 MOMENTUM: Selected <val>, Confirmed <val>, Rejected <val>.
Reason      : Reuses Phase 1 architecture and existing database collection/service.
              Zero post-run database scanning.
              Single atomic MongoDB document per trade_date.
              Strictly matches Phase 2 UI requirements.
Trade-offs  : Does not display technical failure breakdown or candidate tables, per Phase 2 rules.
Files Modified :
  - backend/services/tv_saved_results.py (extended persist_daily_tv_counts parameters and Mongo fields)
  - backend/routes/swing.py (passed selected_count, confirmed_count, rejected_count)
  - backend/routes/momentum.py (passed selected_count, confirmed_count, rejected_count)
  - backend/routes/ai.py (returned all 6 summary fields in calendar response)
  - frontend/src/App.jsx (updated AiDatasetDayPanel layout for SWING and MOMENTUM Selected/Confirmed/Rejected)
  - ENGINEERING_DECISIONS_LOG.md (this entry)
Database Changes :
  - Collection: daily_tradingview_counts (extended schema)
  - Document Schema:
    {
      trade_date: str,
      swing_selected: int, swing_confirmed: int, swing_rejected: int,
      momentum_selected: int, momentum_confirmed: int, momentum_rejected: int,
      updated_at: str
    }
API Changes :
  - GET /api/ai/candidate-outcomes/calendar — returns swing_selected, swing_confirmed, swing_rejected, momentum_selected, momentum_confirmed, momentum_rejected per day.
UI Changes  :
  - AiDatasetDayPanel displays SWING (Selected, Confirmed, Rejected) and MOMENTUM (Selected, Confirmed, Rejected).
Risks       : Low. Backwards-compatible; missing fields default to 0.
Future Notes : Keep single document per trade_date structure intact.
```


