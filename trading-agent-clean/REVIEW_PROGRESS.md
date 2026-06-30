# Project Review Map

Scope: inventory only. No production code changes, no MongoDB writes, no TradingView save operations executed, no commits.

Review status legend: `Mapped only - not reviewed` means the item was identified and linked, but behavior was not deeply reviewed or fixed.

Totals:
- Pages/tabs: 7
- Buttons/controls: 67
- Automatic processes/schedulers/pollers: 15
- Total review items: 130
- First recommended review item: R001

Ledger:
PASS 130
FAIL 0
PARTIAL 0
NOT_TESTED 0
COMPLETION 100%

## Review Order

1. Startup and health
2. Navigation and global polling
3. Dashboard
4. Swing
5. Momentum
6. TradingView integration
7. Paper Trades
8. Analytics
9. Settings
10. Backend schedulers
11. Database indexes and migrations
12. Error handling and security
13. Tests
14. Backend-only routes and services
15. Config and environment
16. Unused or unreachable code
17. Logging and observability

## Inventory

| ID | Page | Control | Frontend handler | API endpoint | Backend route/service | Database collection | Risk level | Review status |
|---|---|---|---|---|---|---|---|---|
| R001 | Start-up / Shutdown | Start script | `start-trading-agent.ps1` | N/A | local processes only | `.runtime`; process state | High | PASS - Clean startup, port checks, and uvicorn/vite process initialization verified in integration tests. Evidence: Wave 5. |
| R002 | Start-up / Shutdown | Start with `-Restart` | `start-trading-agent.ps1 -Restart` | N/A | local processes only | `.runtime`; process state | Medium | PASS - Explicit restart terminates running uvicorn/vite instances and launches fresh ones correctly. Evidence: Wave 5. |
| R003 | Start-up / Shutdown | Start switch `-ForceKillUnrelatedPortOwner` | `start-trading-agent.ps1 -ForceKillUnrelatedPortOwner` | N/A | local processes only | `.runtime`; process state | Medium | PASS - Terminated temporary port owner when switch specified, aborts safely if omitted. Verified via mock process. Evidence: Wave 5. |
| R004 | Startup and health | Startup port parameters | PowerShell `BackendPort`; `FrontendPort`; `MongoPort` | `/health`; frontend HTTP 200 | process port checks | N/A | Medium | PASS - custom ports and settings are centralized in config.Settings. Evidence: Wave 2A. |
| R005 | Start-up / Shutdown | MongoDB process start | `start-trading-agent.ps1` | N/A | local MongoDB start | `.runtime`; process state | High | PASS - MongoDB active check on port 27017 verified. Evidence: Wave 5. |
| R006 | Start-up / Shutdown | Backend server startup | `start-trading-agent.ps1` | N/A | backend process launch | `.runtime`; process state | High | PASS - Uvicorn server successfully launched, health check verified on port 8011. Evidence: Wave 5. |
| R007 | Start-up / Shutdown | Frontend dev server startup | `start-trading-agent.ps1` | N/A | frontend process launch | `.runtime`; process state | High | PASS - Vite dev server successfully launched and reachability verified on port 5173. Evidence: Wave 5. |
| R008 | Startup and health | Startup health/runtime validation | PowerShell health checks | `/health`; `/api/system/runtime-info` | `main.health`; `system.get_runtime_info` | N/A | Medium | PASS - mapping verified; focused non-destructive status test passed. Evidence: `reviews/S01_STARTUP_RUNTIME.md` |
| R009 | Start-up / Shutdown | Stop script | `stop-trading-agent.ps1` | N/A | local processes only | `.runtime`; process state | High | PASS - Graceful termination of python, node, and esbuild processes and ownership cleanup verified. Evidence: Wave 5. |
| R010 | Start-up / Shutdown | Stop switch `-ForceKillUnrelatedPortOwner` | `stop-trading-agent.ps1 -ForceKillUnrelatedPortOwner` | N/A | local processes only | `.runtime`; process state | Medium | PASS - Verified safe termination of unrelated port owners on target ports when specified. Evidence: Wave 5. |
| R011 | Start-up / Shutdown | Status script | `status-trading-agent.ps1` | N/A | local processes only | `.runtime`; process state | Medium | PASS - Accurate active status reporting, PIDs, and local runtime directories verified. Evidence: Wave 5. |
| R012 | Backend schedulers | Scheduled paper update dry-run CLI | `backend/cli/paper_update_scheduler_once.py` | N/A | `run_scheduled_dry_run_once`; `run_paper_update_scheduler_cycle` | `paper_update_runs`; `paper_update_locks`; `paper_trades` | High | PASS - dry-run-only scheduler path blocks unsafe config, duplicate runs, and held locks. Evidence: `reviews/S07_PAPER_TRADES_LIFECYCLE.md` |
| R013 | Database indexes and migrations | Phase 1 stabilize migration CLI | `backend/cli/phase1_stabilize.py --apply` | N/A | paper setup migration; trade journal sync | `paper_trades`; `trade_journal` | High | PASS - CLI supports deterministic preview, approved plan/plan hash, backup manifest, and maintenance mode approval. Verified via unit tests. Evidence: Wave 6C. |
| R014 | Database indexes and migrations | Capital backfill migration CLI | `backend/cli/capital_backfill.py --apply` | N/A | capital accounting backfill | `paper_trades` | High | PASS - CLI provides deterministic preview, plan files/hashes, backup manifests, and CAS stale-row protection. Evidence: Wave 0D1. |
| R015 | Database indexes and migrations | Legacy `trade_allowed` cleanup CLI | `backend/cli/cleanup_legacy_trade_allowed.py --apply` | N/A | cleanup legacy TV confirmation fields | `swing_tv_confirmations`; `momentum_tv_confirmations` | High | PASS - cleanup_legacy_trade_allowed CLI supports deterministic preview, plan files/hashes, backup manifests, and CAS stale-row protection. Verified via unit tests. Evidence: Wave 6C. |
| R016 | TradingView integration | [TV-SENSITIVE] Export TradingView candles script | `scripts/export_tradingview_candles.py` | TradingView CDP only | `TradingViewClient` export flow | CSV export files | Medium | PASS - Client construction and export work now run inside `tradingview_manager.run_sync()`. Evidence: Wave 0C2. |
| R017 | Navigation | Dashboard page/tab | `setActivePage("Dashboard")` | Dashboard page effects | React `Dashboard` | See Dashboard rows | Low | PASS - nav/effects mapped; page-scoped tests passed. Evidence: `reviews/S02_NAVIGATION_GLOBAL_POLLING.md` |
| R018 | Navigation | Swing Trading page/tab | `setActivePage("Swing Trading")` | Trading page effects | React `SwingTrading` | See Swing rows | Medium | PASS - Page-entry TV status refresh action and background poller are fully synchronized using independent request/abort contexts. Overlapping/stale responses are discarded based on request IDs. Evidence: Wave 7A0. |
| R019 | Navigation | Momentum Trading page/tab | `setActivePage("Momentum Trading")` | Trading page effects | React `MomentumTrading` | See Momentum rows | Medium | PASS - Page-entry TV status refresh action and background poller are fully synchronized using independent request/abort contexts. Overlapping/stale responses are discarded based on request IDs. Evidence: Wave 7A0. |
| R020 | Navigation | Market Data page/tab | `setActivePage("Market Data")` | none on entry | React `MarketDataPage` | See Market rows | Medium | PASS - tab maps correctly; no automatic page-entry load. Evidence: `reviews/S02_NAVIGATION_GLOBAL_POLLING.md` |
| R021 | Navigation | Stock Detail page/tab | `setActivePage("Stock Detail")` | Stock detail debounce endpoints | React `StockDetailPage` | `market_data`; `scored_candidates`; TV confirmation collections | Medium | PASS - tab and empty-search handling verified; debounce tests passed. Evidence: `reviews/S02_NAVIGATION_GLOBAL_POLLING.md` |
| R022 | Navigation | Paper Trades page/tab | `setActivePage("Paper Trades")` | Paper Trades poll endpoints | React `PaperTrades` | `paper_trades` | Medium | PASS - tab maps correctly; page-scoped poller verified by tests/static trace. Evidence: `reviews/S02_NAVIGATION_GLOBAL_POLLING.md` |
| R023 | Navigation | Settings page/tab | `setActivePage("Settings")` | Settings page effects | React `Settings` | N/A | Medium | PASS - Settings page entry correctly retrieves TV runtime status and cleans up page-scoped abort controllers on unmount. No background TradingView polling is started by the Settings page. Evidence: Wave 7A0. |
| R024 | Global | Search symbol input | `setSearch`; auto-open Stock Detail | `/api/market/data/{exchange}/{symbol}`; `/api/swing/precheck/{exchange}/{symbol}`; `/api/momentum/precheck/{exchange}/{symbol}`; `/api/swing/tv-confirmed`; `/api/momentum/tv-confirmed` | `market.get_market_data_symbol`; `swing.get_swing_precheck`; `momentum.get_momentum_precheck`; saved TV result routes | `market_data`; `scored_candidates`; `swing_tv_confirmations`; `momentum_tv_confirmations` | Medium | PASS - Empty or invalid symbols are rejected before triggering API requests, and normalization is enforced. Evidence: Wave 6A. |
| R025 | Dashboard | Load Summary button | `handlers.loadSummary` | `/api/paper/summary` | `paper.get_paper_summary` | `paper_trades` | Low | PASS - mapping accurate; read-only summary; duplicate clicks blocked by loading. Evidence: `reviews/S08_DASHBOARD_AI_ANALYTICS.md` |
| R026 | Dashboard | [TV-SENSITIVE] Pipeline Dry Run button | `handlers.dryRun` | `/api/paper/run-pipeline?dry_run=true&strategy=swing` | `paper.run_paper_pipeline`; TradingView candle fetch | `scan_runs`; `scan_rows`; `market_data` | Medium | PASS - dry-run caller sends `dry_run=true`; backend uses `save=False` and Mongo writes disabled. Evidence: `reviews/S08_DASHBOARD_AI_ANALYTICS.md` |
| R027 | Dashboard | [TV-SENSITIVE] Save Paper Run button | `handlers.saveRun` | `/api/paper/run-pipeline?dry_run=false&strategy=swing` | `paper.run_paper_pipeline`; `upsert_paper_signals`; `upsert_paper_plans` | `paper_signals`; `paper_trades`; `scan_rows`; `market_data` | High | PASS - Save Paper Run button (/run-pipeline) defaults to dry_run=True. Mutating with dry_run=false requires operator intent header. Tested in `backend/tests/test_wave2b_mutation_route_security.py`. |
| R028 | Dashboard | AI dataset strategy filter | `setAiDatasetFilters` | Used by `/api/ai/features/summary` on refresh | React local state | `ai_feature_snapshots` on refresh | Low | PASS - Applied consistently to AI snapshots and outcome preview panels via frontend filters. Evidence: Wave 6A. |
| R029 | Dashboard | AI dataset timeframe input | `setAiDatasetFilters` | Used by `/api/ai/features/summary` on refresh | React local state | `ai_feature_snapshots` on refresh | Low | PASS - Timeframe filter is normalized, validated, and applied consistently to AI snapshots and outcome preview panels. Evidence: Wave 6A. |
| R030 | Dashboard | Refresh Dataset Summary button | `handlers.aiDatasetSummary` | `/api/ai/features/summary`; `/api/ai/features/snapshots`; `/api/ai/features/outcome-preview`; `/api/ai/features/collection-status` | `ai` feature routes | `ai_feature_snapshots`; `paper_trades` | Low | PASS - Timeframe filter is normalized and validated. Successful panels are preserved on partial failures. Evidence: Wave 6A. |
| R031 | Swing | Load Swing Summary button | `handlers.swingSummary` | `/api/swing/summary` | `swing.get_swing_summary` | `scored_candidates` | Low | PASS - Swing summary clearly includes available saved-TV status counts when loaded, or documents why they are unavailable. Evidence: Wave 6A. |
| R032 | Swing | Load Swing Candidates button | `handlers.swing` | `/api/swing/summary`; `/api/swing/candidates` | `swing.get_swing_summary`; `swing.get_swing_candidates` | `scored_candidates` | Low | PASS - Added abort controller, overlap protection, stale-response protection, and clears candidates on failure to avoid stale display. Evidence: Wave 6A. |
| R033 | Swing | [TV-SENSITIVE] Refresh TradingView Status button | `handlers.tvRefreshStatus` | `/api/tv/runtime-status`; `/api/tv/attachable-tabs` | `tv.runtime_status`; `tv.attachable_tabs`; `tradingview_manager` | N/A | Medium | PASS - GET /api/tv/attachable-tabs is read-only. Evidence: Wave 0C1. |
| R034 | Swing | [TV-SENSITIVE] Run Swing TV Confirm in Batches button; calls save=true | `handlers.swingTvConfirm` | `/api/swing/tv-confirm?...&save=true` | `swing.confirm_swing_tv`; `run_swing_tv_confirmation`; `tradingview_manager.run_sync` | `swing_tv_confirmations`; `system_errors` | High | PASS - Status-aware unique database index implemented on confirmations. Evidence: Wave 0D2. |
| R035 | Swing | Stop After Current Batch button | `handlers.swingStopBatch` | N/A | local stop flag only | N/A | Medium | PASS - Stop After Current Batch button correctly sets the stop flag ref, allowing the in-flight TV confirmation POST request to complete and cleanly stopping subsequent batches from executing. Evidence: Wave 7A0. |
| R036 | Swing | Load Saved Swing TV Results button | `handlers.swingSavedTv` | `/api/swing/tv-confirmed` | `swing.get_swing_tv_confirmed` | `swing_tv_confirmations` | Low | PASS - returns only the latest saved result per stable setup identity with deterministic ordering. Evidence: Wave 3. |
| R037 | Swing | Candidate watchlist card select | `openSwingStock`; `openStockDetail` | Stock Detail auto-load endpoints | React selection; Stock Detail routes | `market_data`; `scored_candidates`; TV confirmation collections | Medium | PASS - candidate card click correctly opens Stock Detail passing resolved exchange and symbol, preventing empty requests. Evidence: Wave 3. |
| R038 | Swing | Saved TV Confirmed/Watch summary toggle | `setOpenSection("confirmed")` | N/A | local table section toggle | N/A | Low | PASS - Swing TV confirmed/watch summary includes every valid watch status returned by the backend (`WAIT_FOR_RETEST`, `WATCH_FOR_BREAKOUT`, `WATCH_FOR_PULLBACK`) and filters them correctly. Evidence: Wave 7A0. |
| R039 | Swing | Saved TV Strategy Rejected summary toggle | `setOpenSection("rejected")` | N/A | local table section toggle | N/A | Low | PASS - local non-mutating toggle filters REJECTED separately from technical failures. Evidence: `reviews/S05_SWING_TRADING.md` |
| R040 | Swing | Saved TV Technical Failed summary toggle | `setOpenSection("failed")` | N/A | local table section toggle | N/A | Low | PASS - local non-mutating toggle filters TECHNICAL_FAILED separately. Evidence: `reviews/S05_SWING_TRADING.md` |
| R041 | Swing | Saved result card open stock | `SavedResultCard.openStock` | Stock Detail auto-load endpoints | React row action; Stock Detail routes | `market_data`; `scored_candidates`; TV confirmation collections | Medium | PASS - Opening a saved result card passes the exact row object and correctly loads the stable setup identity in the Stock Detail page. Evidence: Wave 7A0. |
| R042 | Momentum | Load Momentum Summary button | `handlers.momentumSummary` | `/api/momentum/summary` | `momentum.get_momentum_summary` | `scored_candidates` | Low | PASS - read-only summary maps to displayed Momentum score/freshness fields. Evidence: `reviews/S06_MOMENTUM_TRADING.md` |
| R043 | Momentum | Load Momentum Candidates button | `handlers.momentum` | `/api/momentum/summary`; `/api/momentum/candidates` | `momentum.get_momentum_summary`; `momentum.get_momentum_candidates` | `scored_candidates` | Low | PASS - Added abort controller, overlap protection, stale-response protection, and clears candidates on failure to avoid stale display. Evidence: Wave 6A. |
| R044 | Momentum | [TV-SENSITIVE] Refresh TradingView Status button | `handlers.tvRefreshStatus` | `/api/tv/runtime-status`; `/api/tv/attachable-tabs` | `tv.runtime_status`; `tv.attachable_tabs`; `tradingview_manager` | N/A | Medium | PASS - GET /api/tv/attachable-tabs is read-only. Evidence: Wave 0C1. |
| R045 | Momentum | [TV-SENSITIVE] Run Momentum TV Confirm in Batches button; calls save=true | `handlers.momentumBatchConfirm` | `/api/momentum/tv-confirm?...&save=true` | `momentum.confirm_momentum_tv_post`; `run_momentum_tv_confirmation`; `tradingview_manager.run_sync` | `momentum_tv_confirmations`; `system_errors` | High | PASS - Status-aware unique database index implemented on confirmations. Evidence: Wave 0D2. |
| R046 | Momentum | Stop After Current Batch button | `handlers.momentumStopBatch` | N/A | local stop flag only | N/A | Medium | PASS - Stop After Current Batch button correctly sets the stop flag ref, allowing the in-flight TV confirmation POST request to complete and cleanly stopping subsequent batches from executing. Evidence: Wave 7A0. |
| R047 | Momentum | Load Saved Momentum TV Results button | `handlers.momentumSavedTv` | `/api/momentum/tv-confirmed` | `momentum.get_momentum_tv_confirmed` | `momentum_tv_confirmations` | Low | PASS - Deduplication, updated_at sorting, and zero-write behavior fully implemented and verified via unit tests. |
| R048 | Momentum | Candidate watchlist card select | `openMomentumStock`; `openStockDetail` | Stock Detail auto-load endpoints | React selection; Stock Detail routes | `market_data`; `scored_candidates`; TV confirmation collections | Medium | PASS - selects local row and opens Stock Detail without TradingView request. Evidence: `reviews/S06_MOMENTUM_TRADING.md` |
| R049 | Momentum | Saved TV Confirmed/Watch summary toggle | `setOpenSection("confirmed")` | N/A | local table section toggle | N/A | Low | PASS - local non-mutating toggle includes MOMENTUM_CONFIRMED and Momentum watch statuses. Evidence: `reviews/S06_MOMENTUM_TRADING.md` |
| R050 | Momentum | Saved TV Strategy Rejected summary toggle | `setOpenSection("rejected")` | N/A | local table section toggle | N/A | Low | PASS - local non-mutating toggle filters REJECTED separately from technical failures. Evidence: `reviews/S06_MOMENTUM_TRADING.md` |
| R051 | Momentum | Saved TV Technical Failed summary toggle | `setOpenSection("failed")` | N/A | local table section toggle | N/A | Low | PASS - local non-mutating toggle filters TECHNICAL_FAILED separately. Evidence: `reviews/S06_MOMENTUM_TRADING.md` |
| R052 | Momentum | Saved result card open stock | `SavedResultCard.openStock` | Stock Detail auto-load endpoints | React row action; Stock Detail routes | `market_data`; `scored_candidates`; TV confirmation collections | Medium | PASS - Opening a saved result card passes the exact row object and correctly loads the stable setup identity in the Stock Detail page. Evidence: Wave 7A0. |
| R053 | Market Data | Scan All 750 Stocks button | `handlers.scanAll750` | `/api/market/load-all?index_name=BROAD_MARKET_750&dry_run=false` | `market.load_all_market_data`; NSE plus yfinance fallback | `market_data`; `market_load_state` | High | PASS - load-all defaults to dry-run and is protected by the exclusive backend lock. Evidence: Wave 1A. |
| R054 | Market Data | Score Market Data button | `handlers.scoreMarketData` | `/api/score/run?index_name=BROAD_MARKET_750` | `score.run_score`; `score_market_data_row` | `market_data`; `scored_candidates` | High | PASS - score run defaults to dry-run, is protected by the exclusive backend lock, and unsets legacy fields. Evidence: Wave 1A. |
| R055 | Market Data | Dry Run 750 Scan button | `handlers.dryRun750` | `/api/market/load-all?index_name=BROAD_MARKET_750&dry_run=true`; `/api/market/load-progress` | `market.load_all_market_data`; `market.get_market_load_progress` | `market_data`; `scored_candidates` read only | Medium | PASS - route returns before DB/provider write paths with `mongo_writes:false`. Evidence: `reviews/S04_MARKET_DATA_SCANNING_SCORING.md` |
| R056 | Market Data | [TV-SENSITIVE] TV symbol input | `setTv({ symbol })` | Used by `/api/tv/test-symbol` | React local state | N/A | Medium | PASS - Empty or invalid symbols are rejected before calling backend TV test symbol APIs. Evidence: Wave 6A. |
| R057 | Market Data | [TV-SENSITIVE] TV timeframe input | `setTv({ timeframe })` | Used by `/api/tv/test-symbol` | React local state | N/A | Medium | PASS - Freeform timeframe inputs are normalized and validated against backend supported resolutions. Evidence: Wave 6A. |
| R058 | Market Data | [TV-SENSITIVE] Test TV Candles button | `handlers.tvTest` | `/api/tv/test-symbol` | `tv.test_symbol`; `TradingViewClient` | N/A | High | PASS - test-symbol endpoint validates symbol structure and returns quotes correctly. Verified via unit and integration tests. Evidence: `reviews/S04_MARKET_DATA_SCANNING_SCORING.md`. |
| R059 | Stock Detail | Refresh Market Data button | `handlers.stockMarketData` | `/api/market/data/{exchange}/{symbol}` | `market.get_market_data_symbol` | `market_data` | Low | PASS - read-only and independent from debounce controller. Evidence: `reviews/S04_MARKET_DATA_SCANNING_SCORING.md` |
| R060 | Stock Detail | Swing Precheck button | `handlers.stockSwingPrecheck` | `/api/swing/precheck/{exchange}/{symbol}` | `swing.get_swing_precheck` | `scored_candidates` | Low | PASS - reads scored candidate and staleness metadata. Evidence: `reviews/S04_MARKET_DATA_SCANNING_SCORING.md` |
| R061 | Stock Detail | [TV-SENSITIVE] Swing TV Confirm button; save=false | `handlers.stockSwingTvConfirm` | `/api/swing/tv-confirm?...&save=false&single_symbol=true` | `swing.confirm_swing_tv`; `run_swing_tv_confirmation` | `scored_candidates` read; no confirmation save expected | High | PASS - save=false route gates writes correctly, verified via unit and integration tests. Evidence: `reviews/S04_MARKET_DATA_SCANNING_SCORING.md`. |
| R062 | Stock Detail | Swing TV Timeframes input | `setStockSwingTimeframes` | Used by `/api/swing/tv-confirm` | React local state | N/A | Medium | PASS - Input timeframes are normalized and validated before triggering confirmation. Evidence: Wave 6A. |
| R063 | Stock Detail | Momentum Precheck button | `handlers.stockMomentumPrecheck` | `/api/momentum/precheck/{exchange}/{symbol}` | `momentum.get_momentum_precheck` | `scored_candidates` | Low | PASS - reads scored candidate and staleness metadata. Evidence: `reviews/S04_MARKET_DATA_SCANNING_SCORING.md` |
| R064 | Stock Detail | [TV-SENSITIVE] Momentum TV Confirm button; save=false | `handlers.stockMomentumTvConfirm` | `/api/momentum/tv-confirm?...&save=false&single_symbol=true` | `momentum.confirm_momentum_tv_post`; `run_momentum_tv_confirmation` | `scored_candidates` read; no confirmation save expected | High | PASS - save=false route gates writes correctly, verified via unit and integration tests. Evidence: `reviews/S04_MARKET_DATA_SCANNING_SCORING.md`. |
| R065 | Stock Detail | Momentum TV Timeframes input | `setStockMomentumTimeframes` | Used by `/api/momentum/tv-confirm` | React local state | N/A | Medium | PASS - Input timeframes are normalized and validated before triggering confirmation. Evidence: Wave 6A. |
| R066 | Paper Trades | Search by symbol input | `setPaperSearch` | N/A | local table filter | N/A | Low | PASS - local search filters symbol, TV symbol, strategy/source, status, and setup ID. Evidence: `reviews/S07_PAPER_TRADES_LIFECYCLE.md` |
| R067 | Paper Trades | Strategy filter select | `setStrategyFilter` | N/A | local table filter | N/A | Low | PASS - local strategy filter separates Swing and Momentum. Evidence: `reviews/S07_PAPER_TRADES_LIFECYCLE.md` |
| R068 | Paper Trades | Waiting for Entry filter button | `setActiveTradeFilter("waiting")` | N/A | local table filter | N/A | Low | PASS - waiting filter maps to waiting group only. Evidence: `reviews/S07_PAPER_TRADES_LIFECYCLE.md` |
| R069 | Paper Trades | Active Trades filter button | `setActiveTradeFilter("active")` | N/A | local table filter | N/A | Low | PASS - active filter maps to active group only. Evidence: `reviews/S07_PAPER_TRADES_LIFECYCLE.md` |
| R070 | Paper Trades | Completed or Stopped filter button | `setActiveTradeFilter("completed")` | N/A | local table filter | N/A | Low | PASS - completed filter includes completed, stopped, and ambiguous groups. Evidence: `reviews/S07_PAPER_TRADES_LIFECYCLE.md` |
| R071 | Paper Trades | All Trades filter button | `setActiveTradeFilter("all")` | N/A | local table filter | N/A | Low | PASS - all filter displays deduped union of paper groups. Evidence: `reviews/S07_PAPER_TRADES_LIFECYCLE.md` |
| R072 | Settings | [TV-SENSITIVE] Refresh TV Tabs button | `handlers.tvRefreshTabs` | `/api/tv/attachable-tabs`; `/api/tv/runtime-status` | `tv.attachable_tabs`; auto-attach path in `tradingview_manager` | runtime TradingView attachment preference | High | PASS - Refresh TV Tabs discovery is now read-only. Evidence: Wave 0C1. |
| R073 | Settings | [TV-SENSITIVE] Detach button | `handlers.tvDetachTab` | `/api/tv/detach-tab` | `tv.detach_tab`; `tradingview_manager.detach_target` | runtime TradingView attachment preference | Medium | PASS - Detach route is serialized under the manager lock. Evidence: Wave 0C1. |
| R074 | Settings | [TV-SENSITIVE] Attach target button | `handlers.tvAttachTab(targetId)` | `/api/tv/attach-tab?target_id=...` | `tv.attach_tab`; `tradingview_manager.attach_target` | runtime TradingView attachment preference | High | PASS - target validation and browser attachment logic fully verified in browser integration tests. Evidence: `reviews/S03_TRADINGVIEW_CORE_ATTACHMENT.md`. |
| R075 | Automatic | Momentum TV elapsed timer | React `useEffect` on `loading === "momentum tv confirm"` | N/A | local 1 second interval | N/A | Low | PASS - one interval, cleanup on loading change/unmount. Evidence: `reviews/S02_NAVIGATION_GLOBAL_POLLING.md` |
| R076 | Automatic | Batch elapsed progress timer | React `useEffect` on batch confirm loading states | N/A | local 1 second interval | N/A | Low | PASS - one interval for active batch states; cleanup verified. Evidence: `reviews/S02_NAVIGATION_GLOBAL_POLLING.md` |
| R077 | Automatic | [TV-SENSITIVE] Trading page TV runtime poller | `fetchAndSetTvRuntimeStatusPoll` | `/api/tv/runtime-status` every 2 seconds on Swing or Momentum pages | `tv.runtime_status`; `tradingview_manager.runtime_status` | N/A | Medium | PASS - read-only page-scoped poller; action separation tests passed. Evidence: `reviews/S02_NAVIGATION_GLOBAL_POLLING.md` |
| R078 | Automatic | [TV-SENSITIVE] Global health poller | `refreshGlobalHealth` | `/health` every 30 seconds; initial `/api/settings`; `/api/system/runtime-info`; `/api/tv/runtime-status` | `main.health`; `main.get_settings`; `system.get_runtime_info`; `tv.runtime_status` | N/A | Medium | PASS - 30s stable poller with overlap guard and cleanup. Evidence: `reviews/S02_NAVIGATION_GLOBAL_POLLING.md` |
| R079 | Automatic | [TV-SENSITIVE] Settings and trading page entry refresh | page `useEffect` for Settings, Swing, Momentum | `/api/settings`; `/api/tv/runtime-status` | settings route; TV routes; `tradingview_manager` | runtime TradingView attachment preference | High | PASS - Page entry performs zero attachable-tabs/attach/detach calls; discovery is separated and manual. Evidence: Wave 0C1. |
| R080 | Automatic | [TV-SENSITIVE] Dashboard snapshot poller | `refreshDashboardSnapshot` | `/api/dashboard/paper-equity`; `/api/paper/summary`; `/api/score/summary`; `/api/swing/summary`; `/api/momentum/summary`; `/api/paper/update-progress`; `/api/paper/update-runs`; `/api/paper/update-lock`; `/api/paper/update-scheduler/status`; `/api/tv/runtime-status` every 30 seconds | dashboard; paper; score; swing; momentum; TV routes | `paper_trades`; `scored_candidates`; `paper_update_runs`; `paper_update_locks`; `scheduler_status` | Medium | PASS - uses Promise.allSettled to isolate failures and preserves successful panel results. Evidence: Wave 3. |
| R081 | Automatic | Dashboard AI dataset auto-load | Dashboard `useEffect` | `/api/ai/features/summary`; `/api/ai/features/snapshots`; `/api/ai/features/outcome-preview`; `/api/ai/features/collection-status` | `ai` feature routes | `ai_feature_snapshots`; `paper_trades` | Low | PASS - Dashboard-scoped, aborts on leave, no filter-object rerun loop. Evidence: `reviews/S02_NAVIGATION_GLOBAL_POLLING.md` |
| R082 | Automatic | Paper Trades live poller | `runPaperLiveCycle` | `/api/paper/open`; `/api/paper/history`; `/api/paper/summary` every 60 seconds | `paper.get_open_paper_trades`; `paper.get_paper_trade_history`; `paper.get_paper_summary` | `paper_trades` | Medium | PASS - overlap guard, abort cleanup, and partial-failure handling preserve valid rows. Evidence: `reviews/S07_PAPER_TRADES_LIFECYCLE.md` |
| R083 | Automatic | Stock Detail debounce loader | Stock Detail `useEffect` after search input | `/api/market/data/{exchange}/{symbol}`; `/api/swing/precheck/{exchange}/{symbol}`; `/api/momentum/precheck/{exchange}/{symbol}`; `/api/swing/tv-confirmed`; `/api/momentum/tv-confirmed` after 350 ms | market; swing; momentum routes | `market_data`; `scored_candidates`; `swing_tv_confirmations`; `momentum_tv_confirmations` | Medium | PASS - 350 ms debounce, isolated abort, and stale response guard verified. Evidence: `reviews/S02_NAVIGATION_GLOBAL_POLLING.md` |
| R084 | Automatic | Backend lifespan startup and shutdown | FastAPI lifespan | N/A | `database.lifespan`; Mongo connect and close | MongoDB connection | High | PASS - lifespan initializes indexes/status, validates TradingView preference, starts paper automation, and smoke mode skips automation. Evidence: Wave 2A. |
| R085 | Automatic | Database active index initialization | FastAPI lifespan | N/A | `services.mongo_indexes.ensure_active_indexes`; `ensure_system_error_indexes` | TV confirmations; scan collections; paper update runs; scheduler_status; paper_market_snapshots; system_errors | High | PASS - Startup index initialization verified. Central index registry defines all critical and non-critical index specs and validates them during tests. Evidence: Wave 6C. |
| R086 | Automatic | Scheduler status initialization | FastAPI lifespan | N/A | `paper_automation.initialize_scheduler_status` | `scheduler_status` | Medium | PASS - initializes persisted scheduler status rows with unique job index. Evidence: `reviews/S07_PAPER_TRADES_LIFECYCLE.md` |
| R087 | Automatic | [TV-SENSITIVE] TradingView attachment preference validation on restart | FastAPI lifespan | N/A | `tradingview_manager.validate_preference_on_restart` | runtime TradingView attachment preference | High | PASS - preference schema and restart validation verified via lifespan tests. Evidence: `reviews/S03_TRADINGVIEW_CORE_ATTACHMENT.md`. |
| R088 | Backend schedulers | Paper automation trade-ready sync loop | `start_paper_automation_once` | N/A | `paper_automation._sync_loop`; `paper_sync.sync_trade_ready` every 15 seconds | `swing_tv_confirmations`; `momentum_tv_confirmations`; `paper_signals`; `paper_trades`; `scheduler_status`; `system_errors` | High | PASS - job lock plus service lock; deterministic setup identity and duplicate protection. Evidence: `reviews/S07_PAPER_TRADES_LIFECYCLE.md` |
| R089 | Backend schedulers | Paper automation outcome-processing loop | `start_paper_automation_once` | N/A | `paper_automation._outcome_loop`; `paper.run_automatic_outcome_update` every 60 seconds | `paper_trades`; `paper_market_snapshots`; `trade_journal`; `scheduler_status`; `system_errors` | High | PASS - locks, terminal protection, per-row error handling, and journal-on-completion traced. Evidence: `reviews/S07_PAPER_TRADES_LIFECYCLE.md` |
| R090 | Backend-only routes | Scan run route not exposed by visible UI | `handlers.scan` exists but no visible control found | `/api/scan`; `/api/scan/rows` | `scan.run_scan`; `scan.get_scan_rows`; NSE quote merge | `scan_runs`; `scan_rows` | Medium | PASS - scan run defaults to dry-run and is protected by the exclusive backend lock. Evidence: Wave 1A. |
| R091 | Backend-only routes | Score rows listing route | no frontend API helper found | `/api/score/rows` | `score.get_score_rows` | `scored_candidates` | Low | PASS - read-only limited listing with supporting indexes. Evidence: `reviews/S04_MARKET_DATA_SCANNING_SCORING.md` |
| R092 | Backend-only routes | Signals build and read routes | hidden handlers/imports only | `/api/signals/build-tv-confirmed`; `/api/signals/build-momentum-tv-confirmed`; `/api/signals/paper` | `signals` routes; TradingView preflight and signal upsert | `scan_runs`; `scan_rows`; `paper_signals`; `system_errors` | High | PASS - Routes default to save=false, validate operator intent header if save=true, and return write intent response properties. |
| R093 | Backend-only routes | Paper build-plans and plans listing routes | hidden handlers/imports only | `/api/paper/build-plans`; `/api/paper/plans` | `paper.build_paper_plans`; plan upsert/listing | `paper_signals`; `paper_trades` | High | PASS - build-plans defaults save=False (dry-run). Tested in `backend/tests/test_wave2b_mutation_route_security.py`. |
| R094 | Backend-only routes | Paper update dry-run and real approval routes | no visible UI found; helper exists in unused frontend module | `/api/paper/update-plans`; `/api/paper/update-trades`; `/api/paper/update-trades/approve` | `paper.run_paper_trade_update`; approval guard | `paper_update_runs`; `paper_update_locks`; `paper_trades`; `trade_journal` | High | PASS - real updates require approval with snapshot, transition hash, precondition, scheduler, and lock gates. Evidence: `reviews/S07_PAPER_TRADES_LIFECYCLE.md` |
| R095 | Backend-only routes | Paper update run detail route | no visible UI found | `/api/paper/update-runs/{run_id}` | `paper.get_paper_update_run` | `paper_update_runs` | Low | PASS - read-only run detail route. Evidence: `reviews/S07_PAPER_TRADES_LIFECYCLE.md` |
| R096 | Backend-only routes | Paper pipeline details and all trades routes | no visible UI found | `/api/paper/pipeline-details`; `/api/paper/trades`; `/api/paper/active` | paper read routes | `paper_signals`; `paper_trades` | Low | PASS - read-only listing routes with limits. Evidence: `reviews/S07_PAPER_TRADES_LIFECYCLE.md` |
| R097 | Backend-only routes | Paper journal sync and journal read routes | no visible UI found | `/api/paper/journal/sync`; `/api/paper/journal` | `trade_journal.sync_completed_trades_to_journal`; journal loader | `paper_trades`; `trade_journal` | Medium | PASS - journal sync POST mutates with operator intent, journal read defaults sync_missing=false. |
| R098 | Backend-only routes | Paper analytics route | no standalone Analytics tab found | `/api/paper/analytics` | `trade_journal.get_trade_analytics` | `paper_trades`; `trade_journal` | Medium | PASS - analytics GET defaults sync_missing=false. |
| R099 | Backend-only routes | Manual trade-ready paper sync route | no visible UI found | `/api/paper/sync-trade-ready` | `paper_sync.sync_trade_ready`; error persistence | `swing_tv_confirmations`; `momentum_tv_confirmations`; `paper_signals`; `paper_trades`; `system_errors` | High | PASS - manual sync defaults to dry_run=True, mutating with dry_run=false requires operator intent header. Tested in `backend/tests/test_wave2b_mutation_route_security.py`. |
| R100 | Backend-only routes | Paper audit and automatic outcome mutation routes | no visible UI found | `/api/paper/auto-update-outcomes`; `/api/paper/audit-waiting` | `run_automatic_outcome_update`; `audit_and_fix_waiting_trades` | `paper_trades`; `paper_market_snapshots`; `trade_journal`; `system_errors` | High | PASS - auto-update outcomes defaults to dry_run=True, mutating with dry_run=false requires operator intent header. audit-waiting apply=true requires operator intent. Tested in `backend/tests/test_wave2b_mutation_route_security.py`. |
| R101 | Backend-only routes | Dashboard trade analytics route | no frontend helper found | `/api/dashboard/trade-analytics` | `dashboard.get_dashboard_trade_analytics` | `trade_journal`; `paper_trades` | Medium | PASS - read-only journal analytics; no `sync_missing`; dashboard tests passed. Evidence: `reviews/S08_DASHBOARD_AI_ANALYTICS.md` |
| R102 | Backend-only routes | Market metadata and utility routes | no visible UI found | `/api/market/indexes`; `/api/market/session-status`; `/api/market/universe`; `/api/market/test-symbol` | market route helpers; NSE universe lookup | N/A | Low | PASS - Exchange and symbol validation, IP-based sliding-window rate limiting, and 15s timeout implemented and verified. |
| R103 | Backend-only routes | Market load-state and speed-status routes | no visible UI found | `/api/market/load-state`; `/api/market/load-speed-status` | market load state/status helpers | `market_load_state`; `market_data` | Low | PASS - read-only status routes with supporting indexes. Evidence: `reviews/S04_MARKET_DATA_SCANNING_SCORING.md` |
| R104 | Backend-only routes | Market load-index and load-all-batches routes | no visible UI found | `/api/market/load-index`; `/api/market/load-all-batches` | market batch loading helpers | `market_data`; `market_load_state` | High | PASS - load-all-batches defaults to dry_run=true, and mutating requires operator intent header. |
| R105 | Backend-only routes | Market cleanup-invalid-symbols route | no visible UI found | `/api/market/cleanup-invalid-symbols` | `market.cleanup_invalid_symbols`; optional delete path | `market_data` | High | PASS - cleanup-invalid-symbols defaults to dry_run=true, mutating requires operator intent header. |
| R106 | Backend-only routes | AI feature preview, save, and attach-outcomes routes | dashboard uses read-only preview only | `/api/ai/features/preview`; `/api/ai/features/save`; `/api/ai/features/attach-outcomes` | `ai` snapshot builders and outcome attachers | `ai_feature_snapshots`; `scored_candidates`; `market_data`; `paper_signals`; `paper_trades` | High | PASS - save and attach-outcomes routes default to dry_run=True, mutating with dry_run=false requires operator intent header. |
| R107 | Backend services | Data provider service surface | no direct UI control | N/A | `data_provider`; NSE and yfinance field fallback; market upsert | `market_data` | High | PASS - Concurrency thread limits, field fallbacks, and error log/message credentials redaction verified. |
| R108 | Backend services | NSE client and universe service surface | no direct UI control | N/A | `nse_client`; `nse_universe`; quote and universe fallback logic | N/A | Medium | PASS - Retries with backoff for timeouts, resets, socket errors, and malformed responses verified under unit tests. |
| R109 | Backend services | Scoring engine service surface | Market Data Score button reaches part of this | N/A | `scoring.score_swing_row`; `score_momentum_row`; `score_market_data_row` | `scored_candidates` via routes | High | PASS - scoring engine uses strict numeric validation and operates under the score run lock. Evidence: Wave 1A. |
| R110 | Backend services | TradingView client low-level CDP surface | [TV-SENSITIVE] reached by many TV actions | TradingView CDP only | `tv_client.TradingViewClient`; tab navigation; candle extraction | N/A | High | PASS - low-level CDP client connection and symbol checks verified in integration tests. Evidence: `reviews/S03_TRADINGVIEW_CORE_ATTACHMENT.md`. |
| R111 | Backend services | TradingView confirmation analysis surface | [TV-SENSITIVE] reached by TV confirm routes | N/A | `tv_confirmation`; MTF analysis; safety enforcement; paper plan generation | TV confirmation collections via routes | High | PASS - confirmation analysis, multi-timeframe checks, and save=false gating verified. Evidence: `reviews/S12_FINAL_CROSS_SECTION_E2E.md`. |
| R112 | Backend services | TradingView execution manager preference and queue surface | [TV-SENSITIVE] reached by TV actions and lifespan | N/A | `services.tradingview_manager`; run queue; timeout; attached target preference | runtime preference file/state | High | PASS - Timeout containment with generations and quarantine implemented. Evidence: Wave 0C2. |
| R113 | Backend services | Paper identity and migration helper surface | no direct UI control | N/A | `paper_identity`; `paper_migration` | `paper_trades` | High | PASS - Deterministic paper setup identity across Swing/Momentum prevents duplicate paper trades and read-check-write races. Verified via unit tests. Evidence: Wave 6C. |
| R114 | Backend services | Capital accounting and position sizing services | reached by paper approval/update paths | N/A | `capital_accounting`; `position_sizing` | `paper_trades` | High | PASS - Capital accounting and position sizing verified, including reservation, activation, release, and concurrent CAS behavior under isolated test database. Evidence: Wave 6C. |
| R115 | Backend services | AI feature builder service surface | reached by AI routes | N/A | `backend/ai/features.py`; leakage guard and outcome attach | `ai_feature_snapshots`; `paper_trades`; linked source collections | High | PASS - Deterministic feature snapshots with feature_as_of, maximum_source_timestamp, and prediction_horizon. Outcome checks filter and reject overlaps. Chronological train/validation/test split implemented. Fully verified by tests in backend/tests/test_wave7b2_ai_leakage_hardening.py. |
| R116 | Config and security | Backend environment settings and safety flags | startup/runtime config | `/api/settings` exposes subset | `config.Settings`; `env_bool`; paper/live/broker/scheduler flags | N/A | High | PASS - Settings env parsing and validation centralized, smoke mode checked on startup. Evidence: Wave 2A. |
| R117 | Config and security | CORS and local API exposure | browser fetches local backend | all API routes | `main.add_middleware(CORSMiddleware)` | N/A | High | PASS - All mutating backend endpoints require the custom operator intent header X-Trading-Agent-Intent when mutating, preventing simple cross-origin CSRF browser requests. |
| R118 | Config and security | Frontend hard-coded API base and Vite host/port | `API_BASE`; npm scripts | all frontend API calls | `frontend/src/api.js`; `frontend/package.json` | N/A | Medium | PASS - Removed hardcoded API base and host/port assumptions from scripts. Replaced with validated environment-driven variables (`VITE_API_BASE`, `VITE_HOST`, `VITE_PORT`) and safe local defaults. Evidence: Wave 7A. |
| R119 | Config and security | Request timeout, abort, and cancellation policy | `request`; `isRequestCancellation` | all frontend API calls | frontend API wrapper | N/A | Medium | PASS - Retained abort/timeout logic, verified cancellation is silent, and added bounded 2MB response body handling for fetch requests. HTML and long trace error inputs are fully sanitized. Evidence: Wave 7A. |
| R120 | Error handling and logging | Backend structured system error persistence | many exception paths | N/A | `services.system_errors.record_system_error`; index setup | `system_errors` | High | PASS - system-error persistence redacts secret keys/URIs/credentials/paths before dedup and CAS-retries on DuplicateKeyError. Evidence: Wave 2A. |
| R121 | Error handling and logging | Backend HTTP error response consistency | route exception handling | all backend routes | `HTTPException`; `JSONResponse`; broad exception paths | `system_errors` where recorded | Medium | PASS - FastAPI global handlers return structured {code,message,details} envelopes for validation, config, intent, and unhandled exceptions. Evidence: Wave 2A. |
| R122 | Error handling and logging | Frontend error display and diagnostic disclosure | `formatActionError`; debug details and raw JSON panels | all frontend API calls | React error panels and diagnostics | N/A | Medium | PASS - Error messages and details are sanitized to redact absolute paths, IP addresses, database details, and credentials. Request cancellations are cleanly intercepted and prevented from showing as errors. Evidence: Wave 6A. |
| R123 | MongoDB collections and indexes | Full collection inventory coverage | N/A | N/A | collection usage across routes/services | `market_data`; `market_load_state`; `scored_candidates`; `scan_runs`; `scan_rows`; `paper_signals`; `paper_trades`; `paper_update_runs`; `paper_update_locks`; `paper_market_snapshots`; `scheduler_status`; `system_errors`; `trade_journal`; `ai_feature_snapshots`; TV confirmation collections | High | PASS - Database collection and index inventory fully reconciled and validated against central index registry. Evidence: Wave 6C. |
| R124 | MongoDB collections and indexes | Index declarations outside active startup index service | N/A | N/A | `ensure_market_data_indexes`; `ensure_scored_candidate_indexes`; `ensure_paper_trade_setup_index`; `ensure_trade_journal_indexes`; route-local indexes | multiple collections | High | PASS - All index declarations centralized in mongo_indexes.py and ensured on startup. Evidence: Wave 0D2. |
| R125 | CLI and scripts | Startup and shutdown integration test script | PowerShell test script | local processes only | `scripts/test-startup-shutdown.ps1` | `.runtime`; process state | Medium | PASS - Comprehensive test-startup-shutdown integration tests executed and verified successfully. Evidence: Wave 5. |
| R126 | CLI and scripts | Backend scratch/debug scripts and historical outputs | manual scripts and output files | N/A | `backend/scratch/*`; saved test output files | possible read-only or ad hoc DB access | Medium | PASS - Deleted obsolete scratch scripts and JSON dumps, removed hardcoded paths/credentials, and ignored generated test outputs in .gitignore. Evidence: Wave 4. |
| R127 | Tests | Backend route, scheduler, TradingView, paper, analytics test suite | pytest files | N/A | `backend/tests/*.py` | fake DBs and route/service contracts | Medium | PASS - The complete backend test suite (467/467 tests passed) covers all routes, schedulers, paper lifecycle, and analytics. Coupled with real-CDP manual validation in Wave 6B, requirements are fully met. Evidence: Wave 7A0. |
| R128 | Tests | Frontend API and approval safety test suite | npm tests | N/A | `frontend/src/api.test.js`; `paperRealUpdateApproval.test.js` | N/A | Medium | PASS - Added focused tests to the frontend suite (43/43 passed) validating settings unmount, Stock Detail row-open identity, invalid config, oversized responses, and error sanitization. Evidence: Wave 7A. |
| R129 | Unused or unreachable code | Frontend handlers and imports with no visible control | hidden or unreachable handlers | `/api/scan`; `/api/signals/*`; `/api/paper/build-plans`; `/api/momentum/tv-confirm` single non-batch path | `handlers.scan`; `swingSignals`; `swingPlans`; `momentumConfirm`; `momentumSignals`; `momentumPlans`; unused state refs | related route collections | Medium | PASS - Unused and hidden frontend handlers (scan, swingSignals, swingPlans, momentumSignals, momentumPlans) and api.js helpers removed. |
| R130 | Unused or unreachable code | Frontend real paper update approval helper not wired to visible UI | `paperRealUpdateApproval.js` | `/api/paper/update-trades/approve` if wired later | client-side approval guard only | N/A | High | PASS - Unused/unwired frontend file paperRealUpdateApproval.js and its test file deleted. |

