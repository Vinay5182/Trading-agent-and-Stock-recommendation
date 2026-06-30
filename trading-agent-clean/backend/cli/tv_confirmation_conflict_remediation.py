from __future__ import annotations

import argparse
import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli.tv_confirmation_conflict_audit import AuditSafetyError, build_audit_report, validate_database_name
from config import settings
from services.migration_safety import (
    MigrationSafetyError,
    apply_migration_plan,
    build_preview_plan,
    load_and_validate_apply_controls,
    safe_json_value,
    write_plan_output,
)
from services.mongo_indexes import TECHNICAL_TV_STATUS, TV_CONFIRMATION_BASE_IDENTITY_FIELDS
from services.tv_confirmation_conflicts import build_remediation_operations


MIGRATION_NAME = "wave0d2_tv_confirmation_conflict_remediation_preview"
ALLOWED_COLLECTIONS = {"swing_tv_confirmations", "momentum_tv_confirmations"}
ALLOWED_FAILURE_PREFIX = "wave0d2:"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview/apply remediation plan for TV confirmation uniqueness conflicts.")
    parser.add_argument("--plan-output", help="JSON plan output path.")
    parser.add_argument("--database-name", default=settings.DATABASE_NAME, help="Database name to audit. URI is never printed.")
    parser.add_argument("--apply", action="store_true", help="Apply an approved plan with full migration safety controls.")
    parser.add_argument("--maintenance-approved", action="store_true", help="Required explicit approval for apply mode.")
    parser.add_argument("--plan-file", help="Approved plan JSON file for apply mode.")
    parser.add_argument("--confirm-plan-sha256", help="Canonical approved plan hash for apply mode.")
    parser.add_argument("--backup-manifest", help="Verified backup manifest for apply mode.")
    parser.add_argument("--runtime-metadata", help="Optional runtime metadata checked by migration safety controls.")
    parser.add_argument("--result-output", help="Optional JSON apply result output path.")
    parser.add_argument(
        "--confirm-database",
        action="store_true",
        help="Allow auditing a database name outside the known local/test names.",
    )
    return parser.parse_args(argv)


async def build_preview(db: Any, *, database_name: str, audit: dict[str, Any] | None = None) -> dict[str, Any]:
    audit = audit or await build_audit_report(db, database_name=database_name)
    operations, manual_review = build_remediation_operations(audit)
    plan = build_preview_plan(
        migration_name=MIGRATION_NAME,
        target_database=database_name,
        operations=operations,
        index_actions=[],
    )
    plan["source_audit"] = {
        "blocking_conflicts_exist": audit.get("blocking_conflicts_exist"),
        "totals": audit.get("totals"),
    }
    plan["manual_review_groups"] = safe_json_value(manual_review)
    plan["deletions_proposed"] = 0
    plan["zero_writes_performed"] = True
    return plan


def validate_wave0d2_apply_plan(plan: dict[str, Any]) -> None:
    if plan.get("deletions_proposed", 0) != 0:
        raise MigrationSafetyError("TV_CONFIRMATION_PLAN_FORBIDS_DELETIONS", "Wave 0D2 plan must not propose deletions.")
    if plan.get("index_actions"):
        raise MigrationSafetyError("TV_CONFIRMATION_PLAN_FORBIDS_INDEX_ACTIONS", "Wave 0D2 plan must not include index actions.")
    if plan.get("manual_review_groups"):
        raise MigrationSafetyError("TV_CONFIRMATION_PLAN_HAS_MANUAL_REVIEW", "Manual-review groups must be resolved before apply.")
    for operation in plan.get("operations") or []:
        if operation.get("collection") not in ALLOWED_COLLECTIONS:
            raise MigrationSafetyError("TV_CONFIRMATION_PLAN_COLLECTION_FORBIDDEN", "Unexpected collection in Wave 0D2 plan.")
        if operation.get("operation_type") != "set_missing_technical_failure_run_id":
            raise MigrationSafetyError("TV_CONFIRMATION_PLAN_OPERATION_FORBIDDEN", "Unexpected operation type in Wave 0D2 plan.")
        update = operation.get("update") or {}
        set_fields = update.get("$set") or {}
        unset_fields = update.get("$unset") or {}
        if set(set_fields) != {"failure_run_id"} or unset_fields:
            raise MigrationSafetyError("TV_CONFIRMATION_PLAN_FIELD_FORBIDDEN", "Only failure_run_id may be set in Wave 0D2 apply.")
        failure_run_id = set_fields.get("failure_run_id")
        expected_prefix = f"{ALLOWED_FAILURE_PREFIX}{operation['collection']}:{operation.get('document_id')}"
        if failure_run_id != expected_prefix:
            raise MigrationSafetyError("TV_CONFIRMATION_PLAN_FAILURE_ID_MISMATCH", "failure_run_id does not match deterministic Wave 0D2 policy.")
        changed_fields = [change.get("field") for change in operation.get("changes") or []]
        if changed_fields != ["failure_run_id"]:
            raise MigrationSafetyError("TV_CONFIRMATION_PLAN_CHANGE_FORBIDDEN", "Plan changes fields other than failure_run_id.")
        preconditions = {condition.get("field"): condition for condition in operation.get("precondition", {}).get("fields") or []}
        required_preconditions = {*TV_CONFIRMATION_BASE_IDENTITY_FIELDS, "tv_status", "failure_run_id"}
        if not required_preconditions <= set(preconditions):
            raise MigrationSafetyError("TV_CONFIRMATION_PLAN_PRECONDITION_INCOMPLETE", "Plan is missing required compare-and-set fields.")
        if preconditions["tv_status"].get("value") != TECHNICAL_TV_STATUS:
            raise MigrationSafetyError("TV_CONFIRMATION_PLAN_STATUS_FORBIDDEN", "Plan must only touch TECHNICAL_FAILED rows.")
        if preconditions["failure_run_id"].get("exists") is not False:
            raise MigrationSafetyError("TV_CONFIRMATION_PLAN_FAILURE_ID_PRECONDITION_REQUIRED", "failure_run_id must still be absent.")


