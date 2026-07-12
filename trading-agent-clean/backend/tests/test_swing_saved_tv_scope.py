import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes import swing


INDEX_NAME = "BROAD_MARKET_750"
MISSING = object()


def run(coro):
    return asyncio.run(coro)


def dotted_get(row: dict, field: str):
    value = row
    for part in field.split("."):
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def matches_value(value, expected) -> bool:
    if isinstance(expected, dict):
        for operator, operand in expected.items():
            if operator == "$in":
                if value is MISSING or value not in operand:
                    return False
            elif operator == "$nin":
                if value is not MISSING and value in operand:
                    return False
            elif operator == "$exists":
                if (value is not MISSING) is not bool(operand):
                    return False
            elif operator == "$ne":
                if value is not MISSING and value == operand:
                    return False
            else:
                raise AssertionError(f"unsupported query operator {operator}")
        return True
    return value is not MISSING and value == expected


def matches_query(row: dict, query: dict | None) -> bool:
    if not query:
        return True
    for key, expected in query.items():
        if key == "$and":
            if not all(matches_query(row, item) for item in expected):
                return False
        elif key == "$or":
            if not any(matches_query(row, item) for item in expected):
                return False
        elif not matches_value(dotted_get(row, key), expected):
            return False
    return True


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = [dict(row) for row in rows]
        self.index = 0

    def sort(self, fields):
        for field, direction in reversed(fields):
            self.rows.sort(key=lambda row: str(dotted_get(row, field) if dotted_get(row, field) is not MISSING else ""), reverse=direction < 0)
        return self

    def limit(self, limit: int):
        self.rows = self.rows[:limit]
        return self

    def skip(self, skip: int):
        self.rows = self.rows[skip:]
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self.index]
        self.index += 1
        return dict(row)


class FakeCollection:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = [dict(row) for row in rows]

    def find(self, query: dict | None = None, *args, **kwargs):
        return FakeCursor([row for row in self.rows if matches_query(row, query)])

    async def count_documents(self, query: dict | None = None, *args, **kwargs):
        return len([row for row in self.rows if matches_query(row, query)])


class FakeDb:
    def __init__(self, candidates: list[dict], swing_confirmations: list[dict], momentum_confirmations: list[dict] | None = None) -> None:
        self.scored_candidates = FakeCollection(candidates)
        self.swing_tv_confirmations = FakeCollection(swing_confirmations)
        self.momentum_tv_confirmations = FakeCollection(momentum_confirmations or [])

    def __getitem__(self, name: str):
        return getattr(self, name)


def candidate(symbol: str, updated_at: str = "2026-07-09T10:00:00Z") -> dict:
    return {
        "index_name": INDEX_NAME,
        "symbol": symbol,
        "canonical_symbol": symbol,
        "tradingview_symbol": f"NSE:{symbol}",
        "selected_for_tv": True,
        "swing_candidate": True,
        "swing_status": "SWING_SELECTED_FOR_TV",
        "score": 80,
        "updated_at": updated_at,
    }


def confirmation(symbol: str, status: str, updated_at: str, *, strategy_type: str | None = None) -> dict:
    row = {
        "index_name": INDEX_NAME,
        "symbol": symbol,
        "canonical_symbol": symbol,
        "tradingview_symbol": f"NSE:{symbol}",
        "tv_status": status,
        "updated_at": updated_at,
    }
    if strategy_type is not None:
        row["strategy_type"] = strategy_type
    return row


def patch_db(monkeypatch, candidates: list[dict], swing_confirmations: list[dict], momentum_confirmations: list[dict] | None = None) -> FakeDb:
    db = FakeDb(candidates, swing_confirmations, momentum_confirmations)
    monkeypatch.setattr(swing, "get_database", lambda: db)
    return db


def test_saved_swing_loader_returns_only_current_candidate_symbols(monkeypatch) -> None:
    patch_db(
        monkeypatch,
        [candidate("AAA"), candidate("BBB"), candidate("CCC")],
        [
            confirmation("AAA", "CONFIRMED_SIGNAL", "2026-07-09T11:00:00Z"),
            confirmation("BBB", "WAIT_FOR_RETEST", "2026-07-09T11:01:00Z", strategy_type="swing"),
            confirmation("AAA", "REJECTED", "2026-07-08T11:00:00Z", strategy_type="swing"),
            confirmation("CCC", "TECHNICAL_FAILED", "2026-07-08T11:00:00Z", strategy_type="swing"),
            confirmation("OUTSIDE", "CONFIRMED_SIGNAL", "2026-07-09T11:00:00Z", strategy_type="swing"),
        ],
    )

    result = run(swing.get_swing_tv_confirmed(index_name=INDEX_NAME, timeframes=None, timeframe=None, limit=None))

    symbols = {row["symbol"] for row in result["rows"]}
    assert symbols == {"AAA", "BBB"}
    assert result["candidate_count"] == 3
    assert result["saved_result_count"] == 2
    assert result["missing_confirmation_count"] == 1
    assert result["unrelated_saved_ignored_count"] == 2
    assert result["status_counts"] == {"CONFIRMED_SIGNAL": 1, "WAIT_FOR_RETEST": 1}


def test_saved_swing_loader_ignores_momentum_confirmations(monkeypatch) -> None:
    patch_db(
        monkeypatch,
        [candidate("AAA")],
        [
            confirmation("AAA", "REJECTED", "2026-07-09T12:00:00Z", strategy_type="momentum"),
            confirmation("AAA", "CONFIRMED_SIGNAL", "2026-07-09T11:00:00Z", strategy_type="swing"),
        ],
        momentum_confirmations=[confirmation("AAA", "MOMENTUM_CONFIRMED", "2026-07-09T13:00:00Z", strategy_type="momentum")],
    )

    result = run(swing.get_swing_tv_confirmed(index_name=INDEX_NAME, timeframes=None, timeframe=None, limit=None))

    assert result["candidate_count"] == 1
    assert result["saved_result_count"] == 1
    assert result["rows"][0]["tv_status"] == "CONFIRMED_SIGNAL"
    assert result["status_counts"] == {"CONFIRMED_SIGNAL": 1}


def test_saved_swing_loader_empty_results_keep_candidate_scope(monkeypatch) -> None:
    patch_db(monkeypatch, [candidate("AAA"), candidate("BBB")], [])

    result = run(swing.get_swing_tv_confirmed(index_name=INDEX_NAME, timeframes=None, timeframe=None, limit=None))

    assert result["candidate_count"] == 2
    assert result["saved_result_count"] == 0
    assert result["missing_confirmation_count"] == 2
    assert result["rows"] == []
    assert result["scope_warning"] == "Only 0/2 current Swing candidates have saved TV results."
