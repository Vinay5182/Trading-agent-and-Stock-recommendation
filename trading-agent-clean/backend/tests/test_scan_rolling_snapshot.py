import sys
import os
from typing import Any
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from routes import scan




@pytest.fixture
def anyio_backend():
    return 'asyncio'


class FakeDeleteResult:
    def __init__(self, deleted_count: int):
        self.deleted_count = deleted_count


class FakeBulkResult:
    def __init__(self, upserted_count: int = 1, modified_count: int = 0):
        self.upserted_count = upserted_count
        self.modified_count = modified_count


class FakeScanRowsCollection:
    def __init__(self, initial_rows: list[dict[str, Any]] | None = None):
        self.rows: list[dict[str, Any]] = list(initial_rows or [])
        self.bulk_writes: list[Any] = []
        self.delete_calls: list[dict[str, Any]] = []

    async def bulk_write(self, operations: list[Any], ordered: bool = False) -> FakeBulkResult:
        self.bulk_writes.append(operations)
        for op in operations:
            doc = dict(op._doc.get("$set", {}))

            if "$setOnInsert" in op._doc:
                doc.update(op._doc["$setOnInsert"])

            spec = op._filter
            existing_idx = next(
                (i for i, r in enumerate(self.rows) if r.get("scan_run_id") == spec.get("scan_run_id") and r.get("symbol") == spec.get("symbol")),
                None,
            )
            if existing_idx is not None:
                self.rows[existing_idx].update(doc)
            else:
                self.rows.append(doc)
        return FakeBulkResult(upserted_count=len(operations))

    async def delete_many(self, query: dict[str, Any]) -> FakeDeleteResult:
        self.delete_calls.append(query)
        ne_target = query.get("scan_run_id", {}).get("$ne")
        if ne_target:
            initial_count = len(self.rows)
            self.rows = [r for r in self.rows if r.get("scan_run_id") == ne_target]
            deleted_count = initial_count - len(self.rows)
            return FakeDeleteResult(deleted_count=deleted_count)
        return FakeDeleteResult(deleted_count=0)

    def find(self, query: dict[str, Any], projection: dict[str, Any] | None = None):
        target_run_id = query.get("scan_run_id")
        matching = [r for r in self.rows if not target_run_id or r.get("scan_run_id") == target_run_id]

        class AsyncCursor:
            def __init__(self, data):
                self.data = data

            def sort(self, *args, **kwargs):
                return self

            def limit(self, limit_num):
                self.data = self.data[:limit_num]
                return self

            def __aiter__(self):
                self._iter = iter(self.data)
                return self

            async def __anext__(self):
                try:
                    return next(self._iter)
                except StopIteration:
                    raise StopAsyncIteration

        return AsyncCursor(matching)


class FakeScanRunsCollection:
    def __init__(self):
        self.runs: list[dict[str, Any]] = []

    async def update_one(self, filter_spec: dict[str, Any], update_spec: dict[str, Any], upsert: bool = True):
        doc = dict(update_spec.get("$set", {}))
        if "$setOnInsert" in update_spec:
            doc.update(update_spec["$setOnInsert"])
        self.runs.append(doc)
        return None

    async def find_one(self, filter_spec: dict[str, Any], projection: dict[str, Any] | None = None, sort=None):
        if not self.runs:
            return None
        return self.runs[-1]


class FakeDatabase:
    def __init__(self, scan_rows: FakeScanRowsCollection):
        self.scan_rows = scan_rows
        self.scan_runs = FakeScanRunsCollection()
        self.market_context_daily = FakeScanRunsCollection()
        self.sector_context_daily = FakeScanRunsCollection()


async def fake_ensure_scan_indexes(_db):
    return {}


@pytest.mark.anyio
async def test_rolling_snapshot_prunes_historical_scan_rows(monkeypatch) -> None:
    initial_rows = [
        {"scan_run_id": "old-run-1", "symbol": "OLD1", "created_at": "2026-05-28T00:00:00"},
        {"scan_run_id": "old-run-1", "symbol": "OLD2", "created_at": "2026-05-28T00:00:00"},
    ]
    fake_rows_coll = FakeScanRowsCollection(initial_rows=initial_rows)
    fake_db = FakeDatabase(fake_rows_coll)

    monkeypatch.setattr(scan, "get_database", lambda: fake_db)
    monkeypatch.setattr(scan, "ensure_scan_indexes", fake_ensure_scan_indexes)
    monkeypatch.setattr(
        scan,
        "fetch_broad_market_nse_quotes",
        lambda: (
            {"NEW1": {"current_price": 100.0, "ltp": 100.0}, "NEW2": {"current_price": 200.0, "ltp": 200.0}},
            {"ok": True},
        ),
    )
    monkeypatch.setattr(scan, "_refresh_market_context", lambda db, rows, now: None)
    monkeypatch.setattr(scan, "require_operator_intent_value", lambda header: None)

    # Execute fresh scan
    res = await scan._run_scan_real(fake_db, scan.ScanRequest(selected_index="BROAD_MARKET_750", limit=10), "BROAD_MARKET_750")

    new_run_id = res["scan_run_id"]

    # Verify delete_many was called for scan_run_id != new_run_id
    assert len(fake_rows_coll.delete_calls) == 1
    assert fake_rows_coll.delete_calls[0] == {"scan_run_id": {"$ne": new_run_id}}

    # Verify only new run rows remain in collection
    distinct_run_ids = set(r["scan_run_id"] for r in fake_rows_coll.rows)
    assert distinct_run_ids == {new_run_id}
    assert len(fake_rows_coll.rows) == 2


@pytest.mark.anyio
async def test_failed_scan_preserves_previous_snapshot(monkeypatch) -> None:
    initial_rows = [
        {"scan_run_id": "old-run-1", "symbol": "OLD1", "created_at": "2026-05-28T00:00:00"},
    ]
    fake_rows_coll = FakeScanRowsCollection(initial_rows=initial_rows)
    fake_db = FakeDatabase(fake_rows_coll)

    def failing_fetch():
        raise RuntimeError("NSE Network Outage")

    monkeypatch.setattr(scan, "get_database", lambda: fake_db)
    monkeypatch.setattr(scan, "ensure_scan_indexes", fake_ensure_scan_indexes)
    monkeypatch.setattr(scan, "fetch_broad_market_nse_quotes", failing_fetch)

    with pytest.raises(RuntimeError, match="NSE Network Outage"):
        await scan._run_scan_real(fake_db, scan.ScanRequest(selected_index="BROAD_MARKET_750"), "BROAD_MARKET_750")

    # Verify delete_many was NEVER called
    assert len(fake_rows_coll.delete_calls) == 0

    # Verify historical rows are preserved
    assert len(fake_rows_coll.rows) == 1
    assert fake_rows_coll.rows[0]["scan_run_id"] == "old-run-1"
