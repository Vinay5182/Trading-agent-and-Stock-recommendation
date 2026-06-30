import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from pymongo.errors import DuplicateKeyError


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.system_errors import (  # noqa: E402
    generate_system_error_dedup_key,
    record_system_error,
    system_error_dedup_payload,
)


def run(coro):
    return asyncio.run(coro)


class FakeSystemErrors:
    def __init__(self, *, raise_duplicate_once: bool = False) -> None:
        self.rows = []
        self.update_calls = []
        self.raise_duplicate_once = raise_duplicate_once

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        self.update_calls.append((query, update, upsert))
        if self.raise_duplicate_once:
            self.raise_duplicate_once = False
            raise DuplicateKeyError("duplicate dedup_key")
        row = next((candidate for candidate in self.rows if all(candidate.get(key) == value for key, value in query.items())), None)
        if row is None:
            if not upsert:
                return SimpleNamespace(modified_count=0, matched_count=0, upserted_id=None)
            row = {**query, **update.get("$setOnInsert", {})}
            self.rows.append(row)
        row.update(update.get("$set", {}))
        for key, value in update.get("$inc", {}).items():
            row[key] = row.get(key, 0) + value
        return SimpleNamespace(modified_count=1, matched_count=1, upserted_id=None)


def fake_db(collection: FakeSystemErrors) -> SimpleNamespace:
    return SimpleNamespace(system_errors=collection)


def test_dedup_key_is_sha256_of_canonical_payload_with_null_missing_identity() -> None:
    missing_doc = {
        "component": "scheduler",
        "operation": "sync",
        "exception_type": "RuntimeError",
        "exception_message": "boom",
    }
    explicit_null_doc = {
        "component": "scheduler",
        "operation": "sync",
        "trade_id": None,
        "setup_id": None,
        "symbol": None,
        "strategy_type": None,
        "scheduler_job": None,
        "exception_type": "RuntimeError",
        "exception_message": "boom",
    }

    missing_key = generate_system_error_dedup_key(missing_doc)
    explicit_null_key = generate_system_error_dedup_key(explicit_null_doc)

    assert missing_key == explicit_null_key
    assert len(missing_key) == 64
    assert system_error_dedup_payload(missing_doc)["identity"]["trade_id"] is None


def test_record_system_error_assigns_dedup_key_and_deduplicates_identical_errors() -> None:
    collection = FakeSystemErrors()
    db = fake_db(collection)

    first = run(record_system_error(db, component="scheduler", operation="sync", scheduler_job="trade_ready", exception=RuntimeError("boom")))
    second = run(record_system_error(db, component="scheduler", operation="sync", scheduler_job="trade_ready", exception=RuntimeError("boom")))

    assert first["dedup_key"] == second["dedup_key"]
    assert len(collection.rows) == 1
    assert collection.rows[0]["dedup_key"] == first["dedup_key"]
    assert collection.rows[0]["dedup_key_version"] == 2
    assert collection.rows[0]["occurrence_count"] == 2
    assert collection.update_calls[0][0] == {"dedup_key": first["dedup_key"]}


def test_record_system_error_keeps_different_errors_separate() -> None:
    collection = FakeSystemErrors()
    db = fake_db(collection)

    first = run(record_system_error(db, component="scheduler", operation="sync", exception=RuntimeError("boom")))
    second = run(record_system_error(db, component="scheduler", operation="sync", exception=RuntimeError("different")))

    assert first["dedup_key"] != second["dedup_key"]
    assert len(collection.rows) == 2
    assert {row["exception_message"] for row in collection.rows} == {"boom", "different"}


def test_record_system_error_duplicate_key_retry_increments_existing_row() -> None:
    collection = FakeSystemErrors(raise_duplicate_once=True)
    db = fake_db(collection)
    dedup_key = generate_system_error_dedup_key(
        {
            "component": "scheduler",
            "operation": "sync",
            "trade_id": None,
            "setup_id": None,
            "symbol": None,
            "strategy_type": None,
            "scheduler_job": None,
            "exception_type": "RuntimeError",
            "exception_message": "boom",
        }
    )
    collection.rows.append({"dedup_key": dedup_key, "occurrence_count": 1})

    retry = run(record_system_error(db, component="scheduler", operation="sync", exception=RuntimeError("boom")))

    assert retry["dedup_key"] == dedup_key
    assert len(collection.update_calls) == 2
    assert collection.update_calls[-1][2] is False
    assert collection.rows[0]["occurrence_count"] == 2
