# Bug Database & Troubleshooting Registry

## BUG-001: TradingView CDP Timeout on Minimized Chrome
- **Status**: Mitigated / Documented
- **Symptoms**: `TradingViewTimeoutError` thrown when invoking batch confirmation routes.
- **Root Cause**: Windows and Chromium pause/throttle JavaScript execution and WebSocket frames on minimized or backgrounded browser windows.
- **Fix / Prevention**: Keep Google Chrome open and visible on screen (un-minimized) during batch confirmation processing.

## BUG-002: NSE Scraper HTTP 429 Rate Limiting
- **Status**: Mitigated / Automated Fallback
- **Symptoms**: Scraper fails with HTTP 429 Too Many Requests or empty response.
- **Root Cause**: NSE India web servers block aggressive scrapers requesting quotes at high rates.
- **Fix / Prevention**: `data_provider.py` automatically catches HTTP 429 / connection errors and seamlessly falls back to `yfinance` to fill quote gaps.

## BUG-003: Duplicate Paper Trades on Concurrent Sync
- **Status**: Resolved
- **Symptoms**: Duplicate paper positions created for the same stock setup identity.
- **Root Cause**: Read-check-write race conditions when multiple paper update tasks execute concurrently.
- **Fix / Prevention**: `backend/services/mongo_indexes.py` enforces a compound `UNIQUE` database index on `(setup_identity: 1)` in `paper_trades`.

## BUG-004: Frontend Table Render Lag
- **Status**: Resolved
- **Symptoms**: UI lag when loading candidate tables.
- **Root Cause**: Rendering 750 unfiltered rows simultaneously in monolithic `App.jsx`.
- **Fix / Prevention**: Added server-side pagination (`limit=100`) and client-side tab section filtering.
