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


def make_trade(
    symbol: str,
    status: str,
    *,
    outcome_status: str | None = None,
    entry_triggered: bool | None = None,
    paper_pnl: float = 0.0,
) -> dict:
    return {
        "_id": f"{symbol.lower()}-id",
        "symbol": symbol,
        "tradingview_symbol": f"NSE:{symbol}",
        "timeframe": "1D",
        "source_signal_type": "SWING_TV_CONFIRMED",
        "paper_only": True,
        "status": status,
        "outcome_status": outcome_status or status,
        "entry_triggered": entry_triggered if entry_triggered is not None else status not in {"NOT_TRIGGERED", "PLANNED", "WAITING", "WAITING_FOR_ENTRY"},
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "target_1": 120.0,
        "target_2": 130.0,
        "target_3": 140.0,
        "quantity": 10,
        "paper_pnl": paper_pnl,
        "updated_at": f"2026-06-07T00:00:00-{symbol}",
    }


def make_candle(*, high: float, low: float = 95.0, close: float = 101.0) -> dict:
    return {
        "time": 1_780_631_100,
        "open": 100.0,
        "high": high,
        "low": low,
        "close": close,
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


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.index = 0

    def sort(self, *args) -> "FakeCursor":
        if len(args) >= 2:
            key, direction = args[0], args[1]
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

    def find(self, query: dict | None = None, *_args, **_kwargs) -> FakeCursor:
        return FakeCursor([row.copy() for row in self.rows if matches_query(row, query)])

    async def update_one(self, *args, **kwargs) -> SimpleNamespace:
        self.update_calls.append((args, kwargs))
        return SimpleNamespace(modified_count=1)

    async def delete_one(self, *args, **kwargs) -> SimpleNamespace:
        self.delete_calls.append((args, kwargs))
        return SimpleNamespace(deleted_count=1)


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
        return [make_candle(high=99.0, close=98.0)]


class FakeTradingViewEntryClient(FakeTradingViewNoEntryClient):
    def fetch_candles(self, _timeframe: str, min_candles: int = 1) -> list[dict]:
        assert min_candles == 1
        return [make_candle(high=100.0, close=101.0)]


def patch_paper_database(monkeypatch, rows: list[dict], market_rows: list[dict] | None = None, snapshots: list[dict] | None = None) -> FakePaperTrades:
    collection = FakePaperTrades(rows)
    effective_market_rows = market_rows if market_rows is not None else [make_market_row(row["symbol"]) for row in rows]
    market_collection = FakeMarketData(effective_market_rows)
    if snapshots is None:
        snapshots = [
            make_snapshot_row(row["symbol"], trade_id=row["_id"], high=market.get("current_price", 101.0), low=market.get("current_price", 101.0), close=market.get("current_price", 101.0))
            for row, market in zip(rows, effective_market_rows)
        ]
    monkeypatch.setattr(
        paper,
        "get_database",
        lambda: SimpleNamespace(
            paper_trades=collection,
            market_data=market_collection,
            paper_market_snapshots=FakePaperMarketSnapshots(snapshots),
        ),
    )
    return collection


def test_settings_endpoint_is_paper_only() -> None:
    client = TestClient(app)

    response = client.get("/api/settings")

    assert response.status_code == 200
    payload = response.json()
    assert payload["paper_mode"] is True
    assert payload["live_trading_enabled"] is False


def test_paper_summary_counts_waiting_open_and_closed_trades(monkeypatch) -> None:
    patch_paper_database(
        monkeypatch,
        [
            make_trade("WAIT1", "NOT_TRIGGERED", entry_triggered=False),
            make_trade("WAIT2", "PLANNED", entry_triggered=False),
            make_trade("OPEN1", "ACTIVE", paper_pnl=15.0),
            make_trade("CLOSED1", "STOPPED", paper_pnl=-10.0),
            make_trade("CLOSED2", "TARGET_2_HIT", paper_pnl=30.0),
        ],
    )
    client = TestClient(app)

    response = client.get("/api/paper/summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_trades"] == 5
    assert payload["waiting_trades"] == 2
    assert payload["open_trades"] == 1
    assert payload["closed_trades"] == 2
    assert payload["not_triggered"] == 1
    assert payload["planned"] == 1
    assert payload["active"] == 1
    assert payload["stopped"] == 1
    assert payload["target_2_hit"] == 1


def test_dry_run_update_endpoint_does_not_write(monkeypatch) -> None:
    collection = patch_paper_database(
        monkeypatch,
        [make_trade("WAIT1", "WAITING_FOR_ENTRY", entry_triggered=False)],
        [make_market_row("WAIT1", high=99.0, close=98.0)],
    )
    monkeypatch.setattr(paper, "TradingViewClient", FakeTradingViewNoEntryClient)
    client = TestClient(app)

    response = client.post("/api/paper/update-trades?dry_run=true&max_trades=6&max_writes=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["mongo_writes_enabled"] is False
    assert payload["updated_count"] == 0
    assert payload["processed"] == 1
    assert payload["proposed_write_count"] == 0
    assert collection.update_calls == []
    assert collection.delete_calls == []


def test_dry_run_blocks_when_proposed_writes_exceed_limit(monkeypatch) -> None:
    collection = patch_paper_database(
        monkeypatch,
        [
            make_trade("WAIT1", "NOT_TRIGGERED", entry_triggered=False),
            make_trade("WAIT2", "NOT_TRIGGERED", entry_triggered=False),
        ],
    )
    monkeypatch.setattr(paper, "TradingViewClient", FakeTradingViewEntryClient)
    client = TestClient(app)

    response = client.post("/api/paper/update-trades?dry_run=true&max_trades=6&max_writes=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["blocked"] is True
    assert payload["block_reason"] == "MAX_WRITES_EXCEEDED"
    assert payload["mongo_writes_enabled"] is False
    assert payload["proposed_write_count"] == 2
    assert payload["updated_count"] == 0
    assert len(payload["results"]) == 2
    assert all(result["would_write"] is True for result in payload["results"])
    assert all(result["write_blocked"] is True for result in payload["results"])
    assert collection.update_calls == []
    assert collection.delete_calls == []


def test_real_mode_requires_approval_before_evaluation(monkeypatch) -> None:
    collection = patch_paper_database(
        monkeypatch,
        [
            make_trade("WAIT1", "NOT_TRIGGERED", entry_triggered=False),
            make_trade("WAIT2", "NOT_TRIGGERED", entry_triggered=False),
        ],
    )
    monkeypatch.setattr(paper, "TradingViewClient", FakeTradingViewEntryClient)
    client = trusted_client()

    response = client.post("/api/paper/update-trades?dry_run=false&max_trades=6&max_writes=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is False
    assert payload["blocked"] is True
    assert payload["block_reason"] == "APPROVAL_REQUIRED"
    assert payload["mongo_writes_enabled"] is False
    assert payload["proposed_write_count"] == 0
    assert payload["updated_count"] == 0
    assert payload["results"] == []
    assert collection.update_calls == []
    assert collection.delete_calls == []


def test_update_plans_real_mode_requires_approval(monkeypatch) -> None:
    collection = patch_paper_database(monkeypatch, [make_trade("WAIT1", "NOT_TRIGGERED", entry_triggered=False)])
    monkeypatch.setattr(paper, "TradingViewClient", FakeTradingViewEntryClient)
    client = trusted_client()

    response = client.post("/api/paper/update-plans?dry_run=false&max_trades=6&max_writes=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["blocked"] is True
    assert payload["block_reason"] == "APPROVAL_REQUIRED"
    assert payload["updated_count"] == 0
    assert collection.update_calls == []
    assert collection.delete_calls == []
