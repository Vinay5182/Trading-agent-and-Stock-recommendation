# Project Checkpoint

## Current State

Phase 24E completed.

## Pre-Debug Status - 2026-06-05

- Project is paper mode only.
- Market data, score, Swing, Momentum, TV Confirm, Stock Detail, and Paper Plan features exist.
- Need full project debug before further changes.
- Do not enable live trading.
- Do not add broker APIs.
- Do not place orders.

## Latest Debug Status - 2026-06-05

- Backend compile passed.
- Frontend build passed.
- Endpoints working.
- TradingView CDP port 9222 working.
- Market scan, score run, Swing candidates, Momentum candidates working.
- Swing TV Confirm working.
- Momentum TV Confirm working.
- Stock Detail click/open logic working.
- Paper Trade Plan working.
- Confirmed plans show Entry, SL, T1, T2, T3.
- WAIT setups show projected Entry, SL, T1, T2, T3.
- Rejected/Technical Failed rows do not create fake paper plans.
- Old saved rows may need TV Confirm rerun to include new paper fields.
- No live trading.
- No broker API.
- No order placement.
- Paper mode only.

## Latest UI Status - 2026-06-05

- Stock Detail Paper Trade Plan now shows CAUTION PLAN when Entry/SL/Targets are valid but fake breakout/trap risk is high.
- Numeric paper levels are unchanged.
- Valid plan is only for clean risk conditions.
- Invalid plan remains for missing/failed plan.
- Frontend build passed.
- Backend unchanged.
- No strategy/scoring/live trading changes.
- Paper mode only.

## Status - 2026-06-01

- Market data loading works.
- Scoring works.
- Swing candidates load from fresh `scored_candidates` after Score Market Data.
- TradingView candle/timeframe merging issue fixed.
- Hard TradingView candle loading added:
  - resolution wait
  - stabilization wait
  - two-fetch candle stability check
  - retry validation
- Verified:
  - 1W resolution W, stable true
  - 1D resolution D, stable true
  - 4H resolution 240, stable true
  - 1H resolution 60, stable true
- Warnings empty.
- `same_last_volume_pairs` empty.
- `same_last_ohlcv_pairs` empty.
- `possible_stale_pairs` empty.
- No scan/scoring/strategy/live trading changes.

## Backend

- Runs on http://127.0.0.1:8011
- MongoDB is local MongoDB service
- Frontend API base is http://127.0.0.1:8011

## Market Data

- Scan All 750 Stocks works.
- Endpoint: `POST /api/market/load-all?index_name=BROAD_MARKET_750&dry_run=false`
- `market_data` count = 750
- `complete_count` = 750
- `fetch_failed` = 0
- NSE is primary.
- yfinance is missing-field fallback only.
- Invalid/dummy symbols removed and blocked.
- Normal scan optimized around 15 seconds when history cache is reused.
- `refresh_history=true` performs full yfinance history refresh.

## NSE Index Aliases

- `NIFTY SMLCAP 250` works.
- `NIFTY MICROCAP250` works.
- `NIFTY SMALLCAP 250` returned 0.
- `NIFTY MICROCAP 250` returned 0.

## Scoring

- `backend/scoring.py` has pure scoring functions.
- `/api/score/run` works.
- `/api/score/summary` works.
- `scored_candidates` count = 750
- Swing candidates count = 20
- Momentum candidates count = 47
- Swing uses `score` / `nse_score > 80`.
- Momentum uses `momentum_score >= 70`.

## Swing

- `/api/swing/summary` works.
- `/api/swing/candidates` works.
- Swing page displays separate summary and table.

## Momentum

- `/api/momentum/summary` works.
- `/api/momentum/candidates` works.
- Momentum page displays separate summary and table.

## Frontend

- `npm build` passed.
- Score Market Data button works.
- Swing candidates displayed.
- Momentum candidates displayed.
- Swing and Momentum are separate pages/sections.

## Safety

- Paper mode only.
- No live trading.
- No broker APIs.
- No buy/sell/order buttons.

## Run Commands

One-click startup:

```powershell
cd "C:\Users\Asus\OneDrive\Documents\Trading_Strategy\trading-agent-clean"
.\start-trading-agent.ps1
```

Stop backend/frontend:

```powershell
cd "C:\Users\Asus\OneDrive\Documents\Trading_Strategy\trading-agent-clean"
.\stop-trading-agent.ps1
```

## Verification Commands

```powershell
curl.exe -X POST "http://127.0.0.1:8011/api/market/load-all?index_name=BROAD_MARKET_750&dry_run=false"
curl.exe -X POST "http://127.0.0.1:8011/api/score/run?index_name=BROAD_MARKET_750"
curl.exe "http://127.0.0.1:8011/api/swing/candidates?index_name=BROAD_MARKET_750&limit=20"
curl.exe "http://127.0.0.1:8011/api/momentum/candidates?index_name=BROAD_MARKET_750&limit=20"
```