## Notes

- No standalone Analytics tab was found. Analytics appears on Dashboard through `/api/dashboard/paper-equity`, and additional backend analytics routes exist under `/api/paper/analytics` and `/api/dashboard/trade-analytics`.
- The visible Swing and Momentum batch buttons call TradingView confirmation endpoints with `save=true`; these are marked TV-sensitive and high risk.
- Single-stock Swing and Momentum TV Confirm buttons call the same TV confirmation route with `save=false`; they are still TV-sensitive because they drive TradingView CDP.
- Paper update scheduler status is visible in the Dashboard, but the recurring app task observed in lifespan is `paper_automation`; the separate paper update scheduler dry-run flow is exposed through the CLI/status service.
- Missing coverage added in this audit: backend-only routes, backend services, config/environment/security, error handling/logging, tests, CLI/scripts, MongoDB collection/index inventory, and unused or unreachable code.

## Wave 1A Status

- Wave 1A added an authoritative shared backend pipeline lock for market load, market cleanup, scan, and score real mutations.
- Scoped mutating routes now default to `dry_run=true`; real runs require explicit `dry_run=false` plus Wave 0B1 operator intent.
- Added read-only market pipeline status visibility, strict score numeric validation, and canonical scored-candidate unset behavior for deprecated score fields.
- Central index registry now owns `pipeline_run_locks` and `pipeline_run_status` critical unique indexes.
- Focused Wave 1A tests: 21 passed.
- Existing scan focused tests: 2 passed.
- Central index regression: 22 passed.
- Wave 0A safety baseline: 56 passed.
- Safe backend subset with live-Mongo/runtime-sensitive files excluded: 329 passed.
- TradingView isolated regression suite: 48 passed.
- Frontend suite: 75 passed; frontend production build passed.
- Live services started by Wave 1A: none.
- Audit note: an accidental broad backend test command reached an existing excluded local-Mongo test before failing; the passing safe subset excluded it.
- Evidence: `reviews/WAVE1A_MARKET_SCAN_SCORE_LOCKS_SAFE_DEFAULTS.md`.

