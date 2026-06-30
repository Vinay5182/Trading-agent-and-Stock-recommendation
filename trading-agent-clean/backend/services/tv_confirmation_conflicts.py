from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from services.mongo_indexes import (
    TECHNICAL_TV_STATUS,
    TV_CONFIRMATION_BASE_IDENTITY_FIELDS,
    TV_CONFIRMATION_UNIQUE_POLICY,
)
from services.migration_safety import build_update_operation, safe_json_value


TV_CONFIRMATION_COLLECTIONS = ("swing_tv_confirmations", "momentum_tv_confirmations")
SAFE_DETAIL_FIELDS = (
    "_id",
    *TV_CONFIRMATION_BASE_IDENTITY_FIELDS,
    "tv_status",
    "failure_run_id",
    "created_at",
    "updated_at",
    "confirmed_at",
    "confirmation_timestamp",
    "timestamp",
    "checked_at",
)
AUDIT_PROJECTION = {field: 1 for field in SAFE_DETAIL_FIELDS}

CLASSIFICATIONS = (
    "VALID_NON_TECHNICAL_UNIQUE",
    "NON_TECHNICAL_DUPLICATE_GROUP",
    "VALID_TECHNICAL_FAILURE",
    "TECHNICAL_FAILURE_DUPLICATE_FAILURE_ID",
    "TECHNICAL_FAILURE_MISSING_FAILURE_ID",
    "MALFORMED_BASE_IDENTITY",
    "STATUS_OUTSIDE_INDEX_PREDICATE",
    "LEGACY_OR_UNKNOWN_STATUS",
)
BLOCKING_CLASSIFICATIONS = {
    "NON_TECHNICAL_DUPLICATE_GROUP",
    "TECHNICAL_FAILURE_DUPLICATE_FAILURE_ID",
    "TECHNICAL_FAILURE_MISSING_FAILURE_ID",
    "MALFORMED_BASE_IDENTITY",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def non_technical_statuses(collection_name: str) -> set[str]:
    return set(TV_CONFIRMATION_UNIQUE_POLICY["non_technical_filter"][collection_name]["tv_status"]["$in"])


def all_known_statuses() -> set[str]:
    statuses = {TECHNICAL_TV_STATUS}
    for status_filter in TV_CONFIRMATION_UNIQUE_POLICY["non_technical_filter"].values():
        statuses.update(status_filter["tv_status"]["$in"])
    return statuses


def canonical_identity(row: dict[str, Any]) -> dict[str, Any]:
    return {field: row.get(field) for field in TV_CONFIRMATION_BASE_IDENTITY_FIELDS}


def identity_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(field) for field in TV_CONFIRMATION_BASE_IDENTITY_FIELDS)


def malformed_identity_fields(row: dict[str, Any]) -> list[str]:
    malformed = []
    for field in TV_CONFIRMATION_BASE_IDENTITY_FIELDS:
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            malformed.append(field)
    return malformed


def safe_document_id(row: dict[str, Any]) -> str | None:
    value = row.get("_id")
    return str(value) if value is not None else None


def confirmation_timestamp(row: dict[str, Any]) -> Any:
    for field in ("confirmed_at", "confirmation_timestamp", "timestamp", "checked_at", "updated_at", "created_at"):
        if row.get(field) is not None:
            return row.get(field)
    return None


def safe_row_detail(row: dict[str, Any], *, missing_fields: list[str] | None = None) -> dict[str, Any]:
    return {
        "document_id": safe_document_id(row),
        "identity": safe_json_value(canonical_identity(row)),
        "tv_status": row.get("tv_status"),
        "failure_run_id": row.get("failure_run_id"),
        "failure_run_id_present": isinstance(row.get("failure_run_id"), str) and bool(row.get("failure_run_id")),
        "created_at": safe_json_value(row.get("created_at")),
        "updated_at": safe_json_value(row.get("updated_at")),
        "confirmation_timestamp": safe_json_value(confirmation_timestamp(row)),
        "missing_or_malformed_fields": list(missing_fields or []),
    }


def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (str(row.get("updated_at") or ""), str(row.get("created_at") or ""), safe_document_id(row) or ""))


