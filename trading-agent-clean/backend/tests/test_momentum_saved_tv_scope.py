import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes import momentum


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
            elif operator == "$gte":
                if value is MISSING or value < operand:
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
        self.find_calls = []

    def find(self, query: dict | None = None, *args, **kwargs):
        self.find_calls.append(query or {})
        return FakeCursor([row for row in self.rows if matches_query(row, query)])

    async def count_documents(self, query: dict | None = None, *args, **kwargs):
        return len([row for row in self.rows if matches_query(row, query)])


class FakeDb:
    def __init__(self, candidates: list[dict], confirmations: list[dict]) -> None:
        self.scored_candidates = FakeCollection(candidates)
        self.momentum_tv_confirmations = FakeCollection(confirmations)

    def __getitem__(self, name: str):
        return getattr(self, name)


def candidate(symbol: str, updated_at: str = "2026-07-09T10:00:00Z") -> dict:
    return {
        "index_name": INDEX_NAME,
        "symbol": symbol,
        "canonical_symbol": symbol,
        "tradingview_symbol": f"NSE:{symbol}",
        "momentum_candidate": True,
        "momentum_status": "MOMENTUM_PRECHECK_PASSED",
        "momentum_score": 80,
        "updated_at": updated_at,
    }


def confirmation(symbol: str, status: str, updated_at: str, *, index_name: str = INDEX_NAME, strategy_type: str = "momentum") -> dict:
    return {
        "index_name": index_name,
        "strategy_type": strategy_type,
        "symbol": symbol,
        "canonical_symbol": symbol,
        "tradingview_symbol": f"NSE:{symbol}",
        "tv_status": status,
        "updated_at": updated_at,
    }


def patch_db(monkeypatch, candidates: list[dict], confirmations: list[dict]) -> FakeDb:
    db = FakeDb(candidates, confirmations)
    monkeypatch.setattr(momentum, "get_database", lambda: db)
    return db


def query_contains_symbol_scope(query: dict) -> bool:
    if "$or" in query and any("symbol" in clause or "canonical_symbol" in clause for clause in query["$or"]):
        return True
    return any(query_contains_symbol_scope(item) for item in query.get("$and", []))


def test_saved_momentum_loader_returns_only_current_candidate_symbols(monkeypatch) -> None:
    db = patch_db(
        monkeypatch,
        [candidate("AAA"), candidate("BBB"), candidate("CCC")],
        [
            confirmation("AAA", "MOMENTUM_CONFIRMED", "2026-07-09T11:00:00Z"),
            confirmation("BBB", "WAIT_FOR_PULLBACK", "2026-07-09T11:01:00Z"),
            confirmation("AAA", "REJECTED", "2026-07-08T11:00:00Z"),
            confirmation("CCC", "TECHNICAL_FAILED", "2026-07-08T11:00:00Z"),
            confirmation("OUTSIDE", "MOMENTUM_CONFIRMED", "2026-07-09T11:00:00Z"),
        ],
    )

    result = run(momentum.get_momentum_tv_confirmed(index_name=INDEX_NAME, timeframes=None, timeframe=None, limit=None))

    symbols = {row["symbol"] for row in result["rows"]}
    assert symbols == {"AAA", "BBB"}
    assert result["candidate_count"] == 3
    assert result["saved_result_count"] == 2
    assert result["missing_confirmation_count"] == 1
    assert result["unrelated_saved_ignored_count"] == 2
    assert result["status_counts"] == {"MOMENTUM_CONFIRMED": 1, "WAIT_FOR_PULLBACK": 1}
    scoped_query = db.momentum_tv_confirmations.find_calls[-1]
    assert query_contains_symbol_scope(scoped_query)


def test_saved_momentum_loader_empty_results_do_not_fall_back_to_stale_data(monkeypatch) -> None:
    patch_db(
        monkeypatch,
        [candidate("AAA"), candidate("BBB")],
        [confirmation("OLD", "MOMENTUM_CONFIRMED", "2026-07-08T11:00:00Z")],
    )

    result = run(momentum.get_momentum_tv_confirmed(index_name=INDEX_NAME, timeframes=None, timeframe=None, limit=None))

    assert result["candidate_count"] == 2
    assert result["saved_result_count"] == 0
    assert result["missing_confirmation_count"] == 2
    assert result["rows"] == []
    assert result["status_counts"] == {}
    assert result["scope_warning"] == "Only 0/2 current Momentum candidates have saved TV results."