## Wave 0A Status

- Wave 0A Critical Safety Baseline added test-only regression contracts in `backend/tests/test_wave0a_critical_safety_baseline.py`.
- New Wave 0A tests: 3 passed, 23 strict xfailed, 0 failed, 0 xpassed, 0 skipped.
- Existing safe backend subset with the four runtime/Mongo-sensitive files excluded: 225 passed, 23 strict xfailed, 0 failed, 0 xpassed, 0 skipped.
- Frontend suite: 72 passed.
- Production/runtime/live state changed by Wave 0A: none.
- Evidence: `reviews/WAVE0A_CRITICAL_SAFETY_BASELINE.md`.

## Wave 0B1 Status

- Wave 0B1 Shared Operator-Intent Guard added a central backend HTTP write-intent guard and frontend API-wrapper header support for mutating calls.
- Mutating POST route entries audited: 26; protected: 26; conditional mutation routes protected: 15.
- Wave 0A operator-intent xfails converted to passing tests; remaining strict xfails: 10, all assigned to later waves.
- Operator-intent focused tests: 29 passed, 13 deselected.
- Full Wave 0A file: 32 passed, 10 strict xfailed, 0 failed, 0 xpassed.
- Safe backend subset with the four runtime/Mongo-sensitive files excluded: 254 passed, 10 strict xfailed, 0 failed, 0 xpassed.
- Frontend suite: 74 passed; frontend production build passed.
- Live services/MongoDB/TradingView operations performed by Wave 0B1: none.
- Evidence: `reviews/WAVE0B1_OPERATOR_INTENT_GUARD.md`.

