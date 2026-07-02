import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes import momentum, swing


def matches(row: dict, query: dict) -> bool:
    for key, expected in query.items():
        actual = row.get(key)
        if isinstance(expected, dict) and "$ne" in expected:
            if actual == expected["$ne"]:
                return False
        elif actual != expected:
            return False
    return True


class FakeCollection:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.update_calls = []

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        self.update_calls.append((query, update, upsert))
        existing = next((row for row in self.rows if matches(row, query)), None)
        if existing:
            existing.update(update.get("$set", {}))
            for field in update.get("$unset", {}):
                existing.pop(field, None)
            return SimpleNamespace(matched_count=1, modified_count=1, upserted_id=None)
        if not upsert:
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)
        inserted = {**{k: v for k, v in query.items() if not isinstance(v, dict)}, **update.get("$setOnInsert", {}), **update.get("$set", {})}
        inserted.setdefault("_id", f"id-{len(self.rows) + 1}")
        self.rows.append(inserted)
        return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=inserted["_id"])


class FakeDb:
    def __init__(self):
        self.swing_tv_confirmations = FakeCollection()
        self.momentum_tv_confirmations = FakeCollection()


def confirmation_row(symbol="TEST", status="CONFIRMED_SIGNAL"):
    return {
        "symbol": symbol,
        "tradingview_symbol": f"NSE:{symbol}",
        "index_name": "BROAD_MARKET_750",
        "timeframes_hash": "abc123",
        "tv_status": status,
        "tv_confirmed": status != "REJECTED",
        "timeframe_debug": {
            "1D": {"last_candle_time": "2026-07-01T09:15:00Z"},
            "1H": {"last_candle_time": "2026-07-01T10:15:00Z"},
        },
        "calculation_timestamp": "2026-07-01T13:00:00.000000Z",
    }


def test_swing_confirmation_write_uses_aware_confirmation_timestamps(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(swing, "get_database", lambda: db)
    monkeypatch.setattr(swing, "utc_now_iso", lambda: "2026-07-01T13:10:55.376000Z")

    asyncio.run(swing.save_confirmation_row(confirmation_row("SWING")))

    stored = db.swing_tv_confirmations.rows[0]
    assert stored["created_at"] == "2026-07-01T13:10:55.376000Z"
    assert stored["updated_at"] == "2026-07-01T13:10:55.376000Z"
    assert stored["confirmed_at"] == "2026-07-01T13:10:55.376000Z"
    assert stored["swing_confirmed_at"] == "2026-07-01T13:10:55.376000Z"
    assert stored.get("momentum_confirmed_at") is None
    assert stored["source_candle_at"] == "2026-07-01T10:15:00.000000Z"
    assert stored["calculation_timestamp"] == "2026-07-01T13:00:00.000000Z"
    assert "created_at" not in db.swing_tv_confirmations.update_calls[0][1]["$set"]


def test_reconfirmation_preserves_created_at_and_updates_confirmed_and_updated(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(swing, "get_database", lambda: db)
    times = iter(["2026-06-11T15:53:58.632000Z", "2026-07-01T13:10:55.376000Z"])
    monkeypatch.setattr(swing, "utc_now_iso", lambda: next(times))

    asyncio.run(swing.save_confirmation_row(confirmation_row("RETRY")))
    asyncio.run(swing.save_confirmation_row(confirmation_row("RETRY", status="WAIT_FOR_RETEST")))

    assert len(db.swing_tv_confirmations.rows) == 1
    stored = db.swing_tv_confirmations.rows[0]
    assert stored["created_at"] == "2026-06-11T15:53:58.632000Z"
    assert stored["confirmed_at"] == "2026-07-01T13:10:55.376000Z"
    assert stored["swing_confirmed_at"] == "2026-07-01T13:10:55.376000Z"
    assert stored["updated_at"] == "2026-07-01T13:10:55.376000Z"
    assert db.swing_tv_confirmations.update_calls[1][2] is True
    assert db.swing_tv_confirmations.update_calls[1][0]["tv_status"] == {"$ne": "TECHNICAL_FAILED"}


def test_swing_and_momentum_confirmation_timestamps_are_independent(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(swing, "get_database", lambda: db)
    monkeypatch.setattr(momentum, "get_database", lambda: db)
    monkeypatch.setattr(swing, "utc_now_iso", lambda: "2026-07-01T13:10:55.376000Z")
    monkeypatch.setattr(momentum, "utc_now_iso", lambda: "2026-07-01T13:12:00.000000Z")

    asyncio.run(swing.save_confirmation_row(confirmation_row("BOTH")))
    asyncio.run(momentum.save_momentum_confirmation_row({**confirmation_row("BOTH", "MOMENTUM_CONFIRMED")}))

    swing_row = db.swing_tv_confirmations.rows[0]
    momentum_row = db.momentum_tv_confirmations.rows[0]
    assert swing_row["swing_confirmed_at"] == "2026-07-01T13:10:55.376000Z"
    assert swing_row.get("momentum_confirmed_at") is None
    assert momentum_row["momentum_confirmed_at"] == "2026-07-01T13:12:00.000000Z"
    assert momentum_row.get("swing_confirmed_at") is None


def test_tv_confirmation_upsert_identity_remains_status_aware(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(momentum, "get_database", lambda: db)
    monkeypatch.setattr(momentum, "utc_now_iso", lambda: "2026-07-01T13:12:00.000000Z")

    asyncio.run(momentum.save_momentum_confirmation_row(confirmation_row("ID", "MOMENTUM_CONFIRMED")))
    query, update, upsert = db.momentum_tv_confirmations.update_calls[0]

    assert query == {
        "symbol": "ID",
        "tradingview_symbol": "NSE:ID",
        "index_name": "BROAD_MARKET_750",
        "timeframes_hash": "abc123",
        "tv_status": {"$ne": "TECHNICAL_FAILED"},
    }
    assert upsert is True
    assert update["$setOnInsert"] == {"created_at": "2026-07-01T13:12:00.000000Z"}
    assert update["$unset"] == {"trade_allowed": ""}


def test_serialization_exposes_named_timestamp_fields_without_created_fallback():
    row = swing.serialize_swing_confirmation_row({
        "symbol": "OLD",
        "created_at": "2026-06-11T15:53:58.632000",
        "updated_at": "2026-07-01T13:10:55.376000Z",
        "source_candle_at": "2026-07-01T10:15:00Z",
    })

    assert set(swing.TIMESTAMP_FIELDS) <= set(row)
    assert row["confirmed_at"] is None
    assert row["swing_confirmed_at"] is None
    assert row["created_at"] == "2026-06-11T15:53:58.632000"
    assert row["source_candle_at"] == "2026-07-01T10:15:00Z"