def classify_rows(collection_name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if collection_name not in TV_CONFIRMATION_COLLECTIONS:
        raise ValueError(f"Unsupported TV confirmation collection: {collection_name}")

    collection_non_technical = non_technical_statuses(collection_name)
    known_statuses = all_known_statuses()
    non_technical_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    technical_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    classified: list[dict[str, Any]] = []

    for row in rows:
        malformed = malformed_identity_fields(row)
        status = str(row.get("tv_status") or "").upper()
        if malformed:
            classified.append({"classification": "MALFORMED_BASE_IDENTITY", "row": row, "missing_fields": malformed})
        elif status in collection_non_technical:
            non_technical_groups[identity_key(row)].append(row)
        elif status == TECHNICAL_TV_STATUS:
            failure_run_id = row.get("failure_run_id")
            if not isinstance(failure_run_id, str) or not failure_run_id:
                classified.append({"classification": "TECHNICAL_FAILURE_MISSING_FAILURE_ID", "row": row, "missing_fields": ["failure_run_id"]})
            else:
                technical_groups[(*identity_key(row), failure_run_id)].append(row)
        elif status in known_statuses:
            classified.append({"classification": "STATUS_OUTSIDE_INDEX_PREDICATE", "row": row, "missing_fields": []})
        else:
            classified.append({"classification": "LEGACY_OR_UNKNOWN_STATUS", "row": row, "missing_fields": []})

    duplicate_groups = []
    for key, group in sorted(non_technical_groups.items(), key=lambda item: tuple(str(part) for part in item[0])):
        if len(group) > 1:
            group_rows = _sort_rows(group)
            duplicate_groups.append(
                {
                    "collection": collection_name,
                    "classification": "NON_TECHNICAL_DUPLICATE_GROUP",
                    "identity": dict(zip(TV_CONFIRMATION_BASE_IDENTITY_FIELDS, key)),
                    "record_count": len(group_rows),
                    "document_ids": [safe_document_id(row) for row in group_rows],
                    "rows": [safe_row_detail(row) for row in group_rows],
                }
            )
            for row in group_rows:
                classified.append({"classification": "NON_TECHNICAL_DUPLICATE_GROUP", "row": row, "missing_fields": []})
        else:
            classified.append({"classification": "VALID_NON_TECHNICAL_UNIQUE", "row": group[0], "missing_fields": []})

    technical_duplicate_groups = []
    for key, group in sorted(technical_groups.items(), key=lambda item: tuple(str(part) for part in item[0])):
        if len(group) > 1:
            identity = dict(zip(TV_CONFIRMATION_BASE_IDENTITY_FIELDS, key[: len(TV_CONFIRMATION_BASE_IDENTITY_FIELDS)]))
            failure_run_id = key[-1]
            group_rows = _sort_rows(group)
            technical_duplicate_groups.append(
                {
                    "collection": collection_name,
                    "classification": "TECHNICAL_FAILURE_DUPLICATE_FAILURE_ID",
                    "identity": identity,
                    "failure_run_id": failure_run_id,
                    "record_count": len(group_rows),
                    "document_ids": [safe_document_id(row) for row in group_rows],
                    "rows": [safe_row_detail(row) for row in group_rows],
                }
            )
            for row in group_rows:
                classified.append({"classification": "TECHNICAL_FAILURE_DUPLICATE_FAILURE_ID", "row": row, "missing_fields": []})
        else:
            classified.append({"classification": "VALID_TECHNICAL_FAILURE", "row": group[0], "missing_fields": []})

    classification_counts = Counter(item["classification"] for item in classified)
    blocking_rows = [item for item in classified if item["classification"] in BLOCKING_CLASSIFICATIONS]
    blocking_details = []
    for item in sorted(blocking_rows, key=lambda entry: (entry["classification"], safe_document_id(entry["row"]) or "")):
        blocking_details.append(
            {
                "collection": collection_name,
                "classification": item["classification"],
                "record_count": 1,
                **safe_row_detail(item["row"], missing_fields=item.get("missing_fields") or []),
            }
        )

    return {
        "collection": collection_name,
        "row_count": len(rows),
        "classification_counts": {name: classification_counts.get(name, 0) for name in CLASSIFICATIONS},
        "blocking_conflict_count": len(blocking_rows),
        "non_technical_duplicate_groups": duplicate_groups,
        "technical_failure_duplicate_groups": technical_duplicate_groups,
        "blocking_details": blocking_details,
        "manual_review_groups": build_manual_review_groups(collection_name, duplicate_groups, technical_duplicate_groups, classified),
        "rows": [
            {
                "collection": collection_name,
                "classification": item["classification"],
                **safe_row_detail(item["row"], missing_fields=item.get("missing_fields") or []),
            }
            for item in sorted(classified, key=lambda entry: (safe_document_id(entry["row"]) or "", entry["classification"]))
        ],
    }


def build_manual_review_groups(
    collection_name: str,
    non_technical_duplicate_groups: list[dict[str, Any]],
    technical_duplicate_groups: list[dict[str, Any]],
    classified: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups = []
    for group in non_technical_duplicate_groups:
        statuses = sorted({str(row.get("tv_status") or "") for row in group.get("rows", [])})
        groups.append(
            {
                "collection": collection_name,
                "classification": "NON_TECHNICAL_DUPLICATE_GROUP",
                "remediation_policy": "MANUAL_REVIEW",
                "reason": "Duplicate non-technical authoritative outcomes require operator review before superseded/archive marking.",
                "identity": group["identity"],
                "document_ids": group["document_ids"],
                "statuses": statuses,
            }
        )
    for group in technical_duplicate_groups:
        groups.append(
            {
                "collection": collection_name,
                "classification": "TECHNICAL_FAILURE_DUPLICATE_FAILURE_ID",
                "remediation_policy": "MANUAL_REVIEW",
                "reason": "Repeated technical failure_run_id values may represent duplicate attempts or collapsed retries.",
                "identity": group["identity"],
                "document_ids": group["document_ids"],
                "failure_run_id": group.get("failure_run_id"),
            }
        )
    for item in classified:
        if item["classification"] == "MALFORMED_BASE_IDENTITY":
            groups.append(
                {
                    "collection": collection_name,
                    "classification": "MALFORMED_BASE_IDENTITY",
                    "remediation_policy": "MANUAL_REVIEW",
                    "reason": "Base identity cannot be fabricated without a deterministic source field.",
                    "identity": canonical_identity(item["row"]),
                    "document_ids": [safe_document_id(item["row"])],
                    "missing_or_malformed_fields": item.get("missing_fields") or [],
                }
            )
    return groups


def deterministic_failure_run_id(collection_name: str, document_id: Any) -> str:
    return f"wave0d2:{collection_name}:{document_id}"


def build_remediation_operations(audit: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    operations = []
    manual_review = []
    for collection in audit.get("collections", {}).values():
        manual_review.extend(collection.get("manual_review_groups") or [])
        for row in collection.get("rows") or []:
            if row.get("classification") != "TECHNICAL_FAILURE_MISSING_FAILURE_ID":
                continue
            document = {
                "_id": row.get("document_id"),
                **{field: row.get("identity", {}).get(field) for field in TV_CONFIRMATION_BASE_IDENTITY_FIELDS},
                "tv_status": row.get("tv_status"),
            }
            if row.get("failure_run_id_present"):
                document["failure_run_id"] = row.get("failure_run_id")
            operations.append(
                build_update_operation(
                    collection=collection["collection"],
                    document=document,
                    set_fields={"failure_run_id": deterministic_failure_run_id(collection["collection"], row.get("document_id"))},
                    eligibility_fields=[*TV_CONFIRMATION_BASE_IDENTITY_FIELDS, "tv_status", "failure_run_id"],
                    operation_type="set_missing_technical_failure_run_id",
                )
            )
    return operations, manual_review


def summarize_collections(collections: dict[str, dict[str, Any]]) -> dict[str, Any]:
    totals = {name: 0 for name in CLASSIFICATIONS}
    for result in collections.values():
        for name, count in (result.get("classification_counts") or {}).items():
            totals[name] = totals.get(name, 0) + int(count or 0)
    return {
        "classification_counts": totals,
        "blocking_conflict_count": sum(int(result.get("blocking_conflict_count") or 0) for result in collections.values()),
        "non_technical_duplicate_groups": sum(len(result.get("non_technical_duplicate_groups") or []) for result in collections.values()),
        "technical_failure_duplicate_groups": sum(len(result.get("technical_failure_duplicate_groups") or []) for result in collections.values()),
        "manual_review_groups": sum(len(result.get("manual_review_groups") or []) for result in collections.values()),
    }