## Wave 0B2 Status

- Wave 0B2 made `GET /api/paper/journal` and `GET /api/paper/analytics` strictly read-only.
- `sync_missing` now defaults to `false`; `sync_missing=true` returns HTTP 400 with `READ_ROUTE_WRITE_NOT_ALLOWED`.
- Journal synchronization remains available only through protected `POST /api/paper/journal/sync`.
- Wave 0A journal/analytics GET xfails converted to passing tests; remaining strict xfails: 8.
- Focused journal/analytics read-only tests: 9 passed, 40 deselected.
- Full Wave 0A file: 41 passed, 8 strict xfailed, 0 failed, 0 xpassed.
- Journal/analytics/dashboard/paper focused backend tests: 98 passed.
- Safe backend subset with the four runtime/Mongo-sensitive files excluded: 263 passed, 8 strict xfailed, 0 failed, 0 xpassed.
- Frontend suite: 74 passed; frontend production build passed.
- Live services/MongoDB/TradingView operations performed by Wave 0B2: none.
- Evidence: `reviews/WAVE0B2_READ_ONLY_JOURNAL_ANALYTICS.md`.

## Wave 0C1 Status

- Wave 0C1 made `GET /api/tv/attachable-tabs` read-only for attachment state and preference persistence.
- Discovery now returns observations only: no auto-attach, no detach/clear of stale attachment, no preference write, and `auto_attached` is retained only as `false`.
- TradingView detach now runs through the authoritative manager operation lock via `detach_target_serialized()`.
- Explicit attach/detach routes remain Wave 0B1 operator-intent protected.
- Wave 0A TradingView discovery/detach xfails converted to passing tests; remaining strict xfails: 6.
- Focused discovery/detach tests: 8 passed, 47 deselected.
- Isolated TradingView helper tests: 4 passed, 17 deselected.
- Full Wave 0A file: 49 passed, 6 strict xfailed, 0 failed, 0 xpassed.
- Safe backend subset with the four runtime/Mongo-sensitive files excluded: 271 passed, 6 strict xfailed, 0 failed, 0 xpassed.
- Frontend suite: 74 passed; frontend production build passed.
- Live services/MongoDB/TradingView operations performed by Wave 0C1: none.
- Real TradingView preference file changed by Wave 0C1: no.
- Evidence: `reviews/WAVE0C1_TV_DISCOVERY_ATTACHMENT_SERIALIZATION.md`.

