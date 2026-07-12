# Trading Agent v1.0 - Data Collection Runbook

## 1. Starting the Backend
To start the FastAPI backend if needed, open Windows PowerShell and run:
```powershell
cd C:\Users\Asus\OneDrive\Documents\Trading_Strategy\trading-agent-clean\backend
.\.venv\Scripts\python.exe -m uvicorn main:app --port 8011
```

## 2. Running Data Collection Tests
Run the following test suite to verify pipeline integrity before or after a run:
```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_data_collection_runner.py -v
.\.venv\Scripts\python.exe -m pytest backend/tests/test_historical_candidate_generator.py -v
.\.venv\Scripts\python.exe -m pytest backend/tests/test_daily_trade_dataset_base.py -v
.\.venv\Scripts\python.exe -m pytest backend/tests/test_daily_trade_dataset_api.py -v
.\.venv\Scripts\python.exe -m compileall backend
```

## 3. Checking Data Collection Status
Monitor collection counts, run status, and duplicate health using the status endpoint:
```http
GET http://127.0.0.1:8011/api/ai/data-collection/status
```

## 4. Safe Future Data Collection Process
If a new data backfill is required, follow these steps strictly:
1. **Always run `dry_run=true` first**: Preview the candidate counts and catch validation errors.
2. **Verify duplicates = 0**: Ensure no duplications would be created.
3. **Verify live `scored_candidates` unchanged**: Make sure the live collections are perfectly segregated.
4. **Run `dry_run=false`**: Execute the persistent batch.
5. **Verify labels remain `PENDING`**: Ensure ML labels were not prematurely assigned.

## 5. Emergency Rollback Guidance
- **Do not delete manually** unless a backup exists. Deletions break idempotency.
- **Prefer idempotent rerun**: The pipeline is designed to safely UPSERT over existing data. Rerunning with the same configuration is safe and self-healing.
- **Check `dataset_build_runs`**: Review the manifest of the failed run.
- **Use Mongo Backup**: If the database was corrupted, use the managed MongoDB Atlas automated snapshot restoration.
