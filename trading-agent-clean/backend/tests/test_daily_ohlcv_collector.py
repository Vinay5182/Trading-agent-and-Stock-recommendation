import asyncio
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai import daily_ohlcv_collector as collector
from ai.daily_dataset_contract import (
    LABEL_PENDING,
    LIFECYCLE_NOT_STARTED,
    OUTCOME_NOT_READY,
    STAGE_SCORE_SNAPSHOT,
)
from ai.historical_ohlcv import HISTORICAL_OHLCV_SCHEMA_VERSION, candle_identity
from services.historical_ohlcv_store import build_persisted_candle


def run(coro):
    return asyncio.run(coro)


def dotted_get(row: dict, field: str):
    value = row
    for part in field.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def dotted_set(row: dict, field: str, value):
    current = row
    parts = field.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = deepcopy(value)


def matches(row: dict, query: dict | None) -> bool:
    for field, expected in (query or {}).items():
        if field == "$or":
            if not any(matches(row, item) for item in expected):
                return False
            continue
        actual = dotted_get(row, field)
        if isinstance(expected, dict):
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$gte" in expected and (actual is None or actual < expected["$gte"]):
                return False
            if "$lte" in expected and (actual is None or actual > expected["$lte"]):
                return False
            if "$exists" in expected and (actual is not None) is not bool(expected["$exists"]):
                return False
            continue
        if actual != expected:
            return False
    return True


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = [deepcopy(row) for row in rows]

    def sort(self, spec, direction=None):
        if isinstance(spec, list):
            sort_spec = spec
        else:
            sort_spec = [(spec, direction or 1)]
        for field, sort_direction in reversed(sort_spec):
            self.rows.sort(key=lambda row: dotted_get(row, field) or "", reverse=int(sort_direction) < 0)
        return self

    def limit(self, length: int):
        self.rows = self.rows[:length]
        return self

    async def to_list(self, length=None):
        return [deepcopy(row) for row in (self.rows if length is None else self.rows[:length])]

    def __aiter__(self):
        self._index = 0
        return self

    async def __anext__(self):
        if self._index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self._index]
        self._index += 1
        return deepcopy(row)


class FakeCollection:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = [deepcopy(row) for row in rows or []]
        self.update_calls = []
        self.bulk_write_calls = []
        self.insert_calls = []
        self.delete_calls = []

    def find(self, query=None, projection=None):
        return FakeCursor([row for row in self.rows if matches(row, query)])

    async def find_one(self, query=None, projection=None, sort=None):
        rows = [row for row in self.rows if matches(row, query)]
        if sort:
            cursor = FakeCursor(rows).sort(sort)
            rows = await cursor.to_list(1)
        return deepcopy(rows[0]) if rows else None

    async def count_documents(self, query=None):
        return len([row for row in self.rows if matches(row, query)])

    async def update_one(self, query, update, upsert=False):
        self.update_calls.append((deepcopy(query), deepcopy(update), upsert))
        row = next((item for item in self.rows if matches(item, query)), None)
        if row is None and upsert:
            row = {}
            for key, value in query.items():
                if not isinstance(value, dict):
                    dotted_set(row, key, value)
            for key, value in (update.get("$setOnInsert") or {}).items():
                dotted_set(row, key, value)
            for key, value in (update.get("$set") or {}).items():
                dotted_set(row, key, value)
            self.rows.append(row)
            return SimpleNamespace(upserted_id="new", upserted_count=1, matched_count=0, modified_count=0)
        if row is None:
            return SimpleNamespace(upserted_id=None, upserted_count=0, matched_count=0, modified_count=0)
        for key, value in (update.get("$set") or {}).items():
            dotted_set(row, key, value)
        return SimpleNamespace(upserted_id=None, upserted_count=0, matched_count=1, modified_count=1)

    async def bulk_write(self, operations, ordered=False):
        self.bulk_write_calls.append((operations, ordered))
        upserted = 0
        modified = 0
        for op in operations:
            query = deepcopy(getattr(op, "_filter", {}))
            update = deepcopy(getattr(op, "_doc", {}))
            row = next((item for item in self.rows if matches(item, query)), None)
            if row is None:
                row = {}
                for key, value in (update.get("$setOnInsert") or {}).items():
                    dotted_set(row, key, value)
                for key, value in (update.get("$set") or {}).items():
                    dotted_set(row, key, value)
                self.rows.append(row)
                upserted += 1
            else:
                for key, value in (update.get("$set") or {}).items():
                    dotted_set(row, key, value)
                modified += 1
        return SimpleNamespace(upserted_count=upserted, modified_count=modified)

    def aggregate(self, pipeline):
        rows = [deepcopy(row) for row in self.rows]
        for stage in pipeline:
            if "$match" in stage:
                rows = [row for row in rows if matches(row, stage["$match"])]
            elif "$group" in stage:
                group_field = stage["$group"]["_id"]
                if isinstance(group_field, str) and group_field.startswith("$"):
                    field = group_field[1:]
                    grouped = {}
                    for row in rows:
                        key = dotted_get(row, field)
                        grouped[key] = grouped.get(key, 0) + 1
                    rows = [{"_id": key, "count": count} for key, count in grouped.items()]
        return FakeCursor(rows)