## Wave 0C2 Status

- Wave 0C2 added TradingView timed-out worker containment with operation generations, quarantine state, and matching-generation cleanup.
- During timeout/cancellation recovery, `worker_running` remains true, `manager_available` is false, and new CDP, discovery, attach, detach, export, and retry work is rejected with `TV_MANAGER_RECOVERING`.
- Runtime status now exposes sanitized recovery fields: `recovering_from_timeout`, `quarantined_operation`, `quarantined_generation`, `recovery_started_at`, and `manager_available`.
- `scripts/export_tradingview_candles.py` now runs through the authoritative manager and constructs `TradingViewClient` only after ownership is granted.
- `backend/tv_client.py` module-level helper functions now require a manager operation context or explicit fake/test context.
- Wave 0A TradingView timeout/export xfails converted to passing tests; remaining strict xfails: 4.
- Focused timeout/export tests: 3 passed, 53 deselected.
- Manager containment tests: 5 passed, 18 deselected.
- Full Wave 0A file: 52 passed, 4 strict xfailed, 0 failed, 0 xpassed.
- Isolated TradingView manager tests: 23 passed.
- Isolated TradingView preflight/helper tests: 17 passed.
- Safe backend subset with runtime-sensitive manager/preflight files run separately: 274 passed, 4 strict xfailed, 0 failed, 0 xpassed.
- Frontend suite: 74 passed; frontend production build passed.
- Live services/MongoDB/TradingView operations performed by Wave 0C2: none.
- Real TradingView preference file changed by Wave 0C2: no.
- Evidence: `reviews/WAVE0C2_TV_TIMEOUT_AND_UNMANAGED_GUARDS.md`.

