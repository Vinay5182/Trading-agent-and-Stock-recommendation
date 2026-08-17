import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import app
from routes import scan


def matches(row: dict, query: dict | None) -> bool:
    if not query:
        return True
    return all(row.get(key) == value for key, value in query.items())


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.index = 0

    def sort(self, *_args):
        return self

    def limit(self, limit: int):
        self.rows = self.rows[:limit]
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self.index]
        self.index += 1
        return row


class FakeBulkResult:
    upserted_count = 1
    modified_count = 0


class FakeCollection:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.indexes = []
        self.bulk_writes = []
        self.update_calls = []

    async def create_index(self, fields, **kwargs):
        self.indexes.append((fields, kwargs))
        return kwargs.get("name", "index")

    async def bulk_write(self, operations, ordered: bool = False):
        self.bulk_writes.append((operations, ordered))
        for operation in operations:
            row = operation._doc.get("$set", {}).copy()
            self.rows.append(row)
        return FakeBulkResult()

    async def delete_many(self, query: dict | None = None):
        if query and "$ne" in query.get("scan_run_id", {}):
            ne_target = query["scan_run_id"]["$ne"]
            self.rows = [r for r in self.rows if r.get("scan_run_id") == ne_target]
        return SimpleNamespace(deleted_count=0)


    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        self.update_calls.append((query, update, upsert))
        row = next((candidate for candidate in self.rows if matches(candidate, query)), None)
        if row is None:
            row = {**query, **update.get("$setOnInsert", {})}
            self.rows.append(row)
        row.update(update.get("$set", {}))
        return SimpleNamespace(modified_count=1, matched_count=1, upserted_id=None)

    async def find_one(self, query: dict | None = None, projection: dict | None = None, sort: list | None = None):
        rows = [row for row in self.rows if matches(row, query)]
        if sort:
            key, direction = sort[0]
            rows = sorted(rows, key=lambda row: row.get(key) or "", reverse=direction < 0)
        if not rows:
            return None
        row = rows[0].copy()
        if projection and projection.get("_id") == 0:
            row.pop("_id", None)
        return row

    def find(self, query: dict | None = None, projection: dict | None = None):
        rows = [row.copy() for row in self.rows if matches(row, query)]
        if projection and projection.get("_id") == 0:
            rows = [{key: value for key, value in row.items() if key != "_id"} for row in rows]
        return FakeCursor(rows)


def fake_db() -> SimpleNamespace:
    return SimpleNamespace(
        swing_tv_confirmations=FakeCollection(),
        momentum_tv_confirmations=FakeCollection(),
        scan_runs=FakeCollection(),
        scan_rows=FakeCollection(),
        market_data=FakeCollection(),
        market_load_state=FakeCollection(),
        pipeline_run_locks=FakeCollection(),
        pipeline_run_status=FakeCollection(),
        paper_update_runs=FakeCollection(),
        scheduler_status=FakeCollection(),
        system_errors=FakeCollection(),
    )


def test_openapi_exposes_scan_contracts() -> None:
    paths = app.openapi()["paths"]

    assert "/api/scan" in paths
    assert "post" in paths["/api/scan"]
    assert "/api/scan/rows" in paths
    assert "get" in paths["/api/scan/rows"]


def test_run_scan_uses_real_quote_rows_and_persists_scan_rows(monkeypatch) -> None:
    async def run() -> None:
        db = fake_db()
        monkeypatch.setattr(scan, "get_database", lambda: db)
        monkeypatch.setattr(
            scan,
            "fetch_broad_market_nse_quotes",
            lambda: (
                {"TEST": {"ltp": 101.5, "day_high": 105.0, "day_low": 99.0}},
                {"source": "fake-nse"},
            ),
        )

        result = await scan.run_scan(
            scan.ScanRequest(selected_index="DEFAULT_UNIVERSE", limit=1, dry_run=False),
            operator_intent="operator-write-v1",
        )
        rows = await scan.get_scan_rows(result["scan_run_id"], limit=100)

        assert result["rows_count"] == 1
        assert result["rows"][0]["symbol"] == "TEST"
        assert result["rows"][0]["tradingview_symbol"] == "NSE:TEST"
        assert db.scan_rows.bulk_writes
        assert rows["count"] == 1
        assert rows["rows"][0]["scan_run_id"] == result["scan_run_id"]

    asyncio.run(run())


def test_sync_scan_rows_from_market_data() -> None:
    async def run() -> None:
        db = fake_db()
        market_rows = [
            {
                "symbol": "RELIANCE",
                "canonical_symbol": "RELIANCE",
                "exchange": "NSE",
                "current_price": 2500.0,
                "change_percent": 1.5,
                "day_high": 2520.0,
                "day_low": 2480.0,
                "traded_volume": 1000000,
                "traded_value": 2500000000.0,
                "source_used": "NSE_PRIMARY",
                "is_complete": True,
            },
            {
                "symbol": "TCS",
                "canonical_symbol": "TCS",
                "exchange": "NSE",
                "current_price": 3800.0,
                "change_percent": -0.5,
                "day_high": 3850.0,
                "day_low": 3780.0,
                "traded_volume": 500000,
                "traded_value": 1900000000.0,
                "source_used": "NSE_PRIMARY",
                "is_complete": True,
            },
        ]

        scan_run_id = await scan.sync_scan_rows_from_market_data(
            db,
            rows=market_rows,
            selected_index="BROAD_MARKET_750",
            force_refresh=False,
        )

        assert scan_run_id.startswith("scan-")
        assert len(db.scan_rows.rows) == 2
        symbols = [r["symbol"] for r in db.scan_rows.rows]
        assert "RELIANCE" in symbols
        assert "TCS" in symbols
        assert db.scan_rows.rows[0]["scan_run_id"] == scan_run_id

    asyncio.run(run())

