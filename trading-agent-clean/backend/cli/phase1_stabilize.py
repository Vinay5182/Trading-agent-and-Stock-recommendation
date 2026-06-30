from __future__ import annotations

import argparse
import asyncio
import json

from config import settings
from database import close_mongo_connection, connect_to_mongo, get_database
from services.migration_safety import (
    MIGRATION_APPLY_TIMESTAMP,
    MigrationSafetyError,
    apply_migration_plan,
    build_preview_plan,
    build_update_operation,
    load_and_validate_apply_controls,
    plan_summary,
    safe_json_value,
    write_plan_output,
)
from services.paper_migration import (
    OBSOLETE_PAPER_TRADE_INDEXES,
    _archived_setup_id,
    _changed_fields,
    migration_setup_identity,
    migration_setup_identity_fields,
    most_advanced_trade,
)
from services.paper_identity import setup_id_from_identity
from services.mongo_indexes import get_index_spec


EXPECTED_SETUP_INDEX = get_index_spec("paper_trades", "paper_trades_setup_id_unique_v1").partial_filter
MIGRATION_NAME = "phase1_stabilize"
PHASE1_PRECONDITION_FIELDS = {
    "paper_only",
    "symbol",
    "canonical_symbol",
    "tradingview_symbol",
    "source_signal_type",
    "source_collection",
    "source_confirmation_id",
    "timeframe",
    "status",
    "outcome_status",
    "state",
    "entry_price",
    "stop_loss",
    "setup_id",
    "setup_identity",
    "canonical_setup_id",
    "duplicate_of_setup_id",
    "state_version",
    "updated_at",
    "created_at",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Paper-trading stabilization migration and verification")
    parser.add_argument("--apply", action="store_true", help="Apply setup_id migration and journal backfill")
    parser.add_argument("--limit", type=int, default=None, help="Optional paper_trades scan limit")
    parser.add_argument("--journal-limit", type=int, default=5000, help="Completed paper_trades journal backfill limit")
    parser.add_argument("--plan-output", help="Write preview plan JSON to this path")
    parser.add_argument("--plan-file", help="Approved preview plan JSON for apply mode")
    parser.add_argument("--confirm-plan-sha256", help="Expected canonical preview plan SHA-256")
    parser.add_argument("--backup-manifest", help="Verified backup manifest JSON")
    parser.add_argument("--maintenance-approved", action="store_true", help="Confirm an operator-approved maintenance window")
    parser.add_argument("--runtime-metadata", help="Optional local runtime metadata JSON used to detect active workers")
    return parser.parse_args(argv)


async def collection_names(db) -> set[str]:
    return set(await db.list_collection_names())


async def index_names_and_defs(collection) -> dict:
    indexes = {}
    async for index in collection.list_indexes():
        indexes[index["name"]] = dict(index)
    return indexes


async def validate_indexes(db) -> dict:
    names = await collection_names(db)
    paper_indexes = await index_names_and_defs(db.paper_trades) if "paper_trades" in names else {}
    journal_indexes = await index_names_and_defs(db.trade_journal) if "trade_journal" in names else {}
    setup_index = paper_indexes.get("paper_trades_setup_id_unique_v1")
    journal_index = journal_indexes.get("trade_journal_paper_trade_id_unique")
    return {
        "paper_trades_exists": "paper_trades" in names,
        "trade_journal_exists": "trade_journal" in names,
        "setup_index_exists": setup_index is not None,
        "setup_index_unique": bool(setup_index and setup_index.get("unique")),
        "setup_index_partial_filter": setup_index.get("partialFilterExpression") if setup_index else None,
        "setup_index_filter_valid": bool(
            setup_index
            and setup_index.get("unique")
            and setup_index.get("partialFilterExpression") == EXPECTED_SETUP_INDEX
        ),
        "trade_journal_unique_index_exists": journal_index is not None,
        "trade_journal_unique_index_unique": bool(journal_index and journal_index.get("unique")),
        "paper_trade_index_names": sorted(paper_indexes),
        "trade_journal_index_names": sorted(journal_indexes),
    }


async def build_phase1_preview_plan(db, *, limit: int | None = None) -> dict:
    collection = getattr(db, "paper_trades", None)
    if collection is None:
        return build_preview_plan(migration_name=MIGRATION_NAME, target_database=db.name, operations=[])

    cursor = collection.find({"paper_only": True}).sort("updated_at", -1)
    if limit is not None:
        cursor = cursor.limit(limit)

    groups: dict[str, list[dict]] = {}
    scanned = 0
    already_archived = 0
    failures = []
    async for trade in cursor:
        scanned += 1
        if trade.get("duplicate_of_setup_id"):
            already_archived += 1
            continue
        identity = migration_setup_identity(trade)
        if not identity:
            failures.append({"trade_id": str(trade.get("_id")), "reason": "MISSING_SETUP_IDENTITY"})
            continue
        groups.setdefault(setup_id_from_identity(identity), []).append(trade)

    operations = []
    duplicate_groups = 0
    for setup_id, trades in groups.items():
        winner = most_advanced_trade(trades)
        duplicate_groups += 1 if len(trades) > 1 else 0
        for trade in trades:
            identity_fields = migration_setup_identity_fields(trade)
            if not identity_fields:
                failures.append({"trade_id": str(trade.get("_id")), "reason": "MISSING_SETUP_IDENTITY"})
                continue
            is_winner = trade.get("_id") == winner.get("_id")
            update_fields = {
                **identity_fields,
                "state_version": trade.get("state_version", 1),
            }
            if not trade.get("setup_migrated_at"):
                update_fields["setup_migrated_at"] = MIGRATION_APPLY_TIMESTAMP
            if not is_winner:
                update_fields.update(
                    {
                        "setup_id": _archived_setup_id(setup_id, trade),
                        "canonical_setup_id": setup_id,
                        "duplicate_of_setup_id": setup_id,
                        "duplicate_of_paper_trade_id": str(winner.get("_id")),
                        "duplicate_resolution": "ARCHIVED_DUPLICATE_RETAINED_HISTORY",
                        "duplicate_archived_at": MIGRATION_APPLY_TIMESTAMP,
                    }
                )
                if isinstance(update_fields.get("setup_identity"), dict):
                    update_fields["setup_identity"] = {
                        **update_fields["setup_identity"],
                        "archived_duplicate": True,
                    }
            changed_fields = _changed_fields(trade, update_fields)
            if not changed_fields:
                continue
            operations.append(build_update_operation(
                collection="paper_trades",
                document=trade,
                set_fields=changed_fields,
                eligibility_fields=PHASE1_PRECONDITION_FIELDS,
                operation_type="archive_duplicate" if not is_winner else "setup_identity_update",
            ))

    setup_index_spec = get_index_spec("paper_trades", "paper_trades_setup_id_unique_v1")
    index_actions = [
        {
            "action": "create_index",
            "collection": setup_index_spec.collection,
            "name": setup_index_spec.name,
            "keys": [list(key) for key in setup_index_spec.keys],
            "options": {
                "unique": setup_index_spec.unique,
                "partialFilterExpression": setup_index_spec.partial_filter,
            },
        }
    ]
    index_actions.extend(
        {
            "action": "drop_index",
            "collection": "paper_trades",
            "name": name,
        }
        for name in OBSOLETE_PAPER_TRADE_INDEXES
    )
    plan = build_preview_plan(
        migration_name=MIGRATION_NAME,
        target_database=db.name,
        operations=operations,
        index_actions=index_actions,
    )
    summary = plan_summary(plan)
    summary.update(
        {
            "scanned": scanned,
            "already_archived": already_archived,
            "duplicate_groups": duplicate_groups,
            "failures_count": len(failures),
            "failures": failures,
        }
    )
    return summary


async def run(args: argparse.Namespace | None = None, *, db_override=None) -> dict:
    args = args or parse_args()
    target_database = getattr(db_override, "name", settings.DATABASE_NAME)
    controls = None
    if args.apply:
        controls = load_and_validate_apply_controls(
            args=args,
            migration_name=MIGRATION_NAME,
            target_database=target_database,
        )
    if db_override is not None:
        if args.apply:
            setup_result = await apply_migration_plan(db_override, controls["plan"])
            validation = await validate_indexes(db_override)
            return {
                "apply": True,
                "setup_id_migration": setup_result,
                "journal_backfill": None,
                "journal_backfill_skipped": True,
                "index_validation": validation,
            }
        preview = await build_phase1_preview_plan(db_override, limit=args.limit)
        write_plan_output(preview["plan"], getattr(args, "plan_output", None))
        validation = await validate_indexes(db_override)
        return safe_json_value({
            "apply": False,
            "setup_id_migration": preview,
            "journal_backfill": None,
            "journal_backfill_skipped": True,
            "index_validation": validation,
        })

    await connect_to_mongo()
    try:
        db = get_database()
        if args.apply:
            setup_result = await apply_migration_plan(db, controls["plan"])
            validation = await validate_indexes(db)
            return {
                "apply": True,
                "setup_id_migration": setup_result,
                "journal_backfill": None,
                "journal_backfill_skipped": True,
                "index_validation": validation,
            }
        preview = await build_phase1_preview_plan(db, limit=args.limit)
        write_plan_output(preview["plan"], getattr(args, "plan_output", None))
        validation = await validate_indexes(db)
        return safe_json_value({
            "apply": False,
            "setup_id_migration": preview,
            "journal_backfill": None,
            "journal_backfill_skipped": True,
            "index_validation": validation,
        })
    finally:
        await close_mongo_connection()


def main(argv: list[str] | None = None) -> None:
    try:
        result = asyncio.run(run(parse_args(argv)))
    except MigrationSafetyError as exc:
        result = {"ok": False, "code": exc.code, "message": exc.message, "details": exc.details}
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
