from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from main import app
from routes import paper
from security.operator_intent import OPERATOR_INTENT_HEADER, OPERATOR_INTENT_VALUE

OPERATOR_HEADERS = {OPERATOR_INTENT_HEADER: OPERATOR_INTENT_VALUE}

class FakePaperUpdateLocks:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []

    async def update_one(self, query: dict, update: dict, upsert: bool = False) -> SimpleNamespace:
        # Check if lock_name already exists in rows with status LOCKED
        lock_name = query.get("lock_name") or update.get("$set", {}).get("lock_name")
        held = any(item.get("lock_name") == lock_name and item.get("status") == "LOCKED" for item in self.rows)
        if held and not any(item.get("run_id") == query.get("run_id") for item in self.rows):
            return SimpleNamespace(modified_count=0, matched_count=0, upserted_id=None)

        row = next((item for item in self.rows if item.get("lock_name") == lock_name), None)
        if row is None:
            row = {"lock_name": lock_name}
            self.rows.append(row)
        row.update(update.get("$set", {}))
        row.update(update.get("$setOnInsert", {}))
        return SimpleNamespace(modified_count=1, matched_count=1, upserted_id=None)

    async def find_one(self, query: dict | None = None, projection: dict | None = None, **_kwargs) -> dict | None:
        lock_name = query.get("lock_name") if query else None
        row = next((item for item in self.rows if item.get("lock_name") == lock_name), None)
        return row

class FakeScanRuns:
    async def find_one(self, *args, **kwargs):
        return {"scan_run_id": "scan-123"}

class FakeScanRows:
    def find(self, *args, **kwargs):
        class Cursor:
            def limit(self, *args):
                return self
            def sort(self, *args):
                return self
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise StopAsyncIteration
        return Cursor()

def patch_fake_db(monkeypatch, locks: list[dict] | None = None):
    db = SimpleNamespace(
        paper_update_locks=FakePaperUpdateLocks(locks),
        scan_runs=FakeScanRuns(),
        scan_rows=FakeScanRows(),
        paper_trades=FakeScanRows(), # dummy
    )
    monkeypatch.setattr(paper, "get_database", lambda: db)
    return db

def test_dry_run_pipeline_does_not_acquire_lock(monkeypatch) -> None:
    db = patch_fake_db(monkeypatch)
    client = TestClient(app)
    response = client.post("/api/paper/run-pipeline?dry_run=true")
    assert response.status_code == 200
    assert len(db.paper_update_locks.rows) == 0

def test_real_run_pipeline_acquires_lock_and_blocks(monkeypatch) -> None:
    db = patch_fake_db(monkeypatch, [
        {
            "lock_name": paper.PAPER_UPDATE_LOCK_NAME,
            "status": "LOCKED",
            "run_id": "existing-run",
            "expires_at": "2999-01-01T00:00:00",
        }
    ])
    client = TestClient(app)
    client.headers.update(OPERATOR_HEADERS)
    response = client.post("/api/paper/run-pipeline?dry_run=false")
    assert response.status_code == 409
    assert response.json()["message"] == "LOCK_ALREADY_HELD"