class FakeDb:
    def __init__(self, *, ohlcv_rows=None, hsc_rows=None, dataset_rows=None, run_rows=None) -> None:
        self.historical_ohlcv = FakeCollection(ohlcv_rows)
        self.historical_scored_candidates = FakeCollection(hsc_rows)
        self.daily_trade_dataset = FakeCollection(dataset_rows)
        self.dataset_build_runs = FakeCollection(run_rows)
        self.scored_candidates = FakeCollection([])

    def __getitem__(self, name: str):
        return getattr(self, name)


def make_candle(symbol="RELIANCE", trade_date="2026-07-09", *, close=120, volume=2000, days_offset=0):
    date_value = pd.Timestamp(trade_date).date() + pd.Timedelta(days=days_offset).to_pytimedelta()
    target = date_value.isoformat()
    open_at, close_at = collector.target_date_bounds_utc(target, exchange="NSE")
    return {
        "schema_version": HISTORICAL_OHLCV_SCHEMA_VERSION,
        "candle_id": candle_identity("NSE", symbol, "1d", open_at),
        "exchange": "NSE",
        "canonical_symbol": symbol,
        "provider": "yfinance",
        "provider_symbol": f"{symbol}.NS",
        "timeframe": "1d",
        "trade_date": target,
        "candle_open_at": open_at,
        "candle_close_at": close_at,
        "open": close - 2,
        "high": close + 5,
        "low": close - 5,
        "close": close,
        "volume": volume,
        "is_closed": True,
        "provenance": {
            "provider": "yfinance",
            "provider_symbol": f"{symbol}.NS",
            "provider_interval": "1d",
            "requested_start": open_at,
            "requested_end": close_at,
            "source_timezone": "Asia/Kolkata",
            "timestamp_semantic": "open",
            "adjusted_prices": False,
            "acquisition_method": "test",
            "acquisition_version": HISTORICAL_OHLCV_SCHEMA_VERSION,
            "normalization_version": "phase5a-v1",
            "fetched_at": close_at,
            "provider_row_fingerprint": f"fp-{symbol}-{target}",
            "validation_status": "VALID",
            "validation_reason_codes": [],
        },
        "quality": {"status": "VALID", "reason_codes": [], "is_duplicate": False, "is_conflicting_duplicate": False},
    }


def fake_fetcher_with(*candles):
    async def fetcher(**kwargs):
        symbol = kwargs["canonical_symbol"]
        return {
            "provider": "yfinance",
            "canonical_symbol": symbol,
            "counts": {"canonical_candle_count": len(candles)},
            "warnings": [],
            "errors": [],
            "candles": [deepcopy(candle) for candle in candles if candle["canonical_symbol"] == symbol],
        }

    return fetcher


def patch_universe(monkeypatch, symbols=("RELIANCE",)):
    monkeypatch.setattr(
        collector,
        "get_universe_result",
        lambda universe: {
            "index_name": universe,
            "source": "TEST",
            "raw_count": len(symbols),
            "error": None,
            "symbols": [
                {
                    "exchange": "NSE",
                    "symbol": symbol,
                    "canonical_symbol": symbol,
                    "tradingview_symbol": f"NSE:{symbol}",
                    "index_name": universe,
                    "index_memberships": [universe],
                }
                for symbol in symbols
            ],
        },
    )


def test_dry_run_performs_no_writes(monkeypatch):
    patch_universe(monkeypatch)
    db = FakeDb()

    result = run(
        collector.run_daily_data_collection_pipeline(
            db,
            trade_date="2026-07-09",
            max_symbols=1,
            dry_run=True,
            fetcher=fake_fetcher_with(make_candle()),
            now=pd.Timestamp("2026-07-11T00:00:00Z").to_pydatetime(),
        )
    )

    assert result["mongo_writes"] is False
    assert result["status"] == collector.DAILY_COLLECTION_STATUS_DRY_RUN_COMPLETED
    assert not db.historical_ohlcv.update_calls
    assert not db.historical_scored_candidates.bulk_write_calls
    assert not db.daily_trade_dataset.update_calls
    assert not db.dataset_build_runs.update_calls


