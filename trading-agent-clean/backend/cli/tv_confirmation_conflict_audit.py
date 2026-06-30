from __future__ import annotations

import argparse
import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings
from services.mongo_indexes import (
    TV_CONFIRMATION_BASE_IDENTITY_FIELDS,
    TV_CONFIRMATION_UNIQUE_POLICY,
    get_collection_index_specs,
)
from services.migration_safety import safe_json_value
from services.tv_confirmation_conflicts import (
    AUDIT_PROJECTION,
    TV_CONFIRMATION_COLLECTIONS,
    classify_rows,
    summarize_collections,
    utc_now_iso,
)


EXPECTED_DATABASES = {"trading_agent_clean", "wave0d1_isolated_fake_db", "wave0d2_isolated_fake_db"}
FORBIDDEN_COLLECTION_METHODS = (
    "insert_one",
    "insert_many",
    "update_one",
    "update_many",
    "replace_one",
    "delete_one",
    "delete_many",
    "bulk_write",
    "find_one_and_update",
    "create_index",
    "drop_index",
    "drop",
)


class AuditSafetyError(RuntimeError):
    pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only TV confirmation uniqueness conflict audit.")
    parser.add_argument("--output", help="Optional JSON report output path.")
    parser.add_argument("--database-name", default=settings.DATABASE_NAME, help="Database name to audit. URI is never printed.")
    parser.add_argument(
        "--confirm-database",
        action="store_true",
        help="Allow auditing a database name outside the known local/test names.",
    )
    return parser.parse_args(argv)


def validate_database_name(database_name: str, *, confirm_database: bool = False) -> None:
    if database_name not in EXPECTED_DATABASES and not confirm_database:
        raise AuditSafetyError(
            f"Refusing unexpected database name {database_name!r}. Re-run with --confirm-database if this is intentional."
        )


def _maybe_await(value: Any) -> Any:
    return value


async def maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


class ReadOnlyCollection:
    def __init__(self, collection: Any) -> None:
        self._collection = collection

    def __getattr__(self, name: str) -> Any:
        if name in FORBIDDEN_COLLECTION_METHODS:
            raise AssertionError(f"write/index method {name} is forbidden during TV confirmation audit")
        return getattr(self._collection, name)

    def find(self, *args, **kwargs):
        return self._collection.find(*args, **kwargs)

    async def count_documents(self, *args, **kwargs):
        return await maybe_await(self._collection.count_documents(*args, **kwargs))

    def list_indexes(self, *args, **kwargs):
        return self._collection.list_indexes(*args, **kwargs)


async def cursor_to_list(cursor: Any) -> list[dict[str, Any]]:
    if hasattr(cursor, "to_list"):
        return [dict(row) for row in await cursor.to_list(length=None)]
    if hasattr(cursor, "__aiter__"):
        return [dict(row) async for row in cursor]
    return [dict(row) for row in cursor]


async def latest_updated_at(collection: ReadOnlyCollection) -> Any:
    cursor = collection.find({"updated_at": {"$exists": True}}, {"updated_at": 1, "_id": 0}).sort("updated_at", -1).limit(1)
    rows = await cursor_to_list(cursor)
    return rows[0].get("updated_at") if rows else None


async def collection_evidence(collection: ReadOnlyCollection) -> dict[str, Any]:
    indexes = await cursor_to_list(collection.list_indexes())
    return {
        "document_count": await collection.count_documents({}),
        "index_names": sorted(str(index.get("name")) for index in indexes if index.get("name")),
        "latest_updated_at": safe_json_value(await latest_updated_at(collection)),
    }


async def read_rows(collection: ReadOnlyCollection) -> list[dict[str, Any]]:
    cursor = collection.find({}, AUDIT_PROJECTION)
    return await cursor_to_list(cursor)


async def build_audit_report(db: Any, *, database_name: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    validate_database_name(database_name, confirm_database=True)
    collections = {}
    read_only_proof = {}
    for collection_name in TV_CONFIRMATION_COLLECTIONS:
        collection = ReadOnlyCollection(db[collection_name] if hasattr(db, "__getitem__") else getattr(db, collection_name))
        before = await collection_evidence(collection)
        rows = await read_rows(collection)
        result = classify_rows(collection_name, rows)
        after = await collection_evidence(collection)
        collections[collection_name] = result
        read_only_proof[collection_name] = {
            "before": before,
            "after": after,
            "document_count_unchanged": before["document_count"] == after["document_count"],
            "index_names_unchanged": before["index_names"] == after["index_names"],
            "latest_updated_at_unchanged": before["latest_updated_at"] == after["latest_updated_at"],
        }

    totals = summarize_collections(collections)
    return {
        "schema_version": 1,
        "audit_name": "tv_confirmation_conflict_audit",
        "created_at": utc_now_iso(),
        "database_name": database_name,
        "collections": safe_json_value(collections),
        "totals": safe_json_value(totals),
        "blocking_conflicts_exist": totals["blocking_conflict_count"] > 0,
        "canonical_policy": safe_json_value(
            {
                "base_identity_fields": list(TV_CONFIRMATION_BASE_IDENTITY_FIELDS),
                "unique_policy": TV_CONFIRMATION_UNIQUE_POLICY,
                "swing_index_specs": [spec.as_dict() for spec in get_collection_index_specs("swing_tv_confirmations", critical=True)],
                "momentum_index_specs": [spec.as_dict() for spec in get_collection_index_specs("momentum_tv_confirmations", critical=True)],
            }
        ),
        "read_only_proof": safe_json_value(read_only_proof),
        "zero_writes_performed": True,
        "index_operations_performed": 0,
        "context": safe_json_value(context or {}),
    }


def write_json(path: str | None, payload: dict[str, Any]) -> None:
    if not path:
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(safe_json_value(payload), indent=2, sort_keys=True), encoding="utf-8")


def sanitized_console_summary(report: dict[str, Any]) -> str:
    lines = [
        "TV confirmation conflict audit",
        f"database={report['database_name']}",
        f"blocking_conflicts_exist={report['blocking_conflicts_exist']}",
        f"blocking_conflict_count={report['totals']['blocking_conflict_count']}",
    ]
    for collection_name, result in report["collections"].items():
        lines.append(
            f"{collection_name}: rows={result['row_count']} "
            f"non_technical_duplicate_groups={len(result['non_technical_duplicate_groups'])} "
            f"technical_failure_duplicate_groups={len(result['technical_failure_duplicate_groups'])} "
            f"missing_failure_id={result['classification_counts']['TECHNICAL_FAILURE_MISSING_FAILURE_ID']} "
            f"malformed={result['classification_counts']['MALFORMED_BASE_IDENTITY']}"
        )
    return "\n".join(lines)


async def run_live_audit(args: argparse.Namespace) -> dict[str, Any]:
    validate_database_name(args.database_name, confirm_database=args.confirm_database)
    from motor.motor_asyncio import AsyncIOMotorClient

    client = AsyncIOMotorClient(settings.MONGO_URI)
    try:
        db = client[args.database_name]
        return await build_audit_report(db, database_name=args.database_name)
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        report = asyncio.run(run_live_audit(args))
        write_json(args.output, report)
        print(sanitized_console_summary(report))
        return 2 if report["blocking_conflicts_exist"] else 0
    except AuditSafetyError as exc:
        print(f"TV_CONFIRMATION_AUDIT_REFUSED: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
