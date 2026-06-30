import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from main import app
from routes import paper
from security.operator_intent import OPERATOR_INTENT_HEADER, OPERATOR_INTENT_VALUE


OPERATOR_HEADERS = {OPERATOR_INTENT_HEADER: OPERATOR_INTENT_VALUE}


def trusted_client() -> TestClient:
    client = TestClient(app)
    client.headers.update(OPERATOR_HEADERS)
    return client


def make_trade(symbol: str, status: str = "WAITING_FOR_ENTRY") -> dict:
    return {
        "_id": f"{symbol.lower()}-id",
        "symbol": symbol,
        "tradingview_symbol": f"NSE:{symbol}",
        "timeframe": "1D",
        "source_signal_type": "SWING_TV_CONFIRMED",
        "paper_only": True,
        "status": status,
        "outcome_status": status,
        "entry_triggered": status not in {"NOT_TRIGGERED", "PLANNED", "WAITING", "WAITING_FOR_ENTRY"},
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "target_1": 120.0,
        "target_2": 130.0,
        "target_3": 140.0,
        "quantity": 10,
        "paper_pnl": 0.0,
        "updated_at": f"2026-06-07T00:00:00-{symbol}",
    }


def make_candle(high: float) -> dict:
    return {
        "time": 1_780_631_100,
        "open": 100.0,
        "high": high,
        "low": 95.0,
        "close": 101.0,
        "volume": 1_000,
    }


def make_market_row(symbol: str, *, high: float = 100.0, low: float = 95.0, close: float = 101.0) -> dict:
    return {
        "exchange": "NSE",
        "symbol": symbol,
        "canonical_symbol": symbol,
        "tradingview_symbol": f"NSE:{symbol}",
        "day_high": high,
        "day_low": low,
        "current_price": close,
        "updated_at": "2026-06-16T10:00:00",
    }


def make_snapshot_row(symbol: str, *, trade_id: str | None = None, high: float = 101.0, low: float = 101.0, close: float = 101.0) -> dict:
    return {
        "paper_only": True,
        "paper_trade_id": trade_id or f"{symbol.lower()}-id",
        "symbol": symbol,
        "canonical_symbol": symbol,
        "observed_at": "2026-06-16T10:00:00",
        "observed_at_iso": "2026-06-16T10:00:00",
        "high": high,
        "low": low,
        "close": close,
        "price": close,
        "source": "test_snapshot",
    }


def matches_query(row: dict, query: dict | None) -> bool:
    if not query:
        return True
    for key, expected in query.items():
        actual = row.get(key)
        if isinstance(expected, dict) and "$in" in expected:
            if actual not in expected["$in"]:
                return False
        elif actual != expected:
            return False
    return True


def project_row(row: dict, projection: dict | None) -> dict:
    if not projection:
        return row.copy()
    projected = row.copy()
    if any(value == 0 for value in projection.values()):
        for key, value in projection.items():
            if value == 0:
                projected.pop(key, None)
        return projected
    return {key: row.get(key) for key, value in projection.items() if value}


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.index = 0

    def sort(self, *args) -> "FakeCursor":
        if len(args) == 1 and isinstance(args[0], list):
            key, direction = args[0][0]
        elif len(args) >= 2:
            key, direction = args[0], args[1]
        else:
            return self
        self.rows = sorted(self.rows, key=lambda row: row.get(key) or "", reverse=direction < 0)
        return self

    def limit(self, limit: int) -> "FakeCursor":
        self.rows = self.rows[:limit]
        return self

    def __aiter__(self) -> "FakeCursor":
        return self

    async def __anext__(self) -> dict:
        if self.index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self.index]
        self.index += 1
        return row


class FakePaperTrades:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.update_calls = []
        self.delete_calls = []

    def find(self, query: dict | None = None, projection: dict | None = None, *_args, **_kwargs) -> FakeCursor:
        return FakeCursor([project_row(row, projection) for row in self.rows if matches_query(row, query)])

    async def update_one(self, *args, **kwargs) -> SimpleNamespace:
        self.update_calls.append((args, kwargs))
        return SimpleNamespace(modified_count=1)

    async def delete_one(self, *args, **kwargs) -> SimpleNamespace:
        self.delete_calls.append((args, kwargs))
        return SimpleNamespace(deleted_count=1)