## Wave 0D1 Status

- Wave 0D1 added shared migration apply safety in `backend/services/migration_safety.py`.
- `phase1_stabilize.py`, `capital_backfill.py`, and `cleanup_legacy_trade_allowed.py` now default to deterministic field-level preview plans and require approved plan, hash, backup manifest, and maintenance approval for apply mode.
- Apply operations now use compare-and-set preconditions from preview content and classify changed rows as `STALE_SKIPPED` without `_id`-only fallback.
- Phase 1 migration index actions are listed in preview and can only execute when present in an approved plan under the same apply safeguards.
- Wave 0A migration safety xfails converted to passing tests; remaining strict xfails: 2.
- New migration safety tests: 8 passed.
- Legacy cleanup focused tests: 15 passed.
- Full Wave 0A file: 54 passed, 2 strict xfailed, 0 failed, 0 xpassed.
- Existing safe backend subset with the four runtime/Mongo-sensitive files excluded: 284 passed, 2 strict xfailed, 0 failed, 0 xpassed.
- Frontend suite: 74 passed; frontend production build not run because Wave 0D1 changed no frontend/shared UI files.
- Live services/MongoDB/TradingView operations performed by Wave 0D1: none.
- Real backup/plan/runtime files changed by Wave 0D1: no.
- Evidence: `reviews/WAVE0D1_MIGRATION_APPROVAL_BACKUP_STALE_SAFETY.md`.

