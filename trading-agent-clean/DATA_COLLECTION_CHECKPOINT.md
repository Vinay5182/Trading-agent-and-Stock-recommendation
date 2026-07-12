# Trading Agent v1.0 - Data Collection Checkpoint

## 1. Current Final Counts
- **historical_ohlcv**: 64142
- **historical_scored_candidates**: 5508
- **daily_trade_dataset**: 6490
- **live scored_candidates**: 750

## 2. Duplicate Status
- **duplicate historical_candidate_id**: 0
- **duplicate identity.dataset_id**: 0

## 3. Final Row States
- `identity.current_stage`: SCORE_SNAPSHOT only
- `ml_label.label_state`: PENDING only
- `future_outcome.outcome_state`: NOT_READY only
- `lifecycle_snapshot.lifecycle_status`: NOT_STARTED only
- `tv_confirmation_snapshot.status`: PENDING only
- `decision_snapshot.action`: PENDING only

## 4. Collections Used
- `historical_ohlcv`
- `historical_scored_candidates`
- `daily_trade_dataset`
- `dataset_build_runs`

## 5. Collections Intentionally Untouched
- `scored_candidates` (live cache)
- `paper_trades`
- `trade_journal`

## 6. Files/Modules Implemented
- `backend/ai/historical_candidate_generator.py`
- `backend/ai/historical_data_collection_runner.py`
- `backend/services/daily_dataset.py`
- `backend/routes/ai.py`
- `backend/services/mongo_indexes.py`
- Related Pytest tests

## 7. What is Complete
- Historical OHLCV mapped to historical candidates
- Historical candidates bridged to daily dataset
- Deterministic symbol selection
- Idempotent upserts for safe re-runs
- Read-only status endpoints for reporting

## 8. What is Intentionally NOT Complete
- Outcome labeling
- ML training
- Neural network integration
- Prediction API logic

## 9. Next Future Phase (NOT STARTED)
- Outcome label generation (Future task)
- Export readiness logic (Future task)
- Baseline ML training (Future task)