class FakePaperUpdateRuns:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.update_calls = []

    async def update_one(self, query: dict, update: dict, upsert: bool = False) -> SimpleNamespace:
        self.update_calls.append((query, update, {"upsert": upsert}))
        row = next((item for item in self.rows if matches_query(item, query)), None)
        if row is None:
            if not upsert:
                return SimpleNamespace(modified_count=0)
            row = {}
            row.update(update.get("$setOnInsert", {}))
            self.rows.append(row)
        row.update(update.get("$set", {}))
        return SimpleNamespace(modified_count=1)

    async def find_one(self, query: dict | None = None, projection: dict | None = None, sort: list | None = None) -> dict | None:
        rows = [row for row in self.rows if matches_query(row, query)]
        if sort:
            key, direction = sort[0]
            rows = sorted(rows, key=lambda row: row.get(key) or "", reverse=direction < 0)
        if not rows:
            return None
        return project_row(rows[0], projection)

    def find(self, query: dict | None = None, projection: dict | None = None, *_args, **_kwargs) -> FakeCursor:
        return FakeCursor([project_row(row, projection) for row in self.rows if matches_query(row, query)])


class FakeMarketData:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def find_one(self, query: dict | None = None, *_args, **_kwargs) -> dict | None:
        row = next((row for row in self.rows if matches_query(row, query)), None)
        return row.copy() if row else None


class FakePaperMarketSnapshots:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def find(self, query: dict | None = None, *_args, **_kwargs) -> FakeCursor:
        return FakeCursor([row.copy() for row in self.rows if matches_query(row, query)])


class FakeTradingViewNoEntryClient:
    def connect_to_debug_port(self) -> bool:
        return True

    def open_symbol(self, _symbol: str) -> dict:
        return {}

    def fetch_candles(self, _timeframe: str, min_candles: int = 1) -> list[dict]:
        assert min_candles == 1
        return [make_candle(99.0)]


class FakeTradingViewEntryClient(FakeTradingViewNoEntryClient):
    def fetch_candles(self, _timeframe: str, min_candles: int = 1) -> list[dict]:
        assert min_candles == 1
        return [make_candle(100.0)]


class FakeTradingViewFailureClient:
    def connect_to_debug_port(self) -> bool:
        raise RuntimeError("simulated TradingView failure")


def patch_fake_db(monkeypatch, trades: list[dict], runs: list[dict] | None = None, market_rows: list[dict] | None = None, snapshots: list[dict] | None = None):
    effective_market_rows = market_rows if market_rows is not None else [make_market_row(row["symbol"]) for row in trades]
    if snapshots is None:
        snapshots = [
            make_snapshot_row(row["symbol"], trade_id=row["_id"], high=market.get("current_price", 101.0), low=market.get("current_price", 101.0), close=market.get("current_price", 101.0))
            for row, market in zip(trades, effective_market_rows)
        ]
    db = SimpleNamespace(
        paper_trades=FakePaperTrades(trades),
        paper_update_runs=FakePaperUpdateRuns(runs),
        market_data=FakeMarketData(effective_market_rows),
        paper_market_snapshots=FakePaperMarketSnapshots(snapshots),
    )
    monkeypatch.setattr(paper, "get_database", lambda: db)
    return db


def test_dry_run_creates_completed_paper_update_run_log(monkeypatch) -> None:
    db = patch_fake_db(monkeypatch, [make_trade("WAIT1")], market_rows=[make_market_row("WAIT1", high=99.0, close=98.0)])
    monkeypatch.setattr(paper, "TradingViewClient", FakeTradingViewNoEntryClient)
    client = TestClient(app)

    response = client.post("/api/paper/update-trades?dry_run=true&max_trades=6&max_writes=1")

    assert response.status_code == 200
    assert response.json()["updated_count"] == 0
    assert len(db.paper_update_runs.rows) == 1
    run = db.paper_update_runs.rows[0]
    assert run["status"] == "COMPLETED"
    assert run["mode"] == "DRY_RUN"
    assert run["dry_run"] is True
    assert run["paper_only"] is True
    assert run["live_trading"] is False
    assert run["broker_orders"] is False
    assert run["processed"] == 1
    assert run["proposed_write_count"] == 0
    assert run["updated_count"] == 0
    assert run["pre_snapshot"]["paper_trades_count"] == 1
    assert run["post_snapshot"]["paper_trades_count"] == 1
    assert len(run["per_trade_results"]) == 1
    assert run["approval_status"] == "AVAILABLE"
    assert run["approval_expires_at"]
    assert run["approval_used_at"] is None
    assert run["approval_used_by_run_id"] is None
    assert run["approved_real_run_id"] is None
    assert run["pre_snapshot_hash"] == run["pre_snapshot"]["snapshot_hash"]
    assert run["proposed_trade_ids"] == []
    assert run["proposed_transition_hash"]
    assert run["approved_max_trades"] == 6
    assert run["approved_max_writes"] == 1
    assert run["target_trade_precondition_hashes"] == {}