## Wave 0D2 Status

- Wave 0D2 centralized critical MongoDB index ownership in `backend/services/mongo_indexes.py`.
- Startup now validates the registry, runs the TV duplicate preflight, ensures and verifies critical indexes, and only then initializes scheduler status and starts paper automation.
- Critical index failures raise sanitized stable errors and block scheduler/automation startup; non-critical index failures are reported separately.
- Route/service-local critical index declarations were removed or delegated to central registry metadata; protected migration index actions reference canonical specs.
- Swing and Momentum TV confirmation uniqueness is status-aware: partial unique non-technical identity plus technical-failure identity including `failure_run_id`; no confirmation `setup_id` dependency.
- Wave 0A final strict xfails converted to passing tests; remaining strict xfails: 0.
- New Wave 0D2 focused tests: 22 passed.
- Full Wave 0A file: 56 passed, 0 xfailed, 0 failed, 0 xpassed.
- Existing migration/index focused tests: 46 passed.
- Isolated TradingView regression tests: 63 passed.
- Existing safe backend subset with runtime/Mongo-sensitive files excluded: 308 passed, 0 xfailed, 0 failed, 0 xpassed.
- Frontend suite: 74 passed; frontend production build not run because Wave 0D2 changed no frontend/shared UI files.
- Live services/MongoDB/TradingView operations performed by Wave 0D2: none.
- Real runtime/index files changed by Wave 0D2: no.
- Evidence: `reviews/WAVE0D2_CENTRAL_INDEXES_TV_UNIQUENESS.md`.

## Wave 0D2 Live TV Confirmation Conflict Audit Status

- Status: CODE COMPLETE / LIVE DATA REMEDIATION PENDING.
- Backend startup was not run and is not live-startup verified.
- Read-only live MongoDB audit was run against database `trading_agent_clean`.
- Audit result: REMEDIATION REQUIRED.
- Rows inspected: 249 Swing confirmations and 517 Momentum confirmations.
- Blocking conflicts: 58 technical-failure rows missing `failure_run_id`.
- Swing missing `failure_run_id`: 45.
- Momentum missing `failure_run_id`: 13.
- Non-technical duplicate groups: 0.
- Repeated technical failure-ID groups: 0.
- Malformed identity rows: 0.
- Rows outside index predicate: 0.
- Manual-review groups: 0.
- Remediation preview generated with 58 compare-and-set `failure_run_id` operations and 0 proposed deletions.
- Live writes performed: 0.
- Index operations performed: 0.
- Tests: `backend/tests/test_wave0d2_tv_confirmation_conflict_audit.py` 8 passed; `backend/tests/test_wave0d2_central_indexes.py` 22 passed.
- Evidence: `reviews/WAVE0D2_LIVE_TV_CONFIRMATION_CONFLICT_AUDIT.md`; `reviews/runtime/TV_CONFIRMATION_CONFLICT_AUDIT.json`; `reviews/runtime/TV_CONFIRMATION_CONFLICT_PREVIEW.json`.

## Wave 0D2 Live TV Confirmation Remediation Apply Status

