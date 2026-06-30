from __future__ import annotations

import hashlib
import json
import os
import subprocess
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PLAN_SCHEMA_VERSION = 1
BACKUP_MANIFEST_SCHEMA_VERSION = 1
ABSENT = {"__absent__": True}
MIGRATION_APPLY_TIMESTAMP = "MIGRATION_APPLY_TIME"


class MigrationSafetyError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): safe_json_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [safe_json_value(item) for item in value]
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def safe_json_dumps(value: Any) -> str:
    return json.dumps(safe_json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def parse_timestamp(value: Any, *, field_name: str) -> datetime:
    try:
        text = str(value or "").strip()
        if not text:
            raise ValueError("empty")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception as exc:
        raise MigrationSafetyError(
            "MIGRATION_INVALID_TIMESTAMP",
            f"{field_name} must be a valid ISO timestamp.",
            {"field": field_name},
        ) from exc


def repository_head_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        value = result.stdout.strip()
        return value or None
    except Exception:
        return None


def document_id_type(value: Any) -> str:
    return type(value).__name__


def deserialize_document_id(value: Any, value_type: str | None = None) -> Any:
    if value_type == "ObjectId":
        try:
            from bson import ObjectId

            return ObjectId(str(value))
        except Exception:
            return value
    return value


def field_precondition(document: dict[str, Any], field: str) -> dict[str, Any]:
    exists = field in document
    return {
        "field": field,
        "exists": exists,
        "value": safe_json_value(document.get(field)) if exists else deepcopy(ABSENT),
    }


def field_change(document: dict[str, Any], field: str, after: Any, action: str) -> dict[str, Any]:
    before_exists = field in document
    after_exists = action != "unset"
    return {
        "field": field,
        "action": action,
        "before": safe_json_value(document.get(field)) if before_exists else deepcopy(ABSENT),
        "after": safe_json_value(after) if after_exists else deepcopy(ABSENT),
        "before_exists": before_exists,
        "after_exists": after_exists,
    }


def build_update_operation(
    *,
    collection: str,
    document: dict[str, Any],
    set_fields: dict[str, Any] | None = None,
    unset_fields: list[str] | tuple[str, ...] | set[str] | None = None,
    eligibility_fields: list[str] | tuple[str, ...] | set[str] | None = None,
    operation_type: str = "update_fields",
) -> dict[str, Any]:
    set_fields = dict(set_fields or {})
    unset_fields = sorted(str(field) for field in (unset_fields or []))
    change_fields = sorted(set(set_fields) | set(unset_fields))
    precondition_fields = sorted(set(change_fields) | {str(field) for field in (eligibility_fields or [])})
    document_id = document.get("_id")
    changes = [field_change(document, field, set_fields[field], "set") for field in sorted(set_fields)]
    changes.extend(field_change(document, field, None, "unset") for field in unset_fields)
    operation = {
        "operation_id": "",
        "collection": collection,
        "document_id": safe_json_value(document_id),
        "document_id_type": document_id_type(document_id),
        "operation_type": operation_type,
        "changes": sorted(changes, key=lambda item: (item["field"], item["action"])),
        "precondition": {
            "document_id": safe_json_value(document_id),
            "fields": [field_precondition(document, field) for field in precondition_fields],
        },
        "update": {
            "$set": safe_json_value(set_fields),
            "$unset": {field: "" for field in unset_fields},
        },
    }
    operation["operation_id"] = hashlib.sha256(
        safe_json_dumps(
            {
                "collection": operation["collection"],
                "document_id": operation["document_id"],
                "operation_type": operation["operation_type"],
                "changes": operation["changes"],
                "precondition": operation["precondition"],
            }
        ).encode("utf-8")
    ).hexdigest()[:24]
    return operation


def sort_operations(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        operations,
        key=lambda op: (
            str(op.get("collection") or ""),
            str(op.get("document_id") or ""),
            str(op.get("operation_type") or ""),
            str(op.get("operation_id") or ""),
        ),
    )


def operation_hash_content(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": plan.get("schema_version"),
        "migration_name": plan.get("migration_name"),
        "target_database": plan.get("target_database"),
        "operations": sort_operations(list(plan.get("operations") or [])),
        "index_actions": sorted(
            list(plan.get("index_actions") or []),
            key=lambda action: (
                str(action.get("collection") or ""),
                str(action.get("action") or ""),
                str(action.get("name") or ""),
            ),
        ),
    }


def compute_plan_hash(plan: dict[str, Any]) -> str:
    return hashlib.sha256(safe_json_dumps(operation_hash_content(plan)).encode("utf-8")).hexdigest()


def build_preview_plan(
    *,
    migration_name: str,
    target_database: str,
    operations: list[dict[str, Any]],
    index_actions: list[dict[str, Any]] | None = None,
    created_at: str | None = None,
    repo_head: str | None = None,
) -> dict[str, Any]:
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "migration_name": migration_name,
        "target_database": target_database,
        "created_at": created_at or utc_now_iso(),
        "repo_head": repo_head if repo_head is not None else repository_head_sha(),
        "apply": False,
        "operations": sort_operations([safe_json_value(op) for op in operations]),
        "index_actions": safe_json_value(index_actions or []),
    }
    plan["operation_count"] = len(plan["operations"])
    plan["document_count"] = len({(op.get("collection"), str(op.get("document_id"))) for op in plan["operations"]})
    plan["index_action_count"] = len(plan["index_actions"])
    plan["operation_content_sha256"] = compute_plan_hash(plan)
    plan["zero_writes_performed"] = True
    return plan


def plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
    field_changes = []
    for operation in plan.get("operations") or []:
        for change in operation.get("changes") or []:
            field_changes.append(
                {
                    "collection": operation.get("collection"),
                    "document_id": operation.get("document_id"),
                    "field": change.get("field"),
                    "before": change.get("before"),
                    "after": change.get("after"),
                    "action": change.get("action"),
                }
            )
    return {
        "apply": False,
        "migration_name": plan.get("migration_name"),
        "target_database": plan.get("target_database"),
        "document_count": plan.get("document_count", 0),
        "operation_count": plan.get("operation_count", 0),
        "index_action_count": plan.get("index_action_count", 0),
        "proposed_field_changes": field_changes,
        "plan_hash": plan.get("operation_content_sha256"),
        "zero_writes_performed": True,
        "plan": plan,
    }


def write_plan_output(plan: dict[str, Any], path: str | os.PathLike | None) -> None:
    if not path:
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(safe_json_value(plan), indent=2, sort_keys=True), encoding="utf-8")


def load_plan_file(path: str | os.PathLike | None) -> dict[str, Any]:
    if not path:
        raise MigrationSafetyError("MIGRATION_PLAN_FILE_REQUIRED", "--plan-file is required in apply mode.")
    plan_path = Path(path)
    if not plan_path.exists():
        raise MigrationSafetyError("MIGRATION_PLAN_FILE_REQUIRED", "Plan file does not exist.", {"path": str(plan_path)})
    try:
        return json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise MigrationSafetyError("MIGRATION_PLAN_INVALID", "Plan file must be valid JSON.") from exc


def validate_plan(
    plan: dict[str, Any],
    *,
    migration_name: str,
    target_database: str,
    confirm_hash: str | None,
    allow_empty: bool = False,
) -> dict[str, Any]:
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise MigrationSafetyError("MIGRATION_PLAN_UNSUPPORTED", "Unsupported migration plan schema version.")
    if plan.get("migration_name") != migration_name:
        raise MigrationSafetyError("MIGRATION_PLAN_MISMATCH", "Plan migration name does not match this CLI.")
    if plan.get("target_database") != target_database:
        raise MigrationSafetyError("MIGRATION_TARGET_DATABASE_MISMATCH", "Plan target database does not match configured target.")
    if not confirm_hash:
        raise MigrationSafetyError("MIGRATION_PLAN_HASH_REQUIRED", "--confirm-plan-sha256 is required in apply mode.")
    actual_hash = compute_plan_hash(plan)
    if actual_hash != confirm_hash or plan.get("operation_content_sha256") != actual_hash:
        raise MigrationSafetyError(
            "MIGRATION_PLAN_HASH_MISMATCH",
            "Confirmed plan hash does not match canonical operation content.",
            {"expected": plan.get("operation_content_sha256"), "actual": actual_hash},
        )
    if not allow_empty and not plan.get("operations") and not plan.get("index_actions"):
        raise MigrationSafetyError("MIGRATION_PLAN_EMPTY", "Apply mode requires a non-empty approved plan.")
    for operation in plan.get("operations") or []:
        if not operation.get("precondition", {}).get("fields"):
            raise MigrationSafetyError("MIGRATION_PRECONDITION_REQUIRED", "Every operation must include stale-write preconditions.")
    return plan


