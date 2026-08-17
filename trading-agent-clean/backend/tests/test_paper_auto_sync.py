from config import settings
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes import paper
from services.paper_identity import apply_setup_identity
from services.paper_migration import ensure_paper_trade_setup_index, migrate_paper_trade_setup_ids
from services.paper_automation import OUTCOME_INTERVAL_SECONDS, SYNC_INTERVAL_SECONDS
from services.paper_sync import sync_trade_ready


def parsed_time(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def matches(row: dict, query: dict | None) -> bool:
    if not query:
        return True
    for key, expected in query.items():
        actual = row.get(key)
        if isinstance(expected, dict) and "$in" in expected:
            if actual not in expected["$in"]:
                return False
        elif isinstance(expected, dict) and "$gte" in expected:
            if actual is None or parsed_time(actual) < parsed_time(expected["$gte"]):
                return False
        elif isinstance(expected, dict) and "$exists" in expected:
            exists = key in row
            if bool(expected["$exists"]) != exists:
                return False
        elif actual != expected:
            return False
    return True


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = [row.copy() for row in rows]
        self.index = 0

    def sort(self, *_args):
        if len(_args) >= 2 and isinstance(_args[0], str):
            key = _args[0]
            reverse = _args[1] == -1
            self.rows.sort(key=lambda row: row.get(key) or "", reverse=reverse)
        elif _args and isinstance(_args[0], list):
            key, direction = _args[0][0]
            self.rows.sort(key=lambda row: row.get(key) or "", reverse=direction == -1)
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


class FakeCollection:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.update_calls = []
        self.indexes = []

    async def create_index(self, fields, **kwargs):
        self.indexes.append((fields, kwargs))
        return kwargs.get("name", "index")

    def find(self, query: dict | None = None, *_args, **_kwargs):
        return FakeCursor([row for row in self.rows if matches(row, query)])

    async def find_one(self, query: dict | None = None, *_args, **_kwargs):
        row = next((row for row in self.rows if matches(row, query)), None)
        return row.copy() if row else None

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        self.update_calls.append((query, update, upsert))
        row = next((row for row in self.rows if matches(row, query)), None)
        if row is not None:
            row.update(update.get("$set", {}))
            for key, value in update.get("$inc", {}).items():
                row[key] = row.get(key, 0) + value
            return SimpleNamespace(modified_count=1 if update.get("$set") else 0, matched_count=1, upserted_id=None)
        if not upsert:
            return SimpleNamespace(modified_count=0, matched_count=0, upserted_id=None)
        new_row = {**query, **update.get("$setOnInsert", {}), **update.get("$set", {})}
        new_row.setdefault("_id", f"id-{len(self.rows) + 1}")
        self.rows.append(new_row)
        return SimpleNamespace(modified_count=0, matched_count=0, upserted_id=new_row["_id"])

    async def insert_one(self, document: dict):
        if document.get("paper_trade_id") and any(row.get("paper_trade_id") == document.get("paper_trade_id") for row in self.rows):
            raise DuplicateKeyError("duplicate paper_trade_id")
        row = document.copy()
        row.setdefault("_id", f"id-{len(self.rows) + 1}")
        self.rows.append(row)
        return SimpleNamespace(inserted_id=row["_id"])

    async def drop_index(self, name: str):
        self.indexes.append(("drop_index", {"name": name}))
        return None


class FakeDb:
    def __init__(self, swing_rows=None, momentum_rows=None, trades=None, signals=None, market_rows=None, snapshots=None) -> None:
        self.swing_tv_confirmations = FakeCollection(swing_rows)
        self.momentum_tv_confirmations = FakeCollection(momentum_rows)
        self.paper_trades = FakeCollection(trades)
        self.paper_signals = FakeCollection(signals)
        self.market_data = FakeCollection(market_rows)
        self.paper_market_snapshots = FakeCollection(snapshots)
        self.trade_journal = FakeCollection([])

    def __getitem__(self, name: str):
        return getattr(self, name)


def trade_ready_row(
    symbol: str,
    *,
    grade: str = "A",
    source_status: str = "CONFIRMED_SIGNAL",
    previous_low: float = 91.0,
) -> dict:
    return {
        "symbol": symbol,
        "tradingview_symbol": f"NSE:{symbol}",
        "index_name": "BROAD_MARKET_750",
        "tv_status": source_status,
        "tv_confirmed": True,
        "trade_quality_grade": grade,
        "trade_allowed": True,
        "paper_plan_valid": True,
        "paper_entry_price": 100.0,
        "paper_stop_loss": 90.0,
        "paper_target_1": 110.0,
        "paper_target_2": 120.0,
        "paper_target_3": 130.0,
        "paper_rr_1": 1.0,
        "paper_rr_2": 2.0,
        "paper_rr_3": 3.0,
        "previous_low": previous_low,
        "next_action_for_paper_trade": "PAPER_PLAN_READY",
    }


def existing_trade(symbol: str, status: str) -> dict:
    return {
        "_id": f"{symbol}-id",
        "symbol": symbol,
        "source_signal_type": "SWING_TV_CONFIRMED",
        "paper_only": True,
        "status": status,
        "outcome_status": status,
        "state_version": 1,
    }


def market_row(symbol: str, *, high: float = 100.0, low: float = 95.0, close: float = 101.0, updated_at: str = "2026-06-16T10:00:00") -> dict:
    return {
        "exchange": "NSE",
        "symbol": symbol,
        "canonical_symbol": symbol,
        "tradingview_symbol": f"NSE:{symbol}",
        "day_high": high,
        "day_low": low,
        "current_price": close,
        "updated_at": updated_at,
    }


def snapshot_row(symbol: str, *, trade_id: str | None = None, observed_at: str = "2026-06-16T10:00:00", high: float = 101.0, low: float = 99.0, close: float = 100.0) -> dict:
    return {
        "paper_only": True,
        "paper_trade_id": trade_id or f"{symbol}-id",
        "symbol": symbol,
        "canonical_symbol": symbol,
        "observed_at": parsed_time(observed_at),
        "observed_at_iso": observed_at,
        "high": high,
        "low": low,
        "close": close,
        "price": close,
        "source": "test_snapshot",
    }


def lifecycle_trade(symbol: str, status: str = "WAITING_FOR_ENTRY", *, created_at: str = "2026-06-16T09:00:00") -> dict:
    return {
        **existing_trade(symbol, status),
        "tradingview_symbol": f"NSE:{symbol}",
        "timeframe": "1D",
        "entry_triggered": status not in {"NOT_TRIGGERED", "PLANNED", "WAITING", "WAITING_FOR_ENTRY"},
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "initial_stop_loss": 90.0,
        "current_stop_loss": 90.0,
        "target_1": 120.0,
        "target_2": 130.0,
        "target_3": 140.0,
        "quantity": 10,
        "quantity_remaining": 10,
        "paper_pnl": 0,
        "created_at": created_at,
        "updated_at": created_at,
    }


def candle(high: float = 110.0, low: float = 98.0, close: float = 105.0) -> dict:
    return {
        "time": 1_700_000_000,
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1_000,
    }


def build_plan_candles() -> list[dict]:
    rows = [candle(high=101.0 + index, low=91.0 + index, close=96.0 + index) for index in range(9)]
    rows.append(candle())
    return rows


def paper_signal_row(symbol: str, *, source_id: str | None = None) -> dict:
    return {
        **trade_ready_row(symbol),
        "_id": source_id or f"confirmation-{symbol}",
        "paper_only": True,
        "timeframe": "1D",
        "signal_type": "SWING_TV_CONFIRMED",
        "created_at": "2026-06-16T09:00:00",
        "updated_at": "2026-06-16T09:05:00",
        "source_confirmation_id": source_id or f"confirmation-{symbol}",
        "source_collection": "paper_signals",
    }


class FakeTradingViewClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def connect_to_debug_port(self) -> bool:
        return True

    def open_symbol(self, _symbol: str) -> dict:
        return {}

    def fetch_candles(self, _timeframe: str, min_candles: int = 1) -> list[dict]:
        assert min_candles == 1
        return build_plan_candles()


def test_sync_adds_trade_ready_once_and_protects_active_and_completed() -> None:
    active_trade = {
        **existing_trade("ACTIVE", "ACTIVE"),
        "target_1": 120.0,
        "target_2": 130.0,
        "target_3": 140.0,
    }
    completed_trade = {
        **existing_trade("DONE", "SL_HIT"),
        "target_1": 120.0,
        "target_2": 130.0,
        "target_3": 140.0,
    }
    db = FakeDb(
        swing_rows=[
            trade_ready_row("NEW"),
            trade_ready_row("ACTIVE"),
            trade_ready_row("DONE"),
            trade_ready_row("WATCH", grade="B"),
        ],
        trades=[active_trade, completed_trade],
    )

    first = asyncio.run(sync_trade_ready(db_override=db))
    second = asyncio.run(sync_trade_ready(db_override=db))

    new_trades = [row for row in db.paper_trades.rows if row["symbol"] == "NEW"]
    assert len(new_trades) == 1
    assert new_trades[0]["status"] == "WAITING_FOR_ENTRY"
    assert new_trades[0]["outcome_status"] == "WAITING_FOR_ENTRY"
    assert new_trades[0]["initial_stop_loss"] == 91.0
    assert new_trades[0]["current_stop_loss"] == 91.0
    assert new_trades[0]["stop_loss"] == 91.0
    assert new_trades[0]["target_1"] == 109.0
    assert new_trades[0]["target_2"] == 118.0
    assert new_trades[0]["target_3"] == 127.0
    assert new_trades[0]["risk_per_share"] == 9.0
    assert new_trades[0]["paper_risk_per_share"] == 9.0
    assert new_trades[0]["paper_target_1"] == 109.0
    assert new_trades[0]["paper_target_2"] == 118.0
    assert new_trades[0]["paper_target_3"] == 127.0
    assert new_trades[0]["risk_reward_1"] == 1.0
    assert new_trades[0]["risk_reward_2"] == 2.0
    assert new_trades[0]["risk_reward_3"] == 3.0
    assert not any(row["symbol"] == "WATCH" for row in db.paper_trades.rows)
    preserved_active = next(row for row in db.paper_trades.rows if row["symbol"] == "ACTIVE")
    preserved_done = next(row for row in db.paper_trades.rows if row["symbol"] == "DONE")
    assert preserved_active["status"] == "ACTIVE"
    assert preserved_done["status"] == "SL_HIT"
    assert preserved_active["target_1"] == 120.0
    assert preserved_active["target_2"] == 130.0
    assert preserved_active["target_3"] == 140.0
    assert preserved_done["target_1"] == 120.0
    assert preserved_done["target_2"] == 130.0
    assert preserved_done["target_3"] == 140.0
    assert first["paper_trades_upserted"] == 1
    assert first["completed_outcomes_protected"] == 1
    assert first["duplicates_prevented"] == 2
    assert first["existing_trade_states_preserved"] == 2
    assert second["paper_trades_upserted"] == 0
    assert second["completed_outcomes_protected"] == 1
    assert second["duplicates_prevented"] == 3
    assert first["identity_fields"] == ["setup_id", "paper_only"]
    assert new_trades[0]["setup_id"]


def test_sync_setup_id_prevents_duplicate_across_statuses() -> None:
    confirmation = {
        **trade_ready_row("DUP"),
        "_id": "confirmation-dup",
        "created_at": "2026-06-16T09:00:00",
    }
    active_trade = apply_setup_identity(
        {
            **existing_trade("DUP", "ACTIVE"),
            "timeframe": "1D",
            "source_collection": "swing_tv_confirmations",
            "source_confirmation_id": "confirmation-dup",
            "created_at": "2026-06-16T09:00:00",
        }
    )
    db = FakeDb(swing_rows=[confirmation], trades=[active_trade])

    result = asyncio.run(sync_trade_ready(db_override=db))

    assert result["paper_trades_upserted"] == 0
    assert result["existing_trades_protected"] == 1
    assert len([row for row in db.paper_trades.rows if row["symbol"] == "DUP"]) == 1
    assert next(row for row in db.paper_trades.rows if row["symbol"] == "DUP")["status"] == "ACTIVE"


def test_sync_creates_new_daily_trade_when_saved_confirmation_id_is_reused() -> None:
    confirmation = {
        **trade_ready_row("DAILY"),
        "_id": "confirmation-daily",
        "created_at": "2026-06-18T06:49:50.060000",
        "updated_at": "2026-06-18T09:30:00Z",
        "confirmed_at": "2026-06-18T09:30:00Z",
        "source_candle_at": "2026-06-18T09:15:00Z",
    }
    db = FakeDb(swing_rows=[confirmation])

    first = asyncio.run(sync_trade_ready(db_override=db))
    db.swing_tv_confirmations.rows[0].update(
        {
            "updated_at": "2026-07-10T17:18:48.137824Z",
            "confirmed_at": "2026-07-10T17:18:48.137824Z",
            "source_candle_at": "2026-07-10T09:45:00Z",
        }
    )
    second = asyncio.run(sync_trade_ready(db_override=db))

    trades = sorted(
        [row for row in db.paper_trades.rows if row["symbol"] == "DAILY"],
        key=lambda row: row["setup_date"],
    )
    assert first["paper_trades_upserted"] == 1
    assert second["paper_trades_upserted"] == 1
    assert len(trades) == 2
    assert [row["setup_date"] for row in trades] == ["2026-06-18", "2026-07-10"]
    assert trades[0]["source_confirmation_id"] == trades[1]["source_confirmation_id"] == "confirmation-daily"
    assert trades[0]["setup_id"] != trades[1]["setup_id"]
    assert trades[1]["created_at"] == "2026-07-10T17:18:48.137824Z"
    assert trades[1]["source_confirmation_created_at"] == "2026-06-18T06:49:50.060000"
    assert trades[1]["source_confirmation_updated_at"] == "2026-07-10T17:18:48.137824Z"
    assert trades[1]["source_trade_date"] == "2026-07-10"
    api_row = paper.paper_api_row(trades[1])
    assert api_row["setup_date"] == "2026-07-10"
    assert api_row["source_trade_date"] == "2026-07-10"
    assert api_row["tv_confirmed_at"] == "2026-07-10T17:18:48.137824Z"


def test_sync_does_not_duplicate_same_day_saved_confirmation() -> None:
    confirmation = {
        **trade_ready_row("SAMEDAY"),
        "_id": "confirmation-sameday",
        "created_at": "2026-06-18T06:49:50.060000",
        "updated_at": "2026-07-10T17:18:48.137824Z",
        "confirmed_at": "2026-07-10T17:18:48.137824Z",
        "source_candle_at": "2026-07-10T09:45:00Z",
    }
    db = FakeDb(swing_rows=[confirmation])

    first = asyncio.run(sync_trade_ready(db_override=db))
    second = asyncio.run(sync_trade_ready(db_override=db))

    trades = [row for row in db.paper_trades.rows if row["symbol"] == "SAMEDAY"]
    assert first["paper_trades_upserted"] == 1
    assert second["paper_trades_upserted"] == 0
    assert len(trades) == 1
    assert trades[0]["setup_date"] == "2026-07-10"
    assert trades[0]["created_at"] == "2026-07-10T17:18:48.137824Z"


def test_sync_protects_existing_same_day_v1_trade() -> None:
    existing_v1 = {
        **existing_trade("V1TODAY", "WAITING_FOR_ENTRY"),
        "setup_id": "paper_setup:v1:legacy-today",
        "canonical_setup_id": "paper_setup:v1:legacy-today",
        "source_confirmation_id": "confirmation-v1today",
        "source_collection": "swing_tv_confirmations",
        "setup_date": "2026-07-10",
        "created_at": "2026-07-10T17:18:48.137824Z",
    }
    confirmation = {
        **trade_ready_row("V1TODAY"),
        "_id": "confirmation-v1today",
        "created_at": "2026-06-18T06:49:50.060000",
        "updated_at": "2026-07-10T17:18:48.137824Z",
        "confirmed_at": "2026-07-10T17:18:48.137824Z",
        "source_candle_at": "2026-07-10T09:45:00Z",
    }
    db = FakeDb(swing_rows=[confirmation], trades=[existing_v1])

    result = asyncio.run(sync_trade_ready(db_override=db))

    trades = [row for row in db.paper_trades.rows if row["symbol"] == "V1TODAY"]
    assert result["paper_trades_upserted"] == 0
    assert result["existing_trades_protected"] == 1
    assert len(trades) == 1
    assert trades[0]["setup_id"] == "paper_setup:v1:legacy-today"


def test_sync_does_not_let_old_legacy_row_block_new_setup_date() -> None:
    legacy_trade = {
        **existing_trade("ROLLOVER", "WAITING_FOR_ENTRY"),
        "created_at": "2026-06-18T06:49:50.060000",
        "updated_at": "2026-06-18T09:30:00Z",
    }
    confirmation = {
        **trade_ready_row("ROLLOVER"),
        "_id": "confirmation-rollover",
        "created_at": "2026-06-18T06:49:50.060000",
        "updated_at": "2026-07-10T17:18:48.137824Z",
        "confirmed_at": "2026-07-10T17:18:48.137824Z",
        "source_candle_at": "2026-07-10T09:45:00Z",
    }
    db = FakeDb(swing_rows=[confirmation], trades=[legacy_trade])

    result = asyncio.run(sync_trade_ready(db_override=db))

    trades = [row for row in db.paper_trades.rows if row["symbol"] == "ROLLOVER"]
    new_trade = next(row for row in trades if row.get("setup_date") == "2026-07-10")
    old_trade = next(row for row in trades if row["_id"] == "ROLLOVER-id")
    assert result["paper_trades_upserted"] == 1
    assert len(trades) == 2
    assert old_trade.get("setup_id") is None
    assert old_trade["created_at"] == "2026-06-18T06:49:50.060000"
    assert new_trade["source_confirmation_id"] == "confirmation-rollover"
    assert new_trade["setup_date"] == "2026-07-10"


def test_sync_preserves_existing_waiting_targets_until_rebuild_path() -> None:
    waiting = {
        **existing_trade("WAITOLD", "WAITING_FOR_ENTRY"),
        "target_1": 120.0,
        "target_2": 130.0,
        "target_3": 140.0,
    }
    db = FakeDb(swing_rows=[trade_ready_row("WAITOLD")], trades=[waiting])

    result = asyncio.run(sync_trade_ready(db_override=db))

    row = next(row for row in db.paper_trades.rows if row["symbol"] == "WAITOLD")
    assert result["paper_trades_upserted"] == 0
    assert row["status"] == "WAITING_FOR_ENTRY"
    assert row["target_1"] == 120.0
    assert row["target_2"] == 130.0
    assert row["target_3"] == 140.0


def test_setup_id_migration_keeps_most_advanced_duplicate_and_preserves_history() -> None:
    waiting = {
        **existing_trade("MIGRATE", "WAITING_FOR_ENTRY"),
        "_id": "waiting-id",
        "timeframe": "1D",
        "source_confirmation_id": "confirmation-migrate",
        "source_collection": "swing_tv_confirmations",
        "created_at": "2026-06-16T09:00:00",
        "updated_at": "2026-06-16T09:05:00",
        "entry_price": 100.0,
        "initial_stop_loss": 90.0,
        "current_stop_loss": 90.0,
    }
    t2_partial = {
        **existing_trade("MIGRATE", "T2_PARTIAL"),
        "_id": "advanced-id",
        "timeframe": "1D",
        "source_confirmation_id": "confirmation-migrate",
        "source_collection": "swing_tv_confirmations",
        "created_at": "2026-06-16T09:00:00",
        "updated_at": "2026-06-16T12:00:00",
        "entry_price": 100.0,
        "initial_stop_loss": 90.0,
        "current_stop_loss": 120.0,
        "partial_exit_1": {"percent": 33},
        "partial_exit_2": {"percent": 33},
    }
    db = FakeDb(trades=[waiting, t2_partial])

    result = asyncio.run(migrate_paper_trade_setup_ids(db, apply=True))

    assert result["ok"] is True
    assert result["duplicate_groups"] == 1
    assert result["duplicates_archived"] == 1
    advanced = next(row for row in db.paper_trades.rows if row["_id"] == "advanced-id")
    archived = next(row for row in db.paper_trades.rows if row["_id"] == "waiting-id")
    assert advanced["setup_id"] == advanced["canonical_setup_id"]
    assert archived["duplicate_of_setup_id"] == advanced["setup_id"]
    assert archived["duplicate_resolution"] == "ARCHIVED_DUPLICATE_RETAINED_HISTORY"
    assert archived["status"] == "WAITING_FOR_ENTRY"
    assert archived["entry_price"] == 100.0
    assert advanced["partial_exit_2"] == {"percent": 33}

    second = asyncio.run(migrate_paper_trade_setup_ids(db, apply=True))

    assert second["ok"] is True
    assert second["duplicate_groups"] == 0
    assert second["duplicates_archived"] == 0
    assert advanced["status"] == "T2_PARTIAL"
    assert advanced["current_stop_loss"] == 120.0
    assert advanced["partial_exit_2"] == {"percent": 33}


def test_setup_id_migration_groups_legacy_duplicates_without_source_confirmation_id() -> None:
    planned = {
        **existing_trade("LEGACY", "PLANNED"),
        "_id": "legacy-planned",
        "timeframe": "1D",
        "created_at": "2026-06-15T09:00:00",
        "updated_at": "2026-06-15T09:05:00",
    }
    active = {
        **existing_trade("LEGACY", "ACTIVE"),
        "_id": "legacy-active",
        "timeframe": "1D",
        "created_at": "2026-06-16T09:00:00",
        "updated_at": "2026-06-16T11:00:00",
        "entry_triggered": True,
    }
    db = FakeDb(trades=[planned, active])

    result = asyncio.run(migrate_paper_trade_setup_ids(db, apply=True))

    assert result["duplicate_groups"] == 1
    active_row = next(row for row in db.paper_trades.rows if row["_id"] == "legacy-active")
    planned_row = next(row for row in db.paper_trades.rows if row["_id"] == "legacy-planned")
    assert active_row["setup_identity"]["legacy_identity_scope"] == "symbol_source_timeframe"
    assert planned_row["duplicate_of_setup_id"] == active_row["setup_id"]
    assert planned_row["status"] == "PLANNED"


def test_setup_id_unique_index_excludes_missing_and_empty_setup_id_documents() -> None:
    db = FakeDb(trades=[existing_trade("NOSETUP", "WAITING_FOR_ENTRY")])

    result = asyncio.run(ensure_paper_trade_setup_index(db))

    assert result["created"] is True
    assert result["partialFilterExpression"] == {
        "paper_only": True,
        "setup_id": {"$exists": True, "$type": "string", "$gt": ""},
    }
    assert db.paper_trades.indexes[-1][1]["partialFilterExpression"] == result["partialFilterExpression"]


def test_build_paper_plans_concurrent_calls_create_one_logical_trade(monkeypatch) -> None:
    db = FakeDb(signals=[paper_signal_row("BUILD")])
    monkeypatch.setattr(paper, "get_database", lambda: db)
    import tv_client
    monkeypatch.setattr(tv_client, "TradingViewClient", FakeTradingViewClient)

    async def run_twice():
        return await asyncio.gather(
            paper.build_paper_plans(limit=1, timeframe="1D", save=True, paper_capital=settings.STARTING_VIRTUAL_BALANCE, risk_percent=1, signal_type="SWING_TV_CONFIRMED"),
            paper.build_paper_plans(limit=1, timeframe="1D", save=True, paper_capital=settings.STARTING_VIRTUAL_BALANCE, risk_percent=1, signal_type="SWING_TV_CONFIRMED"),
        )

    responses = asyncio.run(run_twice())

    rows = [row for row in db.paper_trades.rows if row.get("symbol") == "BUILD"]
    assert len(rows) == 1
    assert sum(response["upserted_count"] for response in responses) == 1
    assert rows[0]["status"] == "WAITING_FOR_ENTRY"
    assert rows[0]["setup_id"]
    assert rows[0]["ai_gate_mode"] == "ADVISORY"
    assert all("$set" not in update for _query, update, upsert in db.paper_trades.update_calls if upsert)


def test_paper_ai_gate_is_advisory_by_default_and_preserves_prediction(monkeypatch) -> None:
    import ml.predict as ml_predict
    import ml.train as ml_train

    feature_names = ["rule_score", "trend_score", "momentum_score", "volume_score", "risk_score"]
    signal = paper_signal_row("ADVISORY")
    signal.update({name: index + 1 for index, name in enumerate(feature_names)})
    plan = paper.build_plan_from_candles(signal, build_plan_candles(), settings.STARTING_VIRTUAL_BALANCE, 1)
    settings_override = {**vars(paper.settings), "PAPER_AI_HARD_GATE_ENABLED": False}
    monkeypatch.setattr(paper, "settings", SimpleNamespace(**settings_override))
    monkeypatch.setattr(ml_predict, "_loaded_meta", {"features": feature_names})

    def fake_predict(feature_row: dict) -> dict:
        assert set(feature_row) == set(feature_names)
        return {"prediction": "LOSS", "confidence": {"LOSS": 0.9, "WIN": 0.1}}

    training_called = False

    async def fake_train():
        nonlocal training_called
        training_called = True
        return {}

    monkeypatch.setattr(ml_predict, "predict_outcome", fake_predict)
    monkeypatch.setattr(ml_train, "run_model_training", fake_train)

    paper.apply_paper_plan_ai_gate_if_ready(plan, signal)

    assert plan["status"] == "WAITING_FOR_ENTRY"
    assert plan["ai_gate_mode"] == "ADVISORY"
    assert plan["ai_gate_decision"] == "REJECTED"
    assert plan["ai_gate_prediction"] == "LOSS"
    assert plan["ai_gate_confidence"] == {"LOSS": 0.9, "WIN": 0.1}
    assert plan["ai_reason"] == "Advisory only: Model predicted non-WIN"
    assert training_called is False


def test_paper_ai_gate_can_hard_reject_only_when_enabled_and_ready(monkeypatch) -> None:
    import ml.predict as ml_predict

    feature_names = ["rule_score", "trend_score", "momentum_score", "volume_score", "risk_score"]
    signal = paper_signal_row("HARDGATE")
    signal.update({name: index + 1 for index, name in enumerate(feature_names)})
    plan = paper.build_plan_from_candles(signal, build_plan_candles(), settings.STARTING_VIRTUAL_BALANCE, 1)
    settings_override = {**vars(paper.settings), "PAPER_AI_HARD_GATE_ENABLED": True}
    monkeypatch.setattr(paper, "settings", SimpleNamespace(**settings_override))
    monkeypatch.setattr(ml_predict, "_loaded_meta", {"features": feature_names})
    monkeypatch.setattr(
        ml_predict,
        "predict_outcome",
        lambda _features: {"prediction": "LOSS", "confidence": {"LOSS": 0.8, "WIN": 0.2}},
    )

    paper.apply_paper_plan_ai_gate_if_ready(plan, signal)

    assert plan["status"] == "AI_REJECTED"
    assert plan["ai_gate_mode"] == "HARD"
    assert plan["ai_gate_decision"] == "REJECTED"
    assert plan["ai_gate_prediction"] == "LOSS"


def test_missing_ai_features_do_not_block_paper_trade_when_hard_gate_off(monkeypatch) -> None:
    import ml.predict as ml_predict

    feature_names = ["rule_score", "trend_score", "momentum_score", "volume_score", "risk_score"]
    signal = paper_signal_row("MISSINGAI")
    signal["rule_score"] = None
    plan = paper.build_plan_from_candles(signal, build_plan_candles(), settings.STARTING_VIRTUAL_BALANCE, 1)
    settings_override = {**vars(paper.settings), "PAPER_AI_HARD_GATE_ENABLED": False}
    monkeypatch.setattr(paper, "settings", SimpleNamespace(**settings_override))
    monkeypatch.setattr(ml_predict, "_loaded_meta", {"features": feature_names})
    monkeypatch.setattr(
        ml_predict,
        "predict_outcome",
        lambda _features: (_ for _ in ()).throw(AssertionError("prediction should not run with incomplete features")),
    )

    paper.apply_paper_plan_ai_gate_if_ready(plan, signal)

    assert plan["status"] == "WAITING_FOR_ENTRY"
    assert plan["ai_gate_mode"] == "ADVISORY"
    assert plan["ai_gate_decision"] == "SKIPPED"
    assert plan["ai_gate_reason"] == "Model features incomplete"
    assert "rule_score" in plan["ai_gate_missing_features"]


def test_upsert_paper_plans_concurrent_calls_create_one_logical_trade(monkeypatch) -> None:
    signal = paper_signal_row("UPSERT")
    plan = paper.build_plan_from_candles(signal, build_plan_candles(), settings.STARTING_VIRTUAL_BALANCE, 1)
    db = FakeDb()
    monkeypatch.setattr(paper, "get_database", lambda: db)

    async def run_twice():
        return await asyncio.gather(
            paper.upsert_paper_plans([plan]),
            paper.upsert_paper_plans([plan]),
        )

    responses = asyncio.run(run_twice())

    rows = [row for row in db.paper_trades.rows if row.get("symbol") == "UPSERT"]
    assert len(rows) == 1
    assert sum(upserted for upserted, _modified in responses) == 1
    assert rows[0]["setup_id"] == plan["setup_id"]
    assert all("$set" not in update for _query, update, upsert in db.paper_trades.update_calls if upsert)


def test_terminal_trade_cannot_be_reset_by_plan_builder(monkeypatch) -> None:
    signal = paper_signal_row("TERMINAL", source_id="terminal-confirmation")
    waiting_plan = paper.build_plan_from_candles(signal, build_plan_candles(), settings.STARTING_VIRTUAL_BALANCE, 1)
    completed_trade = apply_setup_identity(
        {
            **waiting_plan,
            "_id": "terminal-id",
            "status": "COMPLETED",
            "outcome_status": "T3_HIT",
            "state": "T3_HIT",
            "current_stop_loss": 120.0,
            "partial_exit_1": {"exit_price": 120.0, "quantity": 3, "percent": 33, "paper_pnl": 60.0},
            "partial_exit_2": {"exit_price": 130.0, "quantity": 3, "percent": 33, "paper_pnl": 90.0},
            "partial_exit_3": {"exit_price": 140.0, "quantity": 4, "percent": 34, "paper_pnl": 160.0},
            "paper_pnl": 310.0,
        }
    )
    db = FakeDb(trades=[completed_trade])
    monkeypatch.setattr(paper, "get_database", lambda: db)

    result = asyncio.run(paper.upsert_paper_plans([waiting_plan]))

    row = db.paper_trades.rows[0]
    assert result == (0, 0)
    assert row["status"] == "COMPLETED"
    assert row["outcome_status"] == "T3_HIT"
    assert row["current_stop_loss"] == 120.0
    assert row["partial_exit_3"]["percent"] == 34


def test_legacy_identity_metadata_update_preserves_lifecycle_state() -> None:
    legacy_active = {
        **existing_trade("LEGACYACTIVE", "ACTIVE"),
        "current_stop_loss": 123.0,
        "partial_exit_1": {"exit_price": 120.0, "quantity": 3},
    }
    db = FakeDb(swing_rows=[trade_ready_row("LEGACYACTIVE")], trades=[legacy_active])

    result = asyncio.run(sync_trade_ready(db_override=db))

    row = next(row for row in db.paper_trades.rows if row["symbol"] == "LEGACYACTIVE")
    assert result["paper_trades_upserted"] == 0
    assert result["existing_trades_protected"] == 1
    assert row["setup_id"]
    assert row["status"] == "ACTIVE"
    assert row["outcome_status"] == "ACTIVE"
    assert row["current_stop_loss"] == 123.0
    assert row["partial_exit_1"] == {"exit_price": 120.0, "quantity": 3}


def test_trade_ready_sync_interval_is_15_seconds() -> None:
    assert SYNC_INTERVAL_SECONDS == 15


def test_market_data_outcome_scheduler_interval_is_60_seconds() -> None:
    assert OUTCOME_INTERVAL_SECONDS == 60


def test_automatic_outcome_update_guards_completed_trade(monkeypatch) -> None:
    waiting = {
        **existing_trade("WAIT", "NOT_TRIGGERED"),
        "tradingview_symbol": "NSE:WAIT",
        "timeframe": "1D",
        "entry_triggered": False,
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "target_1": 120.0,
        "target_2": 130.0,
        "target_3": 140.0,
        "quantity": 10,
        "paper_pnl": 0,
    }
    completed = {**waiting, **existing_trade("DONE", "T1_HIT")}
    db = FakeDb(trades=[waiting, completed], market_rows=[market_row("WAIT")])

    response = asyncio.run(paper.run_automatic_outcome_update(db_override=db))

    assert response["updated_count"] == 1
    assert response["completed_protected"] == 0
    assert next(row for row in db.paper_trades.rows if row["symbol"] == "WAIT")["status"] == "ACTIVE"
    assert next(row for row in db.paper_trades.rows if row["symbol"] == "DONE")["status"] == "T1_HIT"
    assert len(db.paper_trades.update_calls) == 1
    assert db.paper_trades.update_calls[0][0]["status"] == "NOT_TRIGGERED"


def test_automatic_completed_trade_is_journaled_without_mutating_completed_again() -> None:
    trade = {
        **existing_trade("WIN", "T2_PARTIAL"),
        "tradingview_symbol": "NSE:WIN",
        "timeframe": "1D",
        "entry_triggered": True,
        "entry_triggered_at": "2026-06-10T10:00:00",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "initial_stop_loss": 90.0,
        "current_stop_loss": 120.0,
        "target_1": 120.0,
        "target_2": 130.0,
        "target_3": 140.0,
        "quantity": 10,
        "quantity_remaining": 4,
        "partial_exit_1": {"exit_price": 120.0, "quantity": 3, "percent": 33, "paper_pnl": 60.0},
        "partial_exit_2": {"exit_price": 130.0, "quantity": 3, "percent": 33, "paper_pnl": 90.0},
        "paper_pnl": 150.0,
        "trade_quality_grade": "A",
        "trap_status": "CLEAN",
        "created_at": "2026-06-10T09:00:00",
    }
    db = FakeDb(trades=[trade], market_rows=[market_row("WIN", high=140.0, low=125.0, close=140.0)])

    response = asyncio.run(paper.run_automatic_outcome_update(db_override=db))
    second = asyncio.run(paper.run_automatic_outcome_update(db_override=db))

    updated_trade = next(row for row in db.paper_trades.rows if row["symbol"] == "WIN")
    assert response["updated_count"] == 1
    assert updated_trade["status"] == "COMPLETED"
    assert updated_trade["outcome_status"] == "T3_HIT"
    assert len(db.trade_journal.rows) == 1
    assert db.trade_journal.rows[0]["paper_trade_id"] == "WIN-id"
    assert db.trade_journal.rows[0]["T3_HIT"] is True
    assert db.trade_journal.rows[0]["partial_exits"]["partial_exit_3"]["percent"] == 34
    assert db.trade_journal.rows[0]["partial_exits"]["partial_exit_3"]["quantity"] == 4
    assert second["updated_count"] == 0
    assert len(db.trade_journal.rows) == 1


def test_market_data_before_setup_does_not_trigger_waiting_entry() -> None:
    waiting = lifecycle_trade("LATESETUP", created_at="2026-06-16T11:00:00")
    db = FakeDb(trades=[waiting], market_rows=[market_row("LATESETUP", high=105.0, low=95.0, close=104.0)])

    response = asyncio.run(paper.run_automatic_outcome_update(db_override=db))

    row = db.paper_trades.rows[0]
    assert response["updated_count"] == 0
    assert response["results"][0]["reason"] in ("DATA_INSUFFICIENT", "NO_STATUS_CHANGE")
    assert row["status"] == "WAITING_FOR_ENTRY"
    assert db.paper_trades.update_calls == []
    assert len(db.paper_market_snapshots.rows) == 1


def test_entry_high_before_setup_is_ignored_even_when_day_high_crosses_entry() -> None:
    waiting = lifecycle_trade("BEFOREHIGH", created_at="2026-06-16T11:00:00")
    db = FakeDb(
        trades=[waiting],
        market_rows=[market_row("BEFOREHIGH", high=150.0, low=95.0, close=99.0, updated_at="2026-06-16T12:00:00")],
        snapshots=[
            snapshot_row(
                "BEFOREHIGH",
                observed_at="2026-06-16T10:00:00",
                high=150.0,
                low=99.0,
                close=105.0,
            )
        ],
    )

    response = asyncio.run(paper.run_automatic_outcome_update(db_override=db))

    row = db.paper_trades.rows[0]
    assert response["updated_count"] == 0
    assert response["results"][0]["reason"] == "NO_STATUS_CHANGE"
    assert row["status"] == "WAITING_FOR_ENTRY"
    assert row.get("entry_triggered") is False


def test_entry_high_after_setup_activates_waiting_trade() -> None:
    waiting = lifecycle_trade("AFTERHIGH", created_at="2026-06-16T09:00:00")
    db = FakeDb(
        trades=[waiting],
        snapshots=[
            snapshot_row(
                "AFTERHIGH",
                observed_at="2026-06-16T10:00:00",
                high=101.0,
                low=99.0,
                close=101.0,
            )
        ],
    )

    response = asyncio.run(paper.run_automatic_outcome_update(db_override=db))

    row = db.paper_trades.rows[0]
    assert response["updated_count"] == 1
    assert row["status"] == "ACTIVE"
    assert row["outcome_status"] == "ACTIVE"
    assert row["entry_triggered"] is True


def test_missing_post_setup_history_keeps_waiting_with_data_insufficient_reason() -> None:
    waiting = lifecycle_trade("NOHISTORY", created_at="2026-06-16T11:00:00")
    db = FakeDb(trades=[waiting])

    response = asyncio.run(paper.run_automatic_outcome_update(db_override=db))

    row = db.paper_trades.rows[0]
    assert response["updated_count"] == 0
    assert response["results"][0]["reason"] == "DATA_INSUFFICIENT"
    assert row["status"] == "WAITING_FOR_ENTRY"
    assert row.get("entry_triggered") is False


def test_active_entry_reaudit_reverts_only_unproven_plain_active_trades() -> None:
    active = lifecycle_trade("BADACTIVE", "ACTIVE", created_at="2026-06-16T09:00:00")
    partial = lifecycle_trade("PARTIALSAFE", "T1_PARTIAL", created_at="2026-06-16T09:00:00")
    completed = lifecycle_trade("DONESAFE", "COMPLETED", created_at="2026-06-16T09:00:00")
    db = FakeDb(
        trades=[active, partial, completed],
        snapshots=[snapshot_row("BADACTIVE", observed_at="2026-06-16T10:00:00", high=99.0, low=95.0, close=99.0)],
    )

    response = asyncio.run(paper.reaudit_active_entry_evidence(db, apply=True))

    assert response["audited_count"] == 1
    assert response["updated_count"] == 1
    assert next(row for row in db.paper_trades.rows if row["symbol"] == "BADACTIVE")["status"] == "WAITING_FOR_ENTRY"
    assert next(row for row in db.paper_trades.rows if row["symbol"] == "PARTIALSAFE")["status"] == "T1_PARTIAL"
    assert next(row for row in db.paper_trades.rows if row["symbol"] == "DONESAFE")["status"] == "COMPLETED"


def test_waiting_trade_audit_fixes_post_setup_missed_entry() -> None:
    waiting = lifecycle_trade("MISSED", created_at="2026-06-16T09:00:00")
    db = FakeDb(trades=[waiting], market_rows=[market_row("MISSED", high=101.0, low=95.0, close=101.0)])

    response = asyncio.run(paper.audit_and_fix_waiting_trades(db, apply=True))

    row = db.paper_trades.rows[0]
    assert response["audited_count"] == 1
    assert response["would_update_count"] == 1
    assert response["updated_count"] == 1
    assert response["affected_trades"][0]["symbol"] == "MISSED"
    assert response["affected_trades"][0]["post_setup_high"] == 101.0
    assert row["status"] == "ACTIVE"
    assert row["outcome_status"] == "ACTIVE"
    assert row["entry_triggered"] is True
    assert row["state_version"] == 2


def test_grouped_paper_trade_apis_return_normalized_rows(monkeypatch) -> None:
    waiting = lifecycle_trade("WAITING", "PLANNED")
    active = lifecycle_trade("PARTIAL", "T1_PARTIAL")
    completed = lifecycle_trade("WIN", "COMPLETED")
    completed["outcome_status"] = "T3_HIT"
    stopped = lifecycle_trade("STOP", "SL_HIT")
    ambiguous = lifecycle_trade("AMB", "AMBIGUOUS")
    signal = {**trade_ready_row("SIGNAL"), "paper_only": True, "signal_type": "SWING_TV_CONFIRMED", "created_at": "2026-06-16T09:00:00"}
    db = FakeDb(trades=[waiting, active, completed, stopped, ambiguous], signals=[signal])
    monkeypatch.setattr(paper, "get_database", lambda: db)

    open_payload = asyncio.run(paper.get_open_paper_trades(limit=100))
    history_payload = asyncio.run(paper.get_paper_trade_history(limit=100))
    pipeline_payload = asyncio.run(paper.get_paper_pipeline_details(limit=100))

    assert open_payload["waiting_for_entry"][0]["ui_status"] == "Waiting for Entry"
    assert open_payload["active_partial"][0]["ui_status"] == "Partial"
    assert history_payload["completed"][0]["symbol"] == "WIN"
    assert history_payload["sl_hit"][0]["ui_status"] == "Stopped"
    assert history_payload["ambiguous"][0]["ui_status"] == "Ambiguous"
    assert pipeline_payload["paper_signals"][0]["strategy"] == "Swing"
    assert pipeline_payload["paper_plans"]
