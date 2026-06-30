import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from pymongo.errors import DuplicateKeyError

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


def make_trade(symbol: str, status: str = "NOT_TRIGGERED") -> dict:
    return {
        "_id": f"{symbol.lower()}-id",
        "symbol": symbol,
        "tradingview_symbol": f"NSE:{symbol}",
        "timeframe": "1D",
        "source_signal_type": "SWING_TV_CONFIRMED",
        "paper_only": True,
        "status": status,
        "outcome_status": status,
        "entry_triggered": status not in {"NOT_TRIGGERED", "PLANNED"},
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


def matches_query(row: dict, query: dict | None) -> bool:
    if not query:
        return True
    for key, expected in query.items():
        if key == "$or":
            if not any(matches_query(row, item) for item in expected):
                return False
            continue
        actual = row.get(key)
        if isinstance(expected, dict):
            for operator, value in expected.items():
                if operator == "$in" and actual not in value:
                    return False
                if operator == "$lte" and str(actual) > str(value):
                    return False
                if operator == "$exists" and (key in row) is not bool(value):
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

    async def update_one(self, query: dict, update: dict, **_kwargs) -> SimpleNamespace:
        self.update_calls.append((query, update))
        row = next((item for item in self.rows if matches_query(item, query)), None)
        if row is None:
            return SimpleNamespace(modified_count=0)
        row.update(update.get("$set", {}))
        return SimpleNamespace(modified_count=1)

    async def delete_one(self, query: dict, **_kwargs) -> SimpleNamespace:
        self.delete_calls.append(query)
        before = len(self.rows)
        self.rows = [row for row in self.rows if not matches_query(row, query)]
        return SimpleNamespace(deleted_count=before - len(self.rows))


class FakePaperUpdateRuns:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []

    async def update_one(self, query: dict, update: dict, upsert: bool = False) -> SimpleNamespace:
        row = next((item for item in self.rows if matches_query(item, query)), None)
        if row is None:
            if not upsert:
                return SimpleNamespace(modified_count=0)
            row = {}
            row.update(update.get("$setOnInsert", {}))
            self.rows.append(row)
        row.update(update.get("$set", {}))
        return SimpleNamespace(modified_count=1, matched_count=1, upserted_id=None)

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


class FakePaperUpdateLocks:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.indexes = []

    async def create_index(self, *args, **kwargs) -> str:
        self.indexes.append((args, kwargs))
        return "lock_name_1"

    async def update_one(self, query: dict, update: dict, upsert: bool = False) -> SimpleNamespace:
        row = next((item for item in self.rows if matches_query(item, query)), None)
        if row is None:
            if not upsert:
                return SimpleNamespace(modified_count=0, matched_count=0, upserted_id=None)
            lock_name = update.get("$set", {}).get("lock_name") or query.get("lock_name")
            if any(item.get("lock_name") == lock_name for item in self.rows):
                raise DuplicateKeyError("duplicate lock_name")
            row = {}
            row.update(update.get("$setOnInsert", {}))
            row.update(update.get("$set", {}))
            self.rows.append(row)
            return SimpleNamespace(modified_count=0, matched_count=0, upserted_id="new-lock")
        row.update(update.get("$set", {}))
        return SimpleNamespace(modified_count=1, matched_count=1, upserted_id=None)

    async def find_one(self, query: dict | None = None, projection: dict | None = None, **_kwargs) -> dict | None:
        row = next((item for item in self.rows if matches_query(item, query)), None)
        return project_row(row, projection) if row else None


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


class FailIfCalledTradingViewClient:
    def __init__(self) -> None:
        raise AssertionError("TradingView should not be called when lock is held")


def patch_fake_db(monkeypatch, trades: list[dict], locks: list[dict] | None = None):
    db = SimpleNamespace(
        paper_trades=FakePaperTrades(trades),
        paper_update_runs=FakePaperUpdateRuns(),
        paper_update_locks=FakePaperUpdateLocks(locks),
    )
    monkeypatch.setattr(paper, "get_database", lambda: db)
    return db


def test_first_run_acquires_and_releases_lock(monkeypatch) -> None:
    db = patch_fake_db(monkeypatch, [make_trade("WAIT1")])
    monkeypatch.setattr(paper, "TradingViewClient", FakeTradingViewNoEntryClient)
    client = TestClient(app)

    response = client.post("/api/paper/update-trades?dry_run=true&max_trades=6&max_writes=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["blocked"] is False
    assert payload["lock_released"] is True
    assert db.paper_update_locks.rows[0]["status"] == "RELEASED"
    assert db.paper_update_locks.rows[0]["run_id"] == payload["run_id"]


def test_update_endpoint_blocks_when_lock_is_held(monkeypatch) -> None:
    db = patch_fake_db(
        monkeypatch,
        [make_trade("WAIT1")],
        [
            {
                "lock_name": paper.PAPER_UPDATE_LOCK_NAME,
                "status": "LOCKED",
                "run_id": "existing-run",
                "expires_at": "2999-01-01T00:00:00",
                "paper_only": True,
                "live_trading": False,
                "broker_orders": False,
            }
        ],
    )
    monkeypatch.setattr(paper, "TradingViewClient", FailIfCalledTradingViewClient)
    client = TestClient(app)

    response = client.post("/api/paper/update-trades?dry_run=true&max_trades=6&max_writes=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["blocked"] is True
    assert payload["block_reason"] == "LOCK_ALREADY_HELD"
    assert payload["processed"] == 0
    assert payload["updated_count"] == 0
    assert db.paper_update_locks.rows[0]["status"] == "LOCKED"
    assert db.paper_update_locks.rows[0]["run_id"] == "existing-run"
    assert db.paper_trades.update_calls == []
    assert db.paper_update_runs.rows[0]["status"] == "BLOCKED"
    assert db.paper_update_runs.rows[0]["block_reason"] == "LOCK_ALREADY_HELD"


def test_expired_lock_can_be_reclaimed(monkeypatch) -> None:
    db = patch_fake_db(
        monkeypatch,
        [make_trade("WAIT1")],
        [
            {
                "lock_name": paper.PAPER_UPDATE_LOCK_NAME,
                "status": "LOCKED",
                "run_id": "expired-run",
                "expires_at": "2000-01-01T00:00:00",
                "paper_only": True,
                "live_trading": False,
                "broker_orders": False,
            }
        ],
    )
    monkeypatch.setattr(paper, "TradingViewClient", FakeTradingViewNoEntryClient)
    client = TestClient(app)

    response = client.post("/api/paper/update-trades?dry_run=true&max_trades=6&max_writes=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["blocked"] is False
    assert db.paper_update_locks.rows[0]["status"] == "RELEASED"
    assert db.paper_update_locks.rows[0]["run_id"] == payload["run_id"]
    assert db.paper_update_locks.rows[0]["run_id"] != "expired-run"


def test_release_ignores_non_matching_run_id(monkeypatch) -> None:
    db = patch_fake_db(
        monkeypatch,
        [],
        [
            {
                "lock_name": paper.PAPER_UPDATE_LOCK_NAME,
                "status": "LOCKED",
                "run_id": "correct-run",
                "expires_at": "2999-01-01T00:00:00",
                "paper_only": True,
                "live_trading": False,
                "broker_orders": False,
            }
        ],
    )

    result = asyncio.run(paper.release_paper_update_lock(db, "wrong-run"))

    assert result["released"] is False
    assert db.paper_update_locks.rows[0]["status"] == "LOCKED"
    assert db.paper_update_locks.rows[0]["run_id"] == "correct-run"


def test_update_lock_endpoint_returns_current_lock_status(monkeypatch) -> None:
    patch_fake_db(
        monkeypatch,
        [],
        [
            {
                "lock_name": paper.PAPER_UPDATE_LOCK_NAME,
                "status": "LOCKED",
                "run_id": "current-run",
                "expires_at": "2999-01-01T00:00:00",
                "paper_only": True,
                "live_trading": False,
                "broker_orders": False,
            }
        ],
    )
    client = TestClient(app)

    response = client.get("/api/paper/update-lock")

    assert response.status_code == 200
    payload = response.json()
    assert payload["lock_name"] == paper.PAPER_UPDATE_LOCK_NAME
    assert payload["status"] == "LOCKED"
    assert payload["held"] is True
    assert payload["expired"] is False
    assert payload["paper_only"] is True
    assert payload["live_trading"] is False
    assert payload["broker_orders"] is False


def test_unbound_real_update_is_blocked_before_lock_or_trade_write(monkeypatch) -> None:
    db = patch_fake_db(monkeypatch, [make_trade("WAIT1")])
    monkeypatch.setattr(paper, "TradingViewClient", FakeTradingViewEntryClient)
    client = trusted_client()

    response = client.post("/api/paper/update-trades?dry_run=false&max_trades=1&max_writes=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["blocked"] is True
    assert payload["block_reason"] == "APPROVAL_REQUIRED"
    assert payload["updated_count"] == 0
    assert payload["proposed_write_count"] == 0
    assert db.paper_trades.update_calls == []
    assert db.paper_trades.rows[0]["status"] == "NOT_TRIGGERED"
    assert db.paper_update_locks.rows == []
