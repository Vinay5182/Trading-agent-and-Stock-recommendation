# Coding Rules and Engineering Handbook

This document serves as the authoritative guide for contributing to the Trading Agent codebase. Any AI developer or human contributor must strictly adhere to these rules to maintain stability, performance, and readability.

---

## 1. Naming Conventions

### Python (Backend)
- **Functions and Variables**: `snake_case` (e.g., `score_market_data_row`, `current_price`).
- **Classes**: `PascalCase` (e.g., `TradingViewClient`, `TradingViewExecutionManager`).
- **Constants and Environment Variables**: `UPPER_SNAKE_CASE` (e.g., `LIVE_TRADING_ENABLED`, `SYNC_INTERVAL_SECONDS`).
- **Private Methods/Variables**: Prefix with a single underscore `_` (e.g., `_run_scan_real`, `_active_operation`). Do not use double underscores unless strictly necessary for name mangling.

### JavaScript/React (Frontend)
- **Variables and Functions**: `camelCase` (e.g., `getMarketDataSymbol`, `hasValue`).
- **React Components**: `PascalCase` (e.g., `StatCard`, `PageErrorBoundary`).
- **CSS Classes**: `camelCase` or `kebab-case` depending on existing stylesheet patterns, but remain consistent within the file.

---

## 2. Folder and File Conventions

- **`backend/routes/`**: Contains only FastAPI router definitions. **Rule:** Do not put heavy business logic or database queries here. Routes should parse HTTP parameters, call a service function, and return the result.
- **`backend/services/`**: Contains core business logic, background tasks, and managers (e.g., `paper_automation.py`).
- **`frontend/src/`**: React source. `App.jsx` handles global state and layout. Network requests must be wrapped in `api.js`.

---

## 3. Async Patterns & Concurrency Rules

- **Non-Blocking I/O**: The backend uses FastAPI running on Uvicorn. **Never use `time.sleep()`**. Use `await asyncio.sleep()`.
- **Sync wrapping**: If you must run a synchronous blocking operation (like `requests.get` to a slow API), wrap it in `await asyncio.to_thread(func)`.
- **Database**: Use the asynchronous `motor` driver exclusively. Do not use blocking PyMongo calls.
- **Task Scheduling**: Use `asyncio.create_task()` for background schedulers, but ensure they are attached to global references so they aren't garbage collected unexpectedly.
- **Global Locks**: When communicating with external stateful systems (like the TradingView Chrome tab), use the custom `LoopSafeAsyncLock` (in `tradingview_manager.py`) to prevent WebSocket message interlacing.

---

## 4. Error Handling Style

- **Fail Fast**: If a dependency (like Chrome CDP) is completely unreachable, raise an exception immediately rather than hanging.
- **Graceful API Degradation**: FastAPI routes should catch specific exceptions (like `PipelineRunBusy`) and return a structured 4xx or 2xx JSON with an `error` key, allowing the React frontend to display a toast notification instead of a crash.
- **No Silent Swallowing**: Do not use `except Exception: pass` without logging the error, unless it is a highly predictable and safe failure (e.g., trying to parse a broken date string).

---

## 5. Logging Style

- Use Python's standard `logging` module.
- Retrieve the logger via `logging.getLogger("uvicorn.error")` or `logging.getLogger(__name__)`.
- Log critical infrastructure failures as `logger.error` or `logger.exception` (which includes tracebacks).
- Log high-level checkpoints (e.g., "Market scan completed: 750 rows") as `logger.info`.

---

## 6. Database Access Rules (MongoDB)

- **Connection Management**: Do not create new MongoDB clients per request. Use `get_database()` from `database.py` which reuses the application-level Motor client.
- **Bulk Writes**: For updating multiple stocks (e.g., saving scoring results), always use `bulk_write` with `UpdateOne(..., upsert=True)`. Never loop and `await db.collection.update_one` 750 times, as this exhausts connection pools and is extremely slow.
- **Index Management**: Indexes should be verified/created on application startup (or lazy-loaded safely) via `services.mongo_indexes`.

---

## 7. API Response Format

Always return structured JSON. Prefer returning dictionaries with explicit keys even on success:
```json
{
  "status": "success",
  "processed_count": 50,
  "data": [...]
}
```
If returning a direct list for a table, wrap it: `{"rows": [...], "count": 50}`.

---

## 8. Configuration Handling

- **Source of Truth**: `backend/config.py` using Pydantic `BaseSettings`.
- **Usage**: Never use `os.environ.get()` directly in business logic. Import `settings` from `config` and access `settings.PAPER_MODE`.

---

## 9. TradingView CDP Safety Rules

TradingView integration via Chrome DevTools Protocol is the most fragile part of the system.
- **Never interact with the WebSocket directly from a route**. Always use `TradingViewExecutionManager` to acquire the lock.
- **Assume Disconnection**: Chrome can close the websocket, sleep the tab, or crash. Always wrap CDP calls in `try/except` and handle `TradingViewTabDisconnectedError` gracefully.
- **Spin-Locks**: When waiting for a chart to load, use a timeout-bound async loop checking DOM elements via `Runtime.evaluate`.

---

## 10. Performance Guidelines

- **Caching**: Cache static API responses or use module-level variables for data that changes daily (like index constituent lists).
- **Frontend Renders**: In React, use `useMemo` for sorting/filtering large arrays (like the 750 stock dashboard) to prevent freezing the UI on re-renders.

---

## 11. Security Considerations

- **Operator Intent**: Destructive or heavy operations (like triggering a market scan) require the `X-Operator-Intent` header.
- **No Live Trading Hooks**: Ensure `LIVE_TRADING_ENABLED` remains False during development. Code paths that place real orders must be blocked by this flag.

---

## 12. Danger Zone: Files Not to Modify Without Extreme Caution

1. `backend/services/tradingview_manager.py`: Modifying the lock/generation logic will cause WebSocket interlacing and crash the browser integration.
2. `backend/tv_client.py`: The JSON-RPC message ID logic and WebSocket reader loops are highly tuned.
3. `backend/scoring.py`: Changing mathematical proximity checks or score weighting will invalidate the AI historical dataset and break the strategy.

---

## 13. Checklists

### Before Creating a New Feature
- [ ] Does this require a new database collection? If so, define indexes in `mongo_indexes.py`.
- [ ] Does this involve third-party HTTP calls? If so, implement timeouts and async `aiohttp` or `to_thread`.
- [ ] Are you adding an environment variable? Add it to `.env.example` and `config.py`.

### Before Modifying an Existing Feature
- [ ] If changing a database schema, how does it affect historical paper trades?
- [ ] If modifying the React dashboard, does it break on mobile/smaller screens?
- [ ] Are you preserving the `PAPER_MODE` safety gates?