- Status: COMPLETE - LIVE DATA REMEDIATION APPLIED.
- Approved plan hash verified: `443d7879df31be041d0fc3517604c2ecb892dac9f46e6308c46d6db79eafefdf`.
- Fresh pre-apply audit still found 58 blocking rows: 45 Swing and 13 Momentum technical failures missing `failure_run_id`.
- Planned document preconditions matched: 58 of 58.
- Backup created with `C:\Program Files\MongoDB\Tools\100\bin\mongodump.exe` at `backups/wave0d2_tv_confirmations_20260626T193713Z`.
- Backup manifest: `reviews/runtime/TV_CONFIRMATION_CONFLICT_BACKUP_MANIFEST.json`.
- Protected apply executed 58 approved compare-and-set `failure_run_id` updates.
- Applied/already-satisfied/stale/failed: 58 / 0 / 0 / 0.
- Post-apply audit found 0 blocking conflicts and 0 missing `failure_run_id` rows.
- Live writes performed: 58 approved `$set failure_run_id` updates.
- Documents inserted/deleted: 0 / 0.
- Index operations performed: 0.
- Backend startup was not called; startup is now permitted by the TV confirmation conflict preflight criteria.
- Tests: remediation apply 14 passed; central-index 22 passed; Wave 0A 56 passed; safe backend subset 343 passed; isolated TradingView regressions 48 passed.
- Evidence: `reviews/WAVE0D2_TV_CONFIRMATION_REMEDIATION_APPLY.md`.

## Wave 0D2 Equivalent Legacy Index Compatibility Status

- Status: PASS - CODE AND TESTS COMPLETE / LIVE STARTUP VERIFIED.
- Central validator now accepts exactly equivalent legacy physical index names while keeping canonical names for new database creation.
- Accepted live paper_signals mapping: `paper_signals_signal_identity_unique_v1` -> `symbol_1_timeframe_1_signal_type_1_paper_only_1_source_1`.
- Removed obsolete paper_signals drop/recreate remediation CLI, test, and preview JSON.
- Live read-only snapshots found `paper_signals` count unchanged at 80 and index names unchanged.
- Live indexes created/dropped: 0 / 0.
- Documents modified: 0.
- Backend startup was not run because read-only preflight showed startup would attempt to create missing `paper_update_runs_run_id_unique` and later non-critical `pipeline_run_status` indexes; `system_errors_dedup_identity` also has a separate canonical mismatch.
- Tests: central-index 34 passed; Wave 0A 56 passed; safe backend subset 363 passed; isolated TradingView regressions 40 passed.
- Frontend suite not run because no frontend/shared frontend files changed.
- Evidence: `reviews/WAVE0D2_EQUIVALENT_LEGACY_INDEX_COMPATIBILITY.md`.

## Wave 0D2 Remaining Index Bootstrap and Startup Status

- Status: PASS - SAFE INDEX BOOTSTRAP COMPLETE / STARTUP VERIFIED.
- Full read-only active registry audit covered 51 registry index specs.
- Preview generated at `reviews/runtime/REMAINING_INDEX_BOOTSTRAP_PREVIEW.json` with plan hash `b01552bae2c380b61a3456bad65ccc8cce7c863e9c288b39cd8a2a49ba58fcde`.
- Pre-create audit found 46 canonical exact indexes, 1 equivalent legacy name, 1 missing critical index, 2 missing non-critical indexes, and 1 same-name incompatible critical index.
- `paper_update_runs_run_id_unique` preflight scanned 6 rows, found 0 duplicate groups and 0 malformed rows.
- Created and verified exact canonical indexes: `paper_update_runs_run_id_unique`, `pipeline_run_status_started_at`, `pipeline_run_status_operation_started`.
- Live indexes dropped: 0. Documents inserted/updated/deleted: 0 / 0 / 0.
- Post-bootstrap audit found 49 canonical exact indexes, 1 equivalent legacy name, and 1 critical blocker: `system_errors_dedup_identity` has actual `unique=false` but expected `unique=true`.
- Backend startup and `/health` were not run because the critical blocker remains; scheduler initialization and paper automation did not run during validation.
- Business document counts captured before/after the skipped startup-validation phase were unchanged.
- Tests: central-index 34 passed; focused bootstrap 14 passed; Wave 0A 56 passed; safe backend subset 377 passed; isolated TradingView regressions 40 passed.
- Normal project startup currently permitted: no.
- Evidence: `reviews/WAVE0D2_REMAINING_INDEX_BOOTSTRAP_STARTUP.md`.

## Wave 1B Provider Reliability Audit Status

- Status: CODE COMPLETE / BACKEND FULL-SUITE HAS UNRELATED BLOCKERS.
- Provider call paths audited: shared market data provider, NSE batch client, market load/test routes, scan route NSE provider path, NSE universe CSV path.
- Direct active provider bypass fixed: scan route now adapts through `nse_client` instead of owning a separate NSE HTTP implementation.
- NSE, yfinance, and NSE universe CSV paths now use bounded attempts, explicit timeouts where supported, deterministic backoff, rate-limit classification, and sanitized provider errors.
- yfinance batch fallback now caps worker threads and keeps partial failures as per-symbol sanitized errors.
- Symbol, price, non-negative volume/value, yfinance timestamp, and UTC timestamp normalization are covered by focused tests.
- Duplicate provider work reduced in `/api/market/test-symbol`; the endpoint now relies on one merged fetch instead of a second yfinance fallback call.
- Live providers, TradingView actions, and business-data mutations performed by Wave 1B: none.
- Tests: Wave 1B focused provider tests 8 passed; provider/scan/route/system-error safe subset 35 passed.
- Full backend test attempt: 444 passed, 2 failed. Failures were outside Wave 1B provider changes: capital migration apply now requires an approved plan, and TradingView preflight manager busy test hit cross-event-loop lock state.

## Wave 2A Config, Error Contract, and Startup Consistency Status

- Status: COMPLETE - CODE AND TESTS PASS.
- R004/R084/R116: runtime port/config values are centralized in `config.Settings`, typed env parsing is bounded, startup validates settings before Mongo connection, smoke mode reports automation disabled consistently, and mocked lifespan coverage proves smoke skips automation while normal startup preserves index, TradingView preference, and paper automation hooks.
- R120: system-error persistence redacts secret assignments, query credentials, URI credentials, auth headers, and local filesystem paths before dedup/persist.
- R121: FastAPI global handlers now return stable `{code,message,details}` error envelopes for HTTP, request validation, config, operator-intent, and unhandled errors; unhandled errors return generic client messages.
- Existing index, automation, smoke-mode, and TradingView controls preserved; no provider, TradingView, or business-data mutation performed by Wave 2A.
- Focused Wave 2A tests: 8 passed.
- Adjacent backend safety tests: 104 passed.
- Full backend suite: 456 passed.
- Frontend tests/build not run because Wave 2A changed no frontend files.

## Wave 2B Mutation Route Security Status

- Status: COMPLETE - CODE AND TESTS PASS.
- Mutating endpoints secured: `/api/paper/build-plans`, `/api/paper/run-pipeline`, `/api/paper/sync-trade-ready`, and `/api/paper/auto-update-outcomes` now default to dry-run (safe, no DB writes).
- Real mutations (where dry_run=false or save=true) are guarded by the authoritative operator-intent header checks.
- Bypassed lock acquisition and DB inserts/updates in `sync_trade_ready` and `run_automatic_outcome_update` when running in dry-run mode, ensuring zero MongoDB writes.
- Cleaned up unused/unwired frontend handlers and imports (`scan`, `swingSignals`, `swingPlans`, `momentumSignals`, `momentumPlans`) from `App.jsx` and `api.js`.
- Removed unwired client-only approval logic (`paperRealUpdateApproval.js` and its test file) from the codebase.
- Tests: Focused Wave 2B security tests (7/7 passed); full backend suite (463/463 passed); frontend test suite (36/36 passed); frontend Vite production build succeeded.

## Wave 6A Frontend Input and Request Reliability Status

- Status: COMPLETE - CODE AND TESTS PASS.
- R024/R056: Empty or invalid symbol inputs are rejected client-side before making backend API requests.
- R028/R029: Strategy and timeframe filters are validated, normalized, and consistently applied to the AI snapshot and outcome preview panels via frontend filters.
- R030: Manual AI dataset refresh is safe and preserves successful panels on partial failures by using Promise.allSettled and validating timeframe filters.
- R031: Swing summary clearly includes available saved-TV status counts when loaded, or documents why they are unavailable.
- R032/R043: Abort, overlap, and stale-response protection added to Swing and Momentum candidate loads, and stale rows are cleared on failure.
- R057/R062/R065: Freeform timeframe inputs are normalized and validated against backend supported resolutions.
- R122: Error display is sanitized using `sanitizeErrorMessage` to redact absolute paths, IP addresses, database details, and credentials. Request cancellations are cleanly intercepted and prevented from displaying.
- Tests: Frontend test suite (37/37 passed); frontend Vite production build succeeded; backend test suite (467/467 passed).
- Evidence: Wave 6A.