def test_dry_run_stores_exact_approvable_transition_metadata(monkeypatch) -> None:
    db = patch_fake_db(monkeypatch, [make_trade("WAIT1")])
    monkeypatch.setattr(paper, "TradingViewClient", FakeTradingViewEntryClient)
    client = TestClient(app)

    response = client.post("/api/paper/update-trades?dry_run=true&max_trades=6&max_writes=1")

    assert response.status_code == 200
    run = db.paper_update_runs.rows[0]
    proposal = run["per_trade_results"][0]
    assert run["approval_status"] == "AVAILABLE"
    assert run["proposed_trade_ids"] == ["wait1-id"]
    assert run["target_trade_precondition_hashes"]["wait1-id"]
    assert proposal["proposed_update"]["status"] == "ACTIVE"
    assert proposal["target_trade_precondition_hash"] == run["target_trade_precondition_hashes"]["wait1-id"]
    assert run["proposed_transition_hash"] == paper.proposed_transition_hash(run["per_trade_results"])


def test_unbound_real_update_creates_approval_required_run_log_without_writes(monkeypatch) -> None:
    db = patch_fake_db(monkeypatch, [make_trade("WAIT1"), make_trade("WAIT2")])
    monkeypatch.setattr(paper, "TradingViewClient", FakeTradingViewEntryClient)
    client = trusted_client()

    response = client.post("/api/paper/update-trades?dry_run=false&max_trades=6&max_writes=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["blocked"] is True
    assert payload["block_reason"] == "APPROVAL_REQUIRED"
    run = db.paper_update_runs.rows[0]
    assert run["status"] == "BLOCKED"
    assert run["mode"] == "REAL"
    assert run["blocked"] is True
    assert run["block_reason"] == "APPROVAL_REQUIRED"
    assert run["proposed_write_count"] == 0
    assert run["updated_count"] == 0
    assert db.paper_trades.update_calls == []
    assert db.paper_trades.delete_calls == []


def test_missing_market_data_creates_completed_run_log_without_transition(monkeypatch) -> None:
    db = patch_fake_db(monkeypatch, [make_trade("WAIT1")], market_rows=[])
    client = TestClient(app)

    response = client.post("/api/paper/update-trades?dry_run=true&max_trades=6&max_writes=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["errors_count"] == 0
    assert payload["proposed_write_count"] == 0
    run = db.paper_update_runs.rows[0]
    assert run["status"] == "COMPLETED"
    assert run["errors_count"] == 0
    assert run["per_trade_results"][0]["proposed_reason"] == "DATA_INSUFFICIENT"
    assert run["approval_status"] == "AVAILABLE"
    assert db.paper_trades.update_calls == []


def test_update_progress_returns_latest_run_from_fake_collection(monkeypatch) -> None:
    patch_fake_db(
        monkeypatch,
        [],
        [
            {"run_id": "older", "started_at": "2026-06-07T01:00:00", "status": "COMPLETED", "dry_run": True},
            {
                "run_id": "newer",
                "started_at": "2026-06-07T02:00:00",
                "status": "BLOCKED",
                "dry_run": True,
                "processed": 2,
                "proposed_write_count": 2,
                "updated_count": 0,
                "would_update_count": 2,
                "successful_updates_count": 0,
                "errors_count": 0,
                "blocked": True,
                "block_reason": "MAX_WRITES_EXCEEDED",
                "max_trades": 6,
                "max_writes": 1,
                "changed_trade_ids": [],
                "paper_only": True,
                "live_trading": False,
                "broker_orders": False,
            },
        ],
    )
    client = TestClient(app)

    response = client.get("/api/paper/update-progress")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == "newer"
    assert payload["status"] == "BLOCKED"
    assert payload["blocked"] is True
    assert payload["paper_only"] is True
    assert payload["live_trading"] is False
    assert payload["broker_orders"] is False


def test_update_runs_returns_newest_runs_first(monkeypatch) -> None:
    patch_fake_db(
        monkeypatch,
        [],
        [
            {"run_id": "old", "started_at": "2026-06-07T01:00:00", "status": "COMPLETED"},
            {"run_id": "new", "started_at": "2026-06-07T03:00:00", "status": "COMPLETED"},
            {"run_id": "middle", "started_at": "2026-06-07T02:00:00", "status": "COMPLETED"},
        ],
    )
    client = TestClient(app)

    response = client.get("/api/paper/update-runs?limit=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert [run["run_id"] for run in payload["runs"]] == ["new", "middle"]


def test_update_run_by_id_returns_selected_run(monkeypatch) -> None:
    patch_fake_db(
        monkeypatch,
        [],
        [
            {"run_id": "selected", "started_at": "2026-06-07T01:00:00", "status": "COMPLETED"},
            {"run_id": "other", "started_at": "2026-06-07T02:00:00", "status": "BLOCKED"},
        ],
    )
    client = TestClient(app)

    response = client.get("/api/paper/update-runs/selected")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == "selected"
    assert payload["status"] == "COMPLETED"