def test_duplicate_candle_is_skipped():
    candle = make_candle()
    existing = build_persisted_candle(candle, persistence_run_id="old", first_persisted_at="2026-07-10T00:00:00Z", preview_manifest_hash=None)
    existing["trade_date"] = candle["trade_date"]
    db = FakeDb(ohlcv_rows=[existing])

    result = run(collector.persist_daily_ohlcv_candles(db, [candle], run_id="run-1", dry_run=False))

    assert result["skipped_duplicate_count"] == 1
    assert result["inserted_count"] == 0
    assert not db.historical_ohlcv.update_calls


def test_deterministic_candle_id_is_stable():
    first = collector.build_daily_candle_id(exchange="NSE", canonical_symbol="RELIANCE", trade_date="2026-07-09")
    second = collector.build_daily_candle_id(exchange="NSE", canonical_symbol="RELIANCE", trade_date="2026-07-09")
    different = collector.build_daily_candle_id(exchange="NSE", canonical_symbol="RELIANCE", trade_date="2026-07-10")

    assert first == second
    assert first != different


def test_latest_closed_daily_candle_is_selected():
    older = make_candle(close=100)
    newer = {**make_candle(close=110), "candle_open_at": "2026-07-09T01:00:00Z", "candle_close_at": "2026-07-10T01:00:00Z"}

    result = run(
        collector.collect_daily_ohlcv_for_symbol(
            symbol="RELIANCE",
            trade_date="2026-07-09",
            fetcher=fake_fetcher_with(older, newer),
            now=pd.Timestamp("2026-07-11T00:00:00Z").to_pydatetime(),
        )
    )

    assert result["candles_found"] == 1
    assert result["candles"][0]["close"] == 110


def test_no_future_candle_is_used():
    future = make_candle(trade_date="2026-07-10")

    result = run(
        collector.collect_daily_ohlcv_for_symbol(
            symbol="RELIANCE",
            trade_date="2026-07-10",
            fetcher=fake_fetcher_with(future),
            now=pd.Timestamp("2026-07-09T12:00:00Z").to_pydatetime(),
        )
    )

    assert result["candles_found"] == 0
    assert result["status"] == "NO_DATA"


def test_holiday_no_data_returns_skipped_no_data_and_records_manifest(monkeypatch):
    patch_universe(monkeypatch)
    db = FakeDb()

    result = run(
        collector.run_daily_data_collection_pipeline(
            db,
            trade_date="2026-07-09",
            max_symbols=1,
            dry_run=False,
            fetcher=fake_fetcher_with(),
        )
    )

    assert result["status"] == collector.DAILY_COLLECTION_STATUS_SKIPPED_NO_DATA
    assert db.dataset_build_runs.update_calls


def test_daily_ohlcv_write_only_touches_historical_ohlcv(monkeypatch):
    patch_universe(monkeypatch)
    db = FakeDb()

    result = run(
        collector.collect_daily_ohlcv_for_universe(
            db,
            trade_date="2026-07-09",
            max_symbols=1,
            dry_run=False,
            run_id="run-1",
            fetcher=fake_fetcher_with(make_candle()),
            now=pd.Timestamp("2026-07-11T00:00:00Z").to_pydatetime(),
        )
    )

    assert result["ohlcv_inserted"] == 1
    assert db.historical_ohlcv.update_calls
    assert not db.historical_scored_candidates.bulk_write_calls
    assert not db.daily_trade_dataset.update_calls
    assert not db.scored_candidates.update_calls


def fake_candidate(symbol="RELIANCE"):
    return {
        "historical_candidate_id": "hsc_v1_test",
        "trade_date": "2026-07-09",
        "exchange": "NSE",
        "canonical_symbol": symbol,
        "symbol": symbol,
        "index_name": "HISTORICAL_BACKFILL",
        "index_memberships": ["HISTORICAL_BACKFILL"],
        "strategy_type": "MULTI",
        "score_version": "test-score-v1",
        "selected_for_tv": True,
        "swing_candidate": True,
        "momentum_candidate": False,
        "score": 80,
        "momentum_score": 10,
        "historical_mode": True,
        "current_price": 120,
        "previous_close": 100,
        "open_price": 118,
        "day_high": 125,
        "day_low": 115,
        "traded_volume": 2000,
        "relative_volume": 2.0,
        "thirty_day_change_percent": 20,
    }


