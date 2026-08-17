# Architecture Decision Records (ADRs)

## ADR-001: Paper Trading Only Execution Lock
- **Status**: Accepted / Active
- **Context**: The system evaluates technical trading strategies in real market conditions without financial risk.
- **Decision**: `PAPER_MODE=True` and `LIVE_TRADING_ENABLED=False` are hardcoded in `backend/config.py`. No live broker ordering APIs (e.g. Zerodha Kite Connect) are imported or invoked.
- **Consequences**: Zero risk of unintended real capital execution.

## ADR-002: Chrome DevTools Protocol (CDP) WebSocket Lock Manager
- **Status**: Accepted / Active
- **Context**: TradingView has no official public chart API. The system scrapes DOM and JS data via Google Chrome CDP on port `9222`.
- **Decision**: All CDP commands are synchronized through `backend/services/tradingview_manager.py` using `LoopSafeAsyncLock` (`tradingview_manager.run_sync`).
- **Consequences**: Prevents race conditions and WebSocket frame corruption when concurrent REST API requests attempt chart navigation.

## ADR-003: Non-Lossy Rejection Engine Semantics
- **Status**: Accepted / Active
- **Context**: Setups that fail multi-timeframe alignment or risk checks must not lose technical diagnostic data.
- **Decision**: Downstream setup rejections synchronize `tv_status` to `REJECTED`, while preserving exact underlying cause codes in `avoid_reason` and `paper_plan_reason`.
- **Consequences**: Prevents invalid setups from entering paper trading while retaining 100% of diagnostic data for ML feature analysis.

## ADR-004: Operator Intent Header Security Policy
- **Status**: Accepted / Active
- **Context**: REST API endpoints exposed locally on port `8011` must be protected against unintentional state mutations.
- **Decision**: All mutating HTTP endpoints (`POST`, `PUT`, `DELETE`) enforce header verification `X-Trading-Agent-Intent: operator-write-v1` via `backend/security/operator_intent.py`.
- **Consequences**: Protects against accidental cross-origin browser triggers (CSRF) and ensures explicit intent on database writes.

## ADR-005: Declarative Centralized Database Indexing
- **Status**: Accepted / Active
- **Context**: MongoDB collections require compound unique indexes on `setup_identity`, `symbol`, and `timestamp`.
- **Decision**: Index definitions are centralized in `backend/services/mongo_indexes.py` and initialized during FastAPI startup (`lifespan` in `database.py`).
- **Consequences**: Eliminates ad-hoc index creation across route files and guarantees schema constraint enforcement on server startup.