def plan_with_live_document_id_types(plan: dict[str, Any]) -> dict[str, Any]:
    adjusted = deepcopy(plan)
    for operation in adjusted.get("operations") or []:
        document_id = operation.get("document_id")
        if isinstance(document_id, str) and len(document_id) == 24:
            operation["document_id_type"] = "ObjectId"
    return adjusted


async def apply_approved_plan(db: Any, *, args: argparse.Namespace) -> dict[str, Any]:
    controls = load_and_validate_apply_controls(
        args=args,
        migration_name=MIGRATION_NAME,
        target_database=args.database_name,
    )
    plan = controls["plan"]
    validate_wave0d2_apply_plan(plan)
    result = await apply_migration_plan(db, plan_with_live_document_id_types(plan))
    result["backup_manifest"] = controls["backup_manifest"]
    result["deletions"] = 0
    result["manual_review_groups"] = len(plan.get("manual_review_groups") or [])
    result["index_actions"] = len(plan.get("index_actions") or [])
    result["successful_operations"] = result["applied_operations"] + result["already_satisfied"]
    result["success"] = (
        result["successful_operations"] == result["planned_operations"]
        and result["stale_skipped"] == 0
        and result["failed"] == 0
        and result["deletions"] == 0
        and result["index_actions"] == 0
    )
    return result


async def run_live_preview(args: argparse.Namespace) -> dict[str, Any]:
    validate_database_name(args.database_name, confirm_database=args.confirm_database)
    from motor.motor_asyncio import AsyncIOMotorClient

    client = AsyncIOMotorClient(settings.MONGO_URI)
    try:
        db = client[args.database_name]
        if args.apply:
            return await apply_approved_plan(db, args=args)
        return await build_preview(db, database_name=args.database_name)
    finally:
        client.close()


def write_json(path: str | None, payload: dict[str, Any]) -> None:
    if not path:
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(safe_json_value(payload), indent=2, sort_keys=True), encoding="utf-8")


def console_summary(plan: dict[str, Any]) -> str:
    if plan.get("apply") is True:
        return "\n".join(
            [
                "TV confirmation remediation apply",
                f"planned_operations={plan['planned_operations']}",
                f"applied_operations={plan['applied_operations']}",
                f"already_satisfied={plan['already_satisfied']}",
                f"stale_skipped={plan['stale_skipped']}",
                f"failed={plan['failed']}",
                f"plan_hash_used={plan['plan_hash_used']}",
                f"success={plan['success']}",
            ]
        )
    return "\n".join(
        [
            "TV confirmation remediation preview",
            f"database={plan['target_database']}",
            f"operation_count={plan['operation_count']}",
            f"manual_review_groups={len(plan.get('manual_review_groups') or [])}",
            f"plan_hash={plan['operation_content_sha256']}",
            "zero_writes_performed=True",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        plan = asyncio.run(run_live_preview(args))
        if args.apply:
            write_json(args.result_output, plan)
        else:
            write_plan_output(plan, args.plan_output)
        print(console_summary(plan))
        return 0 if not args.apply or plan.get("success") else 2
    except AuditSafetyError as exc:
        print(f"TV_CONFIRMATION_PREVIEW_REFUSED: {exc}", file=sys.stderr)
        return 3
    except MigrationSafetyError as exc:
        print(f"TV_CONFIRMATION_APPLY_REFUSED: {exc.code}: {exc.message}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
