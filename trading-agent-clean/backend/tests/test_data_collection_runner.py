import pytest
from unittest.mock import MagicMock, AsyncMock
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.historical_data_collection_runner import run_data_collection_batch

import asyncio

def test_data_collection_runner_dry_run_no_writes():
    asyncio.run(_test_data_collection_runner_dry_run_no_writes())

async def _test_data_collection_runner_dry_run_no_writes():
    db = MagicMock()
    # Ensure dry_run_historical_candidate_generation returns something
    # We mock dry_run_historical_candidate_generation and build_daily_dataset_candidate_snapshot_run inside the module?
    # Actually, we can just run the function and assert it doesn't call insert_one on dataset_build_runs
    db["dataset_build_runs"].insert_one = AsyncMock()
    
    # We need to mock dry_run_historical_candidate_generation to avoid actual DB calls
    # Or just mock the collections
    db["historical_ohlcv"].aggregate = MagicMock(return_value=AsyncMock(to_list=AsyncMock(return_value=[])))
    db["historical_ohlcv"].find = MagicMock(return_value=AsyncMock(sort=MagicMock(return_value=AsyncMock(to_list=AsyncMock(return_value=[])))))
    
    # Run the function
    manifest = await run_data_collection_batch(
        db,
        date_from="2026-03-01",
        date_to="2026-03-10",
        max_symbols=10,
        chunk_days=30,
        dry_run=True,
        persist_historical_candidates=True,
        bridge_to_daily_dataset=True
    )
    
    assert manifest["dry_run"] is True
    assert manifest["historical_candidates_inserted"] == 0
    assert manifest["daily_dataset_inserted"] == 0
    db["dataset_build_runs"].insert_one.assert_not_called()
    db["historical_scored_candidates"].bulk_write.assert_not_called()
    
