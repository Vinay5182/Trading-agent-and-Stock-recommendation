from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Add backend directory to sys.path to allow imports from database and services
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings
from database import close_mongo_connection, connect_to_mongo, get_database
from services.migration_safety import (
    MigrationSafetyError,
    apply_migration_plan,
    build_preview_plan,
    build_update_operation,
    load_and_validate_apply_controls,
    plan_summary,
    safe_json_value,
    write_plan_output,
)
from services.paper_sync import is_current_schema, is_trade_ready_saved_row

MIGRATION_NAME = "cleanup_legacy_trade_allowed"
LEGACY_CLEANUP_PRECONDITION_FIELDS = {
    "trade_allowed",
    "tv_status",
    "status",
    "final_status",
    "paper_plan_valid",
    "trade_quality_grade",
    "paper_entry_price",
    "entry_price",
    "entry",
    "paper_stop_loss",
    "stop_loss",
    "sl",
    "paper_target_1",
    "target_1",
    "paper_target_2",
    "target_2",
    "paper_target_3",
    "target_3",
    "risk_summary",
    "trap_status",
    "updated_at",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cleanup legacy trade_allowed field for eligible current-schema confirmations")
    parser.add_argument("--apply", action="store_true", help="Unset trade_allowed: false for eligible current-schema documents")
    parser.add_argument("--plan-output", help="Write preview plan JSON to this path")
    parser.add_argument("--plan-file", help="Approved preview plan JSON for apply mode")
    parser.add_argument("--confirm-plan-sha256", help="Expected canonical preview plan SHA-256")
    parser.add_argument("--backup-manifest", help="Verified backup manifest JSON")
    parser.add_argument("--maintenance-approved", action="store_true", help="Confirm an operator-approved maintenance window")
    parser.add_argument("--runtime-metadata", help="Optional local runtime metadata JSON used to detect active workers")
    return parser.parse_args(argv)


async def build_cleanup_preview_plan(db) -> dict:
    operations = []
    affected_items = []
    total_scanned = 0
    for collection_name in ("swing_tv_confirmations", "momentum_tv_confirmations"):
        strategy = "swing" if "swing" in collection_name else "momentum"
        cursor = db[collection_name].find({"trade_allowed": False})
        async for doc in cursor:
            total_scanned += 1
            if is_current_schema(doc) and is_trade_ready_saved_row(doc):
                affected_items.append({
                    "collection": collection_name,
                    "_id": str(doc["_id"]),
                    "symbol": doc.get("symbol"),
                    "strategy": strategy,
                    "reason": "eligible current-schema confirmation blocked by stale trade_allowed=false"
                })
                operations.append(build_update_operation(
                    collection=collection_name,
                    document=doc,
                    unset_fields=["trade_allowed"],
                    eligibility_fields=LEGACY_CLEANUP_PRECONDITION_FIELDS,
                    operation_type="unset_legacy_trade_allowed",
                ))
    plan = build_preview_plan(
        migration_name=MIGRATION_NAME,
        target_database=getattr(db, "name", settings.DATABASE_NAME),
        operations=operations,
    )
    summary = plan_summary(plan)
    summary.update(
        {
            "total_scanned_blocked": total_scanned,
            "total_identified_eligible": len(affected_items),
            "total_modified": 0,
            "identified_confirmations": affected_items,
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
            result = await apply_migration_plan(db_override, controls["plan"])
            return safe_json_value({**result, "total_modified": result["applied_operations"]})
        preview = await build_cleanup_preview_plan(db_override)
        write_plan_output(preview["plan"], getattr(args, "plan_output", None))
        return safe_json_value(preview)

    await connect_to_mongo()
    try:
        db = get_database()
        if args.apply:
            result = await apply_migration_plan(db, controls["plan"])
            return safe_json_value({**result, "total_modified": result["applied_operations"]})
        preview = await build_cleanup_preview_plan(db)
        write_plan_output(preview["plan"], getattr(args, "plan_output", None))
        return safe_json_value(preview)
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
