import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import app
from services.daily_dataset import (
    DAILY_TRADE_DATASET_COLLECTION,
    DATASET_BUILD_RUNS_COLLECTION,
)

# Use test client
client = TestClient(app)

@pytest.fixture
def mock_db():
    with patch("routes.ai.get_database") as mock_get_db:
        db = MagicMock()
        mock_get_db.return_value = db
        yield db

def test_daily_dataset_summary_endpoint(mock_db):
    # Mock aggregation result
    mock_collection = MagicMock()
    setattr(mock_db, DAILY_TRADE_DATASET_COLLECTION, mock_collection)
    
    class MockCursor:
        def __init__(self, data):
            self.data = data
        def to_list(self, length):
            import asyncio
            f = asyncio.Future()
            f.set_result(self.data)
            return f

    mock_collection.aggregate.return_value = MockCursor([{
        "total": [{"count": 100}],
        "trade_date": [{"_id": "2024-01-01", "count": 50}],
        "strategy_type": [{"_id": "SWING", "count": 100}],
        "current_stage": [{"_id": "SCORE_SNAPSHOT", "count": 100}],
        "action": [{"_id": "TAKE_TRADE", "count": 40}],
        "tv_status": [],
        "lifecycle_status": [],
        "label_state": [{"_id": "READY", "count": 10}],
        "label_category": [],
        "outcome_state": []
    }])

    response = client.get("/api/ai/daily-dataset/summary")
    assert response.status_code == 200
    data = response.json()
    assert data["total_rows"] == 100
    assert data["trade_date"]["2024-01-01"] == 50
    assert data["strategy_type"]["SWING"] == 100
    assert data["current_stage"]["SCORE_SNAPSHOT"] == 100
    assert data["label_state"]["READY"] == 10

def test_daily_dataset_rows_endpoint_filters(mock_db):
    mock_collection = MagicMock()
    setattr(mock_db, DAILY_TRADE_DATASET_COLLECTION, mock_collection)

    class MockCursor:
        def sort(self, *args, **kwargs): return self
        def skip(self, *args, **kwargs): return self
        def to_list(self, length):
            import asyncio
            f = asyncio.Future()
            f.set_result([{"_id": "1", "identity": {"trade_date": "2024-01-01"}}])
            return f

    mock_collection.find.return_value = MockCursor()

    response = client.get("/api/ai/daily-dataset/rows?trade_date_from=2024-01-01&strategy_type=SWING&limit=10")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    
    # Verify find was called with correct filter args
    call_args = mock_collection.find.call_args[0]
    query = call_args[0]
    assert query["identity.trade_date"]["$gte"] == "2024-01-01"
    assert query["identity.strategy_type"] == "SWING"

def test_daily_dataset_rows_endpoint_limit_cap(mock_db):
    response = client.get("/api/ai/daily-dataset/rows?limit=1000")
    assert response.status_code == 422 # Unprocessable entity due to limit constraint (le=500)

def test_daily_dataset_export_readiness_endpoint(mock_db):
    mock_collection = MagicMock()
    setattr(mock_db, DAILY_TRADE_DATASET_COLLECTION, mock_collection)
    
    class MockCursor:
        def __init__(self, data):
            self.data = data
        def to_list(self, length):
            import asyncio
            f = asyncio.Future()
            f.set_result(self.data)
            return f

    mock_collection.aggregate.return_value = MockCursor([{
        "total": [{"count": 100}],
        "label_state": [{"_id": "READY", "count": 100}],
        "label_category": [{"_id": "label_went_up", "count": 100}]
    }])

    response = client.get("/api/ai/daily-dataset/export-readiness")
    assert response.status_code == 200
    data = response.json()
    assert data["total_rows"] == 100
    assert data["label_ready_count"] == 100
    assert data["label_pending_count"] == 0
    assert data["missing_label_count"] == 0
    assert data["leakage_safe_export_ready"] is True

def test_daily_dataset_build_runs_endpoint_empty(mock_db):
    mock_collection = MagicMock()
    setattr(mock_db, DATASET_BUILD_RUNS_COLLECTION, mock_collection)
    
    async def mock_count(*args, **kwargs):
        return 0
    mock_collection.count_documents = mock_count
    
    class MockCursor:
        def sort(self, *args, **kwargs): return self
        def to_list(self, length):
            import asyncio
            f = asyncio.Future()
            f.set_result([])
            return f
            
    mock_collection.find.return_value = MockCursor()

    response = client.get("/api/ai/daily-dataset/build-runs")
    assert response.status_code == 200
    data = response.json()
    assert data["total_count"] == 0
    assert data["runs"] == []

def test_daily_dataset_export_preview_excludes_leakage(mock_db):
    mock_collection = MagicMock()
    setattr(mock_db, DAILY_TRADE_DATASET_COLLECTION, mock_collection)

    class MockCursor:
        def sort(self, *args, **kwargs): return self
        def to_list(self, length):
            import asyncio
            f = asyncio.Future()
            f.set_result([{
                "_id": "1",
                "identity": {"trade_date": "2024-01-01"},
                "future_outcome": {"this_should_be_stripped": True},
                "ml_label": {"label_category": "WIN", "label_state": "READY"}
            }])
            return f

    mock_collection.find.return_value = MockCursor()

    response = client.get("/api/ai/daily-dataset/export-preview")
    assert response.status_code == 200
    data = response.json()
    
    call_args = mock_collection.find.call_args[0]
    projection = call_args[1]
    
    # Assert projection only allows safe fields
    assert "future_outcome" not in projection
    assert "lifecycle_snapshot" not in projection
    assert "ml_label.label_category" in projection
    assert "ml_label.label_state" in projection
    assert "identity" in projection
