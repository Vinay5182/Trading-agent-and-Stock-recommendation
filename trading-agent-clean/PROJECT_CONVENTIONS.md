# Project Conventions

This document outlines the standard conventions, terminologies, and design patterns utilized across the Trading Agent project.

---

## 1. Naming Conventions

### API Naming
- RESTful HTTP methods apply.
- Use `kebab-case` for endpoints (e.g., `/api/market/load-all`, `/api/swing/tv-confirm`).
- Resource grouping:
  - `/api/scan`: Market scanning and ingestion.
  - `/api/score`: Strategy math and computation.
  - `/api/paper`: Virtual portfolio execution.

### Database, Collection, and Environment Variable Naming
- **Database Name**: Default is `trading_agent_clean`.
- **Collections**: `snake_case`, pluralized (e.g., `market_data`, `scored_candidates`, `paper_trades`, `system_errors`).
- **Environment Variables**: `UPPER_SNAKE_CASE` in `.env` and `config.py` (e.g., `PAPER_MODE`).

---

## 2. Trading Terminology & Lifecycles

### Trade Lifecycle Status Enums
The `status` field of a document in `paper_trades` follows a strict state machine:
1. `WAITING_FOR_ENTRY`: The plan is generated, but current price has not breached the entry trigger.
2. `ACTIVE`: The entry price was breached. The position is open.
3. `TARGET_1_HIT`: Price reached 1R profit. Partial position closed.
4. `TARGET_2_HIT`: Price reached 2R profit. Partial position closed.
5. `TARGET_3_HIT`: Price reached 3R profit. Remainder of position closed (Trade Complete).
6. `STOPPED_OUT`: Price breached the Stop Loss level (Trade Complete).
7. `MANUALLY_CLOSED`: Closed by user intervention (Trade Complete).

### Grade Definitions
Stocks are graded based on their mathematical score (0-100). Grades dictate the percentage of the portfolio risked.
- **A+ (A_PLUS)**: Perfect setup. Highest confidence. Max risk allowed (e.g., 0.50%).
- **A**: Great setup. High confidence. Standard risk (e.g., 0.40%).
- **B**: Good setup but has flaws (e.g., volume is slightly low). Reduced risk (e.g., 0.25%).
- **C**: Marginal setup. Lowest risk.
- **NO_TRADE**: Score too low or invalid data.

### Risk Terminology (R-Multiples)
- **1R (Risk)**: The total capital amount risked if the trade hits the Stop Loss.
  - Example: Buy at ₹100, Stop Loss at ₹90. 1R = ₹10 per share.
- **Targets**: Multiples of 1R. 
  - `T1` = Entry + 1R (₹110).
  - `T2` = Entry + 2R (₹120).

---

## 3. Architecture & Organization

### Folder Organization Rationale
- `backend/routes/`: Separation of concerns. Keeps FastAPI HTTP dependency decoupled from math.
- `backend/services/`: Reusable python functions that can be called by Routes, Schedulers, or Tests.
- `frontend/src/`: React root. All state is lifted to `App.jsx` for easy prop-drilling into dashboard tabs since the app is a monolithic single-page dashboard.

### Scheduler Conventions
- Background tasks (like `paper_automation.py`) run as infinite `asyncio` loops.
- **Event Flow**:
  1. Sleep for `SYNC_INTERVAL_SECONDS`.
  2. Acquire local Lock.
  3. Read DB for active trades.
  4. Fetch current prices.
  5. Update DB statuses.
  6. Repeat.
- Schedulers write their heartbeat/health status to the `scheduler_status` MongoDB collection, allowing the frontend to show "System Health: HEALTHY" or "DELAYED".

---

## 4. Reusable Design Patterns

1. **Pipeline Run Locks**: 
   - Found in `services/pipeline_run_lock.py`.
   - Used to prevent two heavy operations (like a market scan) from running concurrently.
   - Throws `PipelineRunBusy` which the frontend catches to say "Operation already running".
2. **Fallback Providers**:
   - The NSE API is notoriously unreliable. `data_provider.py` implements a Chain of Responsibility. Try NSE -> If fail/missing data -> Query `yfinance`.
3. **CDP Injection**:
   - Interacting with TradingView relies on injecting JavaScript directly into the page context using Chrome DevTools Protocol `Runtime.evaluate` instead of parsing complex HTML DOM trees.

---

## 5. Anti-Patterns to Avoid

- **DO NOT Use `time.sleep()`**: Will freeze the entire FastAPI web server and all background tasks. Use `await asyncio.sleep()`.
- **DO NOT Place Logic in Routes**: FastAPI endpoints should only validate parameters and format responses.
- **DO NOT Poll the Database in Tight Loops**: MongoDB will throttle or crash. Use reasonable intervals (e.g., 15-60 seconds) for automation loops.
- **DO NOT Catch Generic Exceptions Silently**: `except Exception: pass` hides critical bugs. Always use `logger.exception`.
- **DO NOT Trust Frontend Math**: The frontend may calculate estimated profits for display, but the backend `paper_automation.py` must re-calculate and enforce all T1/T2/SL logic authoritatively.