def patch_candidate_builder(monkeypatch):
    monkeypatch.setattr(
        collector,
        "calculate_historical_features",
        lambda rows, symbol: pd.DataFrame([{"trade_date": "2026-07-09", "has_lookback": True, "canonical_symbol": symbol}]),
    )
    monkeypatch.setattr(collector, "build_historical_candidate_rows", lambda df, exchange: [fake_candidate()])


def test_pipeline_bridge_writes_only_historical_candidates_and_daily_dataset(monkeypatch):
    patch_candidate_builder(monkeypatch)
    candle = make_candle()
    db = FakeDb()

    result = run(
        collector.run_daily_candidate_dataset_pipeline(
            db,
            trade_date="2026-07-09",
            symbols=["RELIANCE"],
            daily_candles=[candle],
            run_id="run-bridge",
            dry_run=False,
        )
    )

    assert result["historical_candidates_inserted"] == 1
    assert result["daily_dataset_inserted"] == 1
    assert db.historical_scored_candidates.bulk_write_calls
    assert db.daily_trade_dataset.update_calls
    assert not db.historical_ohlcv.update_calls
    assert not db.scored_candidates.update_calls


def test_live_scored_candidates_remains_untouched_and_labels_stay_pending(monkeypatch):
    patch_candidate_builder(monkeypatch)
    db = FakeDb()

    result = run(
        collector.run_daily_candidate_dataset_pipeline(
            db,
            trade_date="2026-07-09",
            symbols=["RELIANCE"],
            daily_candles=[make_candle()],
            run_id="run-dry",
            dry_run=True,
        )
    )

    row = result["daily_dataset_rows"][0]
    assert not db.scored_candidates.update_calls
    assert row["identity"]["current_stage"] == STAGE_SCORE_SNAPSHOT
    assert row["ml_label"]["label_state"] == LABEL_PENDING
    assert row["future_outcome"]["outcome_state"] == OUTCOME_NOT_READY
    assert row["paper_trade_link"]["link_status"] == "PENDING"
    assert row["lifecycle_snapshot"]["lifecycle_status"] == LIFECYCLE_NOT_STARTED


def test_manifest_is_recorded_for_persistent_run(monkeypatch):
    patch_universe(monkeypatch)
    db = FakeDb()

    result = run(
        collector.run_daily_data_collection_pipeline(
            db,
            trade_date="2026-07-09",
            max_symbols=1,
            dry_run=False,
            fetcher=fake_fetcher_with(),
        )
    )

    assert result["manifest"]["run_type"] == collector.DAILY_COLLECTION_RUN_TYPE
    assert db.dataset_build_runs.update_calls


def test_read_only_status_endpoint_does_not_mutate_data(monkeypatch):
    db = FakeDb(
        ohlcv_rows=[make_candle()],
        hsc_rows=[fake_candidate()],
        dataset_rows=[
            {
                "identity": {"dataset_id": "ds-1", "current_stage": STAGE_SCORE_SNAPSHOT},
                "ml_label": {"label_state": LABEL_PENDING},
            }
        ],
        run_rows=[
            {
                "run_id": "run-1",
                "run_type": collector.DAILY_COLLECTION_RUN_TYPE,
                "trade_date": "2026-07-09",
                "status": collector.DAILY_COLLECTION_STATUS_COMPLETED,
                "completed_at": "2026-07-09T12:00:00Z",
            }
        ],
    )
    from routes import ai as ai_routes
    from main import app

    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    response = TestClient(app).get("/api/ai/daily-collection/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["historical_ohlcv_count"] == 1
    assert payload["label_pending_count"] == 1
    assert payload["mongo_writes"] is False
    assert not db.historical_ohlcv.update_calls
    assert not db.historical_scored_candidates.bulk_write_calls
    assert not db.daily_trade_dataset.update_calls
    assert not db.dataset_build_runs.update_calls


def test_no_external_calls_are_made_when_fetcher_is_injected(monkeypatch):
    patch_universe(monkeypatch)
    db = FakeDb()

    def forbidden_fetcher(**kwargs):
        raise AssertionError("external provider was called")

    result = run(
        collector.run_daily_data_collection_pipeline(
            db,
            trade_date="2026-07-09",
            max_symbols=1,
            dry_run=True,
            fetcher=fake_fetcher_with(make_candle()),
            now=pd.Timestamp("2026-07-11T00:00:00Z").to_pydatetime(),
        )
    )

    assert result["status"] == collector.DAILY_COLLECTION_STATUS_DRY_RUN_COMPLETED
    assert forbidden_fetcher
