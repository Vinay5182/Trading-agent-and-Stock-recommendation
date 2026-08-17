# Feature History & Implementation Log

## Phase 1: Core Scraper & Stateless Scoring Engine
- **Features**: Market data collection (`data_provider.py`, `nse_client.py`), Swing & Momentum scoring math (`scoring.py`).
- **Files**: `backend/data_provider.py`, `backend/scoring.py`, `backend/routes/scan.py`, `backend/routes/score.py`.
- **Collections**: `market_data`, `scored_candidates`.

## Phase 2: TradingView Browser CDP Automation
- **Features**: Chrome DevTools Protocol client (`tv_client.py`), MTF candle gap validation (`tv_confirmation.py`), thread lock manager (`tradingview_manager.py`).
- **Files**: `backend/tv_client.py`, `backend/tv_confirmation.py`, `backend/services/tradingview_manager.py`, `backend/routes/tv.py`.
- **Collections**: `swing_tv_confirmations`, `momentum_tv_confirmations`.

## Phase 3: Paper Trading Lifecycle Engine
- **Features**: Virtual trade state machine (`WAITING_FOR_ENTRY` -> `ACTIVE` -> `T1_HIT` / `SL_HIT`), risk-based position sizing (`position_sizing.py`), capital reservation (`capital_accounting.py`), trade journal (`trade_journal.py`).
- **Files**: `backend/services/paper_sync.py`, `backend/services/paper_automation.py`, `backend/services/position_sizing.py`, `backend/services/trade_journal.py`.
- **Collections**: `paper_trades`, `paper_signals`, `trade_journal`.

## Phase 4: AI/ML Feature Snapshot Engine & Security Hardening
- **Features**: 45-feature technical snapshot builder with temporal leakage guards (`features.py`), operator intent header security (`operator_intent.py`), central index registry (`mongo_indexes.py`), test suite expansion (467 tests).
- **Files**: `backend/ai/features.py`, `backend/security/operator_intent.py`, `backend/services/mongo_indexes.py`.
- **Collections**: `ai_feature_snapshots`, `candidate_trade_outcomes`, `system_errors`.