def validate_backup_manifest(
    path: str | os.PathLike | None,
    *,
    target_database: str,
    plan_created_at: str | None,
) -> dict[str, Any]:
    if not path:
        raise MigrationSafetyError("MIGRATION_BACKUP_MANIFEST_REQUIRED", "--backup-manifest is required in apply mode.")
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise MigrationSafetyError("MIGRATION_BACKUP_MANIFEST_REQUIRED", "Backup manifest does not exist.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise MigrationSafetyError("MIGRATION_BACKUP_MANIFEST_INVALID", "Backup manifest must be valid JSON.") from exc
    required = {"schema_version", "database_name", "created_at", "backup_location", "collections", "verified_by_operator"}
    if not required <= set(manifest):
        raise MigrationSafetyError(
            "MIGRATION_BACKUP_MANIFEST_INVALID",
            "Backup manifest is missing required fields.",
            {"missing": sorted(required - set(manifest))},
        )
    if not (manifest.get("backup_id") or manifest.get("backup_hash")):
        raise MigrationSafetyError("MIGRATION_BACKUP_MANIFEST_INVALID", "Backup manifest requires backup_id or backup_hash.")
    if manifest.get("schema_version") != BACKUP_MANIFEST_SCHEMA_VERSION:
        raise MigrationSafetyError("MIGRATION_BACKUP_MANIFEST_INVALID", "Unsupported backup manifest schema version.")
    if manifest.get("database_name") != target_database:
        raise MigrationSafetyError("MIGRATION_BACKUP_DATABASE_MISMATCH", "Backup manifest database does not match target database.")
    if not str(manifest.get("backup_location") or "").strip():
        raise MigrationSafetyError("MIGRATION_BACKUP_MANIFEST_INVALID", "Backup location must not be empty.")
    if manifest.get("verified_by_operator") is not True:
        raise MigrationSafetyError("MIGRATION_BACKUP_NOT_VERIFIED", "Backup manifest must be verified by the operator.")
    if not isinstance(manifest.get("collections"), list) or not manifest["collections"]:
        raise MigrationSafetyError("MIGRATION_BACKUP_MANIFEST_INVALID", "Backup manifest collections must be a non-empty list.")
    backup_time = parse_timestamp(manifest.get("created_at"), field_name="backup.created_at")
    if plan_created_at:
        plan_time = parse_timestamp(plan_created_at, field_name="plan.created_at")
        if backup_time < plan_time:
            raise MigrationSafetyError("MIGRATION_BACKUP_TOO_OLD", "Backup manifest is older than the approved plan.")
    return safe_json_value(manifest)


def validate_maintenance(
    *,
    maintenance_approved: bool,
    runtime_metadata_path: str | os.PathLike | None = None,
) -> None:
    if maintenance_approved is not True:
        raise MigrationSafetyError(
            "MIGRATION_MAINTENANCE_REQUIRED",
            "Apply mode requires explicit maintenance approval.",
        )
    if not runtime_metadata_path:
        return
    path = Path(runtime_metadata_path)
    if not path.exists():
        return
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise MigrationSafetyError("MIGRATION_RUNTIME_METADATA_INVALID", "Runtime metadata must be valid JSON.") from exc
    active = metadata.get("application_active") is True or metadata.get("workers_active") is True
    maintenance_mode = metadata.get("maintenance_mode") is True
    if active and not maintenance_mode:
        raise MigrationSafetyError(
            "MIGRATION_MAINTENANCE_REQUIRED",
            "Application runtime appears active and maintenance mode is not confirmed.",
        )


def load_and_validate_apply_controls(
    *,
    args: Any,
    migration_name: str,
    target_database: str,
) -> dict[str, Any]:
    validate_maintenance(
        maintenance_approved=bool(getattr(args, "maintenance_approved", False)),
        runtime_metadata_path=getattr(args, "runtime_metadata", None),
    )
    plan = load_plan_file(getattr(args, "plan_file", None))
    plan = validate_plan(
        plan,
        migration_name=migration_name,
        target_database=target_database,
        confirm_hash=getattr(args, "confirm_plan_sha256", None),
    )
    backup = validate_backup_manifest(
        getattr(args, "backup_manifest", None),
        target_database=target_database,
        plan_created_at=plan.get("created_at"),
    )
    return {"plan": plan, "backup_manifest": backup}


def build_cas_filter(operation: dict[str, Any]) -> dict[str, Any]:
    query = {
        "_id": deserialize_document_id(operation.get("document_id"), operation.get("document_id_type")),
    }
    for condition in operation.get("precondition", {}).get("fields") or []:
        field = condition["field"]
        if condition.get("exists") is True:
            query[field] = condition.get("value")
        else:
            query[field] = {"$exists": False}
    return query


def operation_already_satisfied(document: dict[str, Any] | None, operation: dict[str, Any]) -> bool:
    if not document:
        return False
    for change in operation.get("changes") or []:
        field = change["field"]
        if change.get("action") == "unset":
            if field in document:
                return False
        elif safe_json_value(document.get(field)) != change.get("after"):
            return False
    return True


def collection_for(db: Any, collection_name: str) -> Any:
    if hasattr(db, "__getitem__"):
        try:
            return db[collection_name]
        except Exception:
            pass
    return getattr(db, collection_name)


async def apply_index_action(db: Any, action: dict[str, Any]) -> dict[str, Any]:
    collection = collection_for(db, action["collection"])
    if action.get("action") == "create_index":
        keys = [tuple(item) for item in action.get("keys") or []]
        kwargs = dict(action.get("options") or {})
        if action.get("name") and "name" not in kwargs:
            kwargs["name"] = action["name"]
        await collection.create_index(keys, **kwargs)
        return {"status": "APPLIED", "action": action.get("action"), "collection": action.get("collection"), "name": action.get("name")}
    if action.get("action") == "drop_index":
        await collection.drop_index(action["name"])
        return {"status": "APPLIED", "action": action.get("action"), "collection": action.get("collection"), "name": action.get("name")}
    return {"status": "BLOCKED", "reason": "UNKNOWN_INDEX_ACTION", "action": action}


async def apply_migration_plan(db: Any, plan: dict[str, Any]) -> dict[str, Any]:
    planned = len(plan.get("operations") or [])
    applied: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    already_satisfied: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for operation in plan.get("operations") or []:
        collection = collection_for(db, operation["collection"])
        query = build_cas_filter(operation)
        update = deepcopy(operation.get("update") or {})
        try:
            result = await collection.update_one(query, update, upsert=False)
            matched = int(getattr(result, "matched_count", getattr(result, "modified_count", 0)) or 0)
            modified = int(getattr(result, "modified_count", 0) or 0)
            entry = {
                "collection": operation["collection"],
                "document_id": operation["document_id"],
                "operation_id": operation["operation_id"],
                "changed_fields": [change["field"] for change in operation.get("changes") or []],
            }
            if matched > 0:
                if modified > 0:
                    applied.append(entry)
                else:
                    already_satisfied.append({**entry, "status": "ALREADY_SATISFIED"})
                continue
            current = None
            if hasattr(collection, "find_one"):
                current = await collection.find_one({"_id": deserialize_document_id(operation.get("document_id"), operation.get("document_id_type"))})
            if operation_already_satisfied(current, operation):
                already_satisfied.append({**entry, "status": "ALREADY_SATISFIED"})
            else:
                stale.append({**entry, "status": "STALE_SKIPPED"})
        except Exception as exc:
            failed.append(
                {
                    "collection": operation.get("collection"),
                    "document_id": operation.get("document_id"),
                    "operation_id": operation.get("operation_id"),
                    "reason": exc.__class__.__name__,
                }
            )

    index_attempted: list[dict[str, Any]] = []
    index_blocked: list[dict[str, Any]] = []
    for action in plan.get("index_actions") or []:
        try:
            index_attempted.append(await apply_index_action(db, action))
        except Exception as exc:
            index_blocked.append({"action": action, "reason": exc.__class__.__name__})

    return {
        "ok": not failed and not stale,
        "apply": True,
        "plan_hash_used": plan.get("operation_content_sha256"),
        "planned_operations": planned,
        "applied_operations": len(applied),
        "stale_skipped": len(stale),
        "already_satisfied": len(already_satisfied),
        "failed": len(failed),
        "index_operations_attempted": len(index_attempted),
        "index_operations_blocked": len(index_blocked),
        "applied": applied,
        "stale": stale,
        "already_satisfied_details": already_satisfied,
        "failures": failed,
        "index_attempted": index_attempted,
        "index_blocked": index_blocked,
        "rollback_guidance": "Restore from the verified backup manifest and generate a fresh preview before retrying.",
        "requires_fresh_preview": bool(stale),
    }
