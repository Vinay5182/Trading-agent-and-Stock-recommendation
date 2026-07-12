# Dataset Collection & ML Training Runbook

This runbook outlines how to safely collect data into the `daily_trade_dataset`, monitor health, and determine when it is appropriate to begin ML training.

## 1. Safe Daily Collection Process
The system is designed to collect data autonomously via the following daily flow:
- **Build Candidates**: Nightly (or per market open), trigger the `build_daily_dataset_candidate_snapshot_run` service (this can be wired into a cron route).
- **Dry-Run First**: Always execute with `dry_run=True` to safely preview the proposed rows and view the exact counts.
- **Commit Build**: Re-execute with `dry_run=False` to securely persist the generated dataset IDs and initial `score_snapshot`.
- **Collect Confirmations**: As TradingView alerts arrive throughout the day, the routes automatically enrich the respective dataset rows.
- **Sync Lifecycles & Outcomes**: Background sync operations (or targeted label tasks) routinely map paper outcomes and historical OHLCV results back into the dataset.

## 2. Safe Backfill Process
To bootstrap the dataset without waiting months for live forward collection, historical backfill can be executed:
- **Prerequisites**: Ensure `historical_ohlcv` collections and historical scored candidates are populated.
- **Dry-Run Mode**: Use `preview_daily_dataset_from_scored_candidates` or run the builder helper in `dry_run=True` for historical dates (e.g. past 1 year).
- **Approved Backfill**: Run the builder progressively on targeted date chunks to avoid overloading memory. Monitor database indexes and CPU.
- **Apply Labels**: Execute the `update_daily_dataset_outcome_from_historical_ohlcv` routine across the newly built rows to apply simulated OHLCV labels.

## 3. Recommended Training Thresholds
Before considering model training, verify the following thresholds are met:
- **Minimum Total Rows**: ~2,500 – 5,000 completed observations minimum.
- **Minimum `READY` Labels**: 80% of rows should be in a `READY` state.
- **Minimum Labels per Class**: At least 300 - 500 samples per critical class (e.g., Target Hit, SL Hit, Rejected).
- **Minimum Market Days**: Data should span a minimum of 6 - 12 months to encompass varying market conditions (bull, bear, chop).
- **Strategy Diversity**: Recommend at least 1,000 Swing rows and 1,000 Momentum rows if training a shared model, or stratify strictly by strategy.
- **Class Imbalance Warning**: If over 80% of data is skewed to one label category, you must use undersampling/oversampling or class weights during ML pipeline configuration to prevent bias.

## 4. Commands to Verify Dataset Health
These endpoints are strictly read-only and designed for operator monitoring.

### Check Overall Counts & Class Balance
```bash
curl -s http://localhost:8011/api/ai/daily-dataset/summary
```
*Look for balanced representation across `action`, `tv_status`, and `label_category`.*

### Verify Exact Row Formatting (No Huge Docs)
```bash
curl -s http://localhost:8011/api/ai/daily-dataset/rows?limit=5
```
*Ensure rows return cleanly with deterministic `_id`.*

### Verify Export & Leakage Safety Status
```bash
curl -s http://localhost:8011/api/ai/daily-dataset/export-readiness
```
*Ensure `leakage_safe_export_ready` is `true`. Check `pending` and `missing` counts.*

### Verify Leakage-Safe Export Projection
```bash
curl -s http://localhost:8011/api/ai/daily-dataset/export-preview?limit=1
```
*Verify that `future_outcome` and `lifecycle_snapshot` are entirely absent from the response payload.*

---

**FINAL VERDICT**: DO NOT train the ML model today. Use the runbook strictly for data accumulation until statistical thresholds are comfortably reached.
