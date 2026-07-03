from __future__ import annotations

import asyncio
import hashlib
import inspect
import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable, Mapping

from ai.historical_ohlcv import (
    CURRENT_CANDLE_INCOMPLETE,
    HISTORICAL_OHLCV_NORMALIZATION_VERSION,
    HISTORICAL_OHLCV_SCHEMA_VERSION,
    REQUIRED_CANDLE_KEYS,
    HistoricalOHLCVError,
    candle_identity,
    fetch_historical_ohlcv,
    validate_request_range,
)
from data_provider import normalize_symbol
from services.migration_safety import safe_json_dumps, safe_json_value
from services.timestamps import canonical_utc_iso, utc_now


HISTORICAL_OHLCV_STORE_VERSION = "phase5b1-v1"
HISTORICAL_OHLCV_COLLECTION = "historical_ohlcv"
HISTORICAL_OHLCV_APPLY_APPROVAL = "historical-ohlcv-apply-v1"
HISTORICAL_OHLCV_APPLY_ACK = "insert-only-no-overwrite"
HISTORICAL_OHLCV_PLAN_TTL_SECONDS = 30 * 60
HISTORICAL_OHLCV_APPLY_BATCH_SIZE = 500
HISTORICAL_CONTENT_FINGERPRINT_VERSION = "historical-content-v1"

HISTORICAL_INSERT = "HISTORICAL_INSERT"
HISTORICAL_NOOP_IDENTICAL = "HISTORICAL_NOOP_IDENTICAL"
HISTORICAL_CONFLICT_CONTENT = "HISTORICAL_CONFLICT_CONTENT"
HISTORICAL_CONFLICT_SCHEMA = "HISTORICAL_CONFLICT_SCHEMA"
HISTORICAL_CONFLICT_IDENTITY = "HISTORICAL_CONFLICT_IDENTITY"
HISTORICAL_EXCLUDED_INVALID = "HISTORICAL_EXCLUDED_INVALID"
HISTORICAL_INCOMPLETE_CANDLE = "HISTORICAL_INCOMPLETE_CANDLE"
HISTORICAL_REQUIRED_INDEX_MISSING = "HISTORICAL_REQUIRED_INDEX_MISSING"
HISTORICAL_REQUIRED_INDEX_INCOMPATIBLE = "HISTORICAL_REQUIRED_INDEX_INCOMPATIBLE"
HISTORICAL_PLAN_STALE = "HISTORICAL_PLAN_STALE"
HISTORICAL_PLAN_HASH_MISMATCH = "HISTORICAL_PLAN_HASH_MISMATCH"
HISTORICAL_PLAN_INPUT_CHANGED = "HISTORICAL_PLAN_INPUT_CHANGED"
HISTORICAL_APPROVAL_REQUIRED = "HISTORICAL_APPROVAL_REQUIRED"
HISTORICAL_APPLY_PARTIAL_FAILURE = "HISTORICAL_APPLY_PARTIAL_FAILURE"
HISTORICAL_APPLY_IDEMPOTENT = "HISTORICAL_APPLY_IDEMPOTENT"
HISTORICAL_PLAN_EXPIRED = "HISTORICAL_PLAN_EXPIRED"

CANONICAL_EXACT = "CANONICAL_EXACT"
EQUIVALENT_LEGACY_NAME_ACCEPTED = "EQUIVALENT_LEGACY_NAME_ACCEPTED"
MISSING_BLOCKED = "MISSING_BLOCKED"
SAME_NAME_INCOMPATIBLE = "SAME_NAME_INCOMPATIBLE"
MULTIPLE_EQUIVALENT_INDEXES_AMBIGUOUS = "MULTIPLE_EQUIVALENT_INDEXES_AMBIGUOUS"
COLLECTION_UNAVAILABLE = "COLLECTION_UNAVAILABLE"

REQUIRED_PROVENANCE_FIELDS = {
    "provider",
    "provider_symbol",
    "provider_interval",
    "requested_start",
    "requested_end",
    "source_timezone",
    "timestamp_semantic",
    "adjusted_prices",
    "acquisition_method",
    "acquisition_version",
    "normalization_version",
    "fetched_at",
    "provider_row_fingerprint",
    "validation_status",
    "validation_reason_codes",
}

SAFE_EXAMPLE_FIELDS = {
    "candle_id",
    "exchange",
    "canonical_symbol",
    "timeframe",
    "candle_open_at",
    "candle_close_at",
    "action",
    "code",
    "reason_codes",
    "canonical_content_fingerprint",
    "existing_content_fingerprint",
}


class HistoricalPersistenceError(RuntimeError):
    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.message = message
        self.details = dict(details or {})
        super().__init__(message)


@dataclass(frozen=True)
class HistoricalIndexSpec:
    name: str
    keys: tuple[tuple[str, int], ...]
    unique: bool = False
    purpose: str = ""
    required_for_apply: bool = False

    def create_keys(self) -> list[tuple[str, int]]:
        return list(self.keys)

    def create_options(self) -> dict[str, Any]:
        options = {"name": self.name}
        if self.unique:
            options["unique"] = True
        return options

    def as_dict(self) -> dict[str, Any]:
        return {
            "collection": HISTORICAL_OHLCV_COLLECTION,
            "name": self.name,
            "keys": [list(key) for key in self.keys],
            "unique": self.unique,
            "purpose": self.purpose,
            "required_for_apply": self.required_for_apply,
            "startup_owned": False,
        }


HISTORICAL_REQUIRED_UNIQUE_INDEX = HistoricalIndexSpec(
    name="historical_ohlcv_schema_candle_unique_v1",
    keys=(("schema_version", 1), ("candle_id", 1)),
    unique=True,
    required_for_apply=True,
    purpose="Insert-only historical OHLCV identity guard.",
)

HISTORICAL_QUERY_INDEXES = (
    HistoricalIndexSpec(
        name="historical_ohlcv_symbol_timeframe_open_v1",
        keys=(("exchange", 1), ("canonical_symbol", 1), ("timeframe", 1), ("candle_open_at", 1)),
        purpose="Symbol/timeframe historical range reads.",
    ),
    HistoricalIndexSpec(
        name="historical_ohlcv_timeframe_open_v1",
        keys=(("timeframe", 1), ("candle_open_at", 1)),
        purpose="Timeframe-wide historical range audits.",
    ),
)


def required_historical_index_specs() -> tuple[HistoricalIndexSpec, ...]:
    return (HISTORICAL_REQUIRED_UNIQUE_INDEX, *HISTORICAL_QUERY_INDEXES)


def _utc(value: datetime | None = None) -> datetime:
    raw = value or utc_now()
    if raw.tzinfo is None:
        raw = raw.replace(tzinfo=UTC)
    return raw.astimezone(UTC)


def _parse_utc(value: Any, *, field_name: str) -> datetime:
    try:
        text = str(value or "").strip()
        if not text:
            raise ValueError("empty")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except Exception as exc:
        raise HistoricalPersistenceError(
            HISTORICAL_EXCLUDED_INVALID,
            f"{field_name} must be a valid UTC timestamp.",
            {"field": field_name},
        ) from exc


def _stable_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(safe_json_dumps(value).encode("utf-8")).hexdigest()


def _normalize_numeric(value: Any, *, field: str) -> int | float:
    if isinstance(value, bool):
        raise HistoricalPersistenceError(HISTORICAL_EXCLUDED_INVALID, f"{field} must be numeric.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise HistoricalPersistenceError(HISTORICAL_EXCLUDED_INVALID, f"{field} must be numeric.") from exc
    if not math.isfinite(number):
        raise HistoricalPersistenceError(HISTORICAL_EXCLUDED_INVALID, f"{field} must be finite.")
    return int(number) if number.is_integer() else number


def historical_content_payload(candle: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "fingerprint_version": HISTORICAL_CONTENT_FINGERPRINT_VERSION,
        "schema_version": candle.get("schema_version"),
        "candle_id": candle.get("candle_id"),
        "exchange": candle.get("exchange"),
        "canonical_symbol": candle.get("canonical_symbol"),
        "timeframe": candle.get("timeframe"),
        "candle_open_at": candle.get("candle_open_at"),
        "candle_close_at": candle.get("candle_close_at"),
        "open": _normalize_numeric(candle.get("open"), field="open"),
        "high": _normalize_numeric(candle.get("high"), field="high"),
        "low": _normalize_numeric(candle.get("low"), field="low"),
        "close": _normalize_numeric(candle.get("close"), field="close"),
        "volume": _normalize_numeric(candle.get("volume"), field="volume"),
        "is_closed": candle.get("is_closed") is True,
    }
    if candle.get("adjusted_close") not in (None, ""):
        payload["adjusted_close"] = _normalize_numeric(candle.get("adjusted_close"), field="adjusted_close")
    return payload


def canonical_content_fingerprint(candle: Mapping[str, Any]) -> str:
    return _stable_hash(historical_content_payload(candle))


def _provider_row_fingerprint(candle: Mapping[str, Any]) -> str | None:
    provenance = candle.get("provenance") if isinstance(candle.get("provenance"), Mapping) else {}
    value = provenance.get("provider_row_fingerprint")
    return str(value) if value not in (None, "") else None


def _provenance_complete(candle: Mapping[str, Any]) -> bool:
    provenance = candle.get("provenance") if isinstance(candle.get("provenance"), Mapping) else {}
    return all(field in provenance and provenance.get(field) not in (None, "") for field in REQUIRED_PROVENANCE_FIELDS)


def validate_candle_for_persistence(candle: Mapping[str, Any]) -> list[str]:
    reason_codes: list[str] = []
    missing = sorted(REQUIRED_CANDLE_KEYS - set(candle))
    if missing:
        reason_codes.append(HISTORICAL_EXCLUDED_INVALID)
    if candle.get("schema_version") != HISTORICAL_OHLCV_SCHEMA_VERSION:
        reason_codes.append(HISTORICAL_CONFLICT_SCHEMA)
    if candle.get("is_closed") is not True:
        reason_codes.append(HISTORICAL_INCOMPLETE_CANDLE)
    if not _provenance_complete(candle):
        reason_codes.append(HISTORICAL_EXCLUDED_INVALID)
    try:
        expected_candle_id = candle_identity(
            str(candle.get("exchange") or ""),
            str(candle.get("canonical_symbol") or ""),
            str(candle.get("timeframe") or ""),
            str(candle.get("candle_open_at") or ""),
        )
        if candle.get("candle_id") != expected_candle_id:
            reason_codes.append(HISTORICAL_CONFLICT_IDENTITY)
    except Exception:
        reason_codes.append(HISTORICAL_CONFLICT_IDENTITY)
    quality = candle.get("quality") if isinstance(candle.get("quality"), Mapping) else {}
    quality_codes = set(quality.get("reason_codes") or [])
    if CURRENT_CANDLE_INCOMPLETE in quality_codes:
        reason_codes.append(HISTORICAL_INCOMPLETE_CANDLE)
    try:
        canonical_content_fingerprint(candle)
    except HistoricalPersistenceError:
        reason_codes.append(HISTORICAL_EXCLUDED_INVALID)
    return sorted(set(reason_codes), key=reason_codes.index)


def build_persisted_candle(
    candle: Mapping[str, Any],
    *,
    persistence_run_id: str,
    first_persisted_at: str,
    preview_manifest_hash: str | None,
) -> dict[str, Any]:
    content_fingerprint = canonical_content_fingerprint(candle)
    document = {
        "store_version": HISTORICAL_OHLCV_STORE_VERSION,
        "schema_version": HISTORICAL_OHLCV_SCHEMA_VERSION,
        "candle_id": candle["candle_id"],
        "exchange": candle["exchange"],
        "canonical_symbol": candle["canonical_symbol"],
        "provider": candle["provider"],
        "provider_symbol": candle["provider_symbol"],
        "timeframe": candle["timeframe"],
        "candle_open_at": candle["candle_open_at"],
        "candle_close_at": candle["candle_close_at"],
        "open": candle["open"],
        "high": candle["high"],
        "low": candle["low"],
        "close": candle["close"],
        "volume": candle["volume"],
        "is_closed": True,
        "content_fingerprint_version": HISTORICAL_CONTENT_FINGERPRINT_VERSION,
        "canonical_content_fingerprint": content_fingerprint,
        "provenance": safe_json_value(candle.get("provenance") or {}),
        "quality": safe_json_value(candle.get("quality") or {}),
        "persistence": {
            "store_version": HISTORICAL_OHLCV_STORE_VERSION,
            "persistence_run_id": persistence_run_id,
            "first_persisted_at": first_persisted_at,
            "acquisition_contract_version": candle.get("schema_version"),
            "normalization_version": HISTORICAL_OHLCV_NORMALIZATION_VERSION,
            "source_provider": candle.get("provider"),
            "source_fingerprint": _provider_row_fingerprint(candle),
            "preview_manifest_hash": preview_manifest_hash,
            "overwrite_enabled": False,
            "insert_only": True,
        },
    }
    if candle.get("adjusted_close") not in (None, ""):
        document["adjusted_close"] = candle["adjusted_close"]
    return safe_json_value(document)


def persisted_content_fingerprint(document: Mapping[str, Any]) -> str | None:
    value = document.get("canonical_content_fingerprint")
    if value:
        return str(value)
    try:
        return canonical_content_fingerprint(document)
    except HistoricalPersistenceError:
        return None


def classify_persistence_action(
    candle: Mapping[str, Any],
    *,
    existing_document: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    reason_codes = validate_candle_for_persistence(candle)
    content_fingerprint = None
    if not reason_codes or reason_codes == [HISTORICAL_INCOMPLETE_CANDLE]:
        try:
            content_fingerprint = canonical_content_fingerprint(candle)
        except HistoricalPersistenceError:
            content_fingerprint = None

    base = {
        "candle_id": candle.get("candle_id"),
        "exchange": candle.get("exchange"),
        "canonical_symbol": candle.get("canonical_symbol"),
        "timeframe": candle.get("timeframe"),
        "candle_open_at": candle.get("candle_open_at"),
        "candle_close_at": candle.get("candle_close_at"),
        "canonical_content_fingerprint": content_fingerprint,
        "reason_codes": reason_codes,
    }
    if reason_codes:
        code = HISTORICAL_INCOMPLETE_CANDLE if HISTORICAL_INCOMPLETE_CANDLE in reason_codes else reason_codes[0]
        return {**base, "action": "exclude", "code": code}
    if existing_document is None:
        return {**base, "action": "insert", "code": HISTORICAL_INSERT}

    existing_schema = existing_document.get("schema_version")
    if existing_schema != HISTORICAL_OHLCV_SCHEMA_VERSION:
        return {
            **base,
            "action": "conflict",
            "code": HISTORICAL_CONFLICT_SCHEMA,
            "existing_schema_version": existing_schema,
        }
    if existing_document.get("candle_id") != candle.get("candle_id"):
        return {**base, "action": "conflict", "code": HISTORICAL_CONFLICT_IDENTITY}
    existing_fingerprint = persisted_content_fingerprint(existing_document)
    if existing_fingerprint == content_fingerprint:
        return {
            **base,
            "action": "noop",
            "code": HISTORICAL_NOOP_IDENTICAL,
            "existing_content_fingerprint": existing_fingerprint,
        }
    return {
        **base,
        "action": "conflict",
        "code": HISTORICAL_CONFLICT_CONTENT,
        "existing_content_fingerprint": existing_fingerprint,
    }


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _cursor_to_list(cursor: Any) -> list[dict[str, Any]]:
    if cursor is None:
        return []
    to_list = getattr(cursor, "to_list", None)
    if callable(to_list):
        rows = await _maybe_await(to_list(length=None))
        return [dict(row) for row in (rows or [])]
    if hasattr(cursor, "__aiter__"):
        return [dict(row) async for row in cursor]
    return [dict(row) for row in cursor]


def _collection_name(collection: Any) -> str:
    return str(getattr(collection, "name", HISTORICAL_OHLCV_COLLECTION) or HISTORICAL_OHLCV_COLLECTION)


async def _index_documents(collection: Any) -> list[dict[str, Any]]:
    if collection is None:
        return []
    index_information = getattr(collection, "index_information", None)
    if callable(index_information):
        info = await _maybe_await(index_information())
        return [{"name": name, **dict(document)} for name, document in dict(info or {}).items()]
    list_indexes = getattr(collection, "list_indexes", None)
    if callable(list_indexes):
        return await _cursor_to_list(await _maybe_await(list_indexes()))
    return []


def _normalize_index_keys(value: Any) -> tuple[tuple[str, int], ...]:
    if isinstance(value, Mapping):
        return tuple((str(key), int(direction)) for key, direction in value.items())
    normalized = []
    for item in value or []:
        if isinstance(item, Mapping):
            key = item.get("key") or item.get("field") or item.get("name")
            direction = item.get("direction") or item.get("value") or 1
            normalized.append((str(key), int(direction)))
        else:
            key, direction = item
            normalized.append((str(key), int(direction)))
    return tuple(normalized)


def _index_matches_spec(document: Mapping[str, Any], spec: HistoricalIndexSpec) -> bool:
    if _normalize_index_keys(document.get("key")) != spec.keys:
        return False
    if bool(document.get("unique", False)) != spec.unique:
        return False
    return True


def _safe_index_doc(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": document.get("name"),
        "key": [list(item) for item in _normalize_index_keys(document.get("key"))],
        "unique": bool(document.get("unique", False)),
    }


async def classify_historical_index_readiness(collection: Any) -> dict[str, Any]:
    if collection is None:
        return {
            "ok": False,
            "status": "BLOCKED",
            "code": COLLECTION_UNAVAILABLE,
            "collection": HISTORICAL_OHLCV_COLLECTION,
            "required_unique_index_present": False,
            "apply_blocked": True,
            "classifications": [],
            "missing_indexes": [HISTORICAL_REQUIRED_UNIQUE_INDEX.as_dict()],
            "incompatible_indexes": [],
            "zero_writes_performed": True,
            "zero_indexes_created": True,
        }

    docs = await _index_documents(collection)
    by_name = {str(doc.get("name")): doc for doc in docs if doc.get("name")}
    classifications: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    incompatible: list[dict[str, Any]] = []
    for spec in required_historical_index_specs():
        same_name = by_name.get(spec.name)
        equivalent = [
            doc
            for doc in docs
            if str(doc.get("name")) != spec.name and _index_matches_spec(doc, spec)
        ]
        if same_name and _index_matches_spec(same_name, spec):
            classification = {
                "collection": HISTORICAL_OHLCV_COLLECTION,
                "canonical_name": spec.name,
                "physical_name": spec.name,
                "classification": CANONICAL_EXACT,
                "required_for_apply": spec.required_for_apply,
                "reason": "canonical index is present",
            }
        elif same_name:
            classification = {
                "collection": HISTORICAL_OHLCV_COLLECTION,
                "canonical_name": spec.name,
                "physical_name": spec.name,
                "classification": SAME_NAME_INCOMPATIBLE,
                "required_for_apply": spec.required_for_apply,
                "reason": "same-name index has incompatible keys or uniqueness",
                "existing": _safe_index_doc(same_name),
                "expected": spec.as_dict(),
            }
            incompatible.append(classification)
        elif len(equivalent) == 1:
            classification = {
                "collection": HISTORICAL_OHLCV_COLLECTION,
                "canonical_name": spec.name,
                "physical_name": equivalent[0].get("name"),
                "classification": EQUIVALENT_LEGACY_NAME_ACCEPTED,
                "required_for_apply": spec.required_for_apply,
                "reason": "equivalent index exists under a different physical name",
            }
        elif len(equivalent) > 1:
            classification = {
                "collection": HISTORICAL_OHLCV_COLLECTION,
                "canonical_name": spec.name,
                "physical_names": [doc.get("name") for doc in equivalent],
                "classification": MULTIPLE_EQUIVALENT_INDEXES_AMBIGUOUS,
                "required_for_apply": spec.required_for_apply,
                "reason": "multiple equivalent historical indexes are ambiguous",
                "expected": spec.as_dict(),
            }
            incompatible.append(classification)
        else:
            classification = {
                "collection": HISTORICAL_OHLCV_COLLECTION,
                "canonical_name": spec.name,
                "physical_name": None,
                "classification": MISSING_BLOCKED,
                "required_for_apply": spec.required_for_apply,
                "reason": "index is missing; Phase 5B1 does not create indexes",
                "expected": spec.as_dict(),
            }
            missing.append(spec.as_dict())
        classifications.append(classification)

    required = next(
        item for item in classifications if item["canonical_name"] == HISTORICAL_REQUIRED_UNIQUE_INDEX.name
    )
    required_ready = required["classification"] in {CANONICAL_EXACT, EQUIVALENT_LEGACY_NAME_ACCEPTED}
    incompatible_required = [
        item for item in incompatible if item.get("required_for_apply") is True
    ]
    status = "READY" if required_ready and not incompatible_required else "BLOCKED"
    code = None
    if incompatible_required:
        code = HISTORICAL_REQUIRED_INDEX_INCOMPATIBLE
    elif not required_ready:
        code = HISTORICAL_REQUIRED_INDEX_MISSING
    return {
        "ok": status == "READY",
        "status": status,
        "code": code,
        "collection": _collection_name(collection),
        "required_unique_index_present": required_ready,
        "apply_blocked": status != "READY",
        "classifications": classifications,
        "missing_indexes": missing,
        "incompatible_indexes": incompatible,
        "required_index_contract": HISTORICAL_REQUIRED_UNIQUE_INDEX.as_dict(),
        "query_index_contracts": [spec.as_dict() for spec in HISTORICAL_QUERY_INDEXES],
        "zero_writes_performed": True,
        "zero_indexes_created": True,
    }


async def _find_existing_documents(collection: Any, candle_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    ids = sorted({str(candle_id) for candle_id in candle_ids if candle_id})
    if not ids or collection is None:
        return {}
    find = getattr(collection, "find", None)
    if not callable(find):
        return {}
    projection = {
        "_id": 0,
        "store_version": 1,
        "schema_version": 1,
        "candle_id": 1,
        "exchange": 1,
        "canonical_symbol": 1,
        "timeframe": 1,
        "candle_open_at": 1,
        "candle_close_at": 1,
        "open": 1,
        "high": 1,
        "low": 1,
        "close": 1,
        "volume": 1,
        "adjusted_close": 1,
        "is_closed": 1,
        "canonical_content_fingerprint": 1,
        "content_fingerprint_version": 1,
        "persistence": 1,
    }
    cursor = find(
        {"schema_version": HISTORICAL_OHLCV_SCHEMA_VERSION, "candle_id": {"$in": ids}},
        projection,
    )
    rows = await _cursor_to_list(cursor)
    return {str(row.get("candle_id")): row for row in rows if row.get("candle_id")}


def _sanitize_examples(rows: Iterable[Mapping[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
    examples = []
    for row in list(rows)[:limit]:
        examples.append({key: safe_json_value(row.get(key)) for key in SAFE_EXAMPLE_FIELDS if key in row})
    return examples


def _fingerprint_hash(fingerprints: Iterable[str]) -> str:
    return _stable_hash({"fingerprints": sorted(str(item) for item in fingerprints)})


def _plan_hash_content(plan: Mapping[str, Any]) -> dict[str, Any]:
    material = deepcopy(dict(plan))
    material.pop("manifest_hash", None)
    material.pop("plan_hash", None)
    for document in material.get("candidate_documents") or []:
        persistence = document.get("persistence") if isinstance(document.get("persistence"), dict) else None
        if persistence is not None:
            persistence["preview_manifest_hash"] = None
    return safe_json_value(material)


def compute_historical_manifest_hash(plan: Mapping[str, Any]) -> str:
    return _stable_hash(_plan_hash_content(plan))


def verify_historical_plan_hash(plan: Mapping[str, Any]) -> str:
    input_fingerprints = [str(item) for item in plan.get("input_candle_fingerprints") or []]
    expected_input_hash = _fingerprint_hash(input_fingerprints)
    if expected_input_hash != plan.get("input_fingerprint_hash"):
        raise HistoricalPersistenceError(
            HISTORICAL_PLAN_INPUT_CHANGED,
            "Plan input candle fingerprint list does not match the recorded input hash.",
            {"expected": plan.get("input_fingerprint_hash"), "actual": expected_input_hash},
        )
    manifest_hash = compute_historical_manifest_hash(plan)
    if manifest_hash != plan.get("manifest_hash"):
        raise HistoricalPersistenceError(
            HISTORICAL_PLAN_HASH_MISMATCH,
            "Plan manifest hash does not match canonical plan content.",
            {"expected": plan.get("manifest_hash"), "actual": manifest_hash},
        )
    for document in plan.get("candidate_documents") or []:
        persistence = document.get("persistence") if isinstance(document.get("persistence"), Mapping) else {}
        if persistence.get("preview_manifest_hash") != manifest_hash:
            raise HistoricalPersistenceError(
                HISTORICAL_PLAN_HASH_MISMATCH,
                "Candidate document preview manifest hash does not match plan hash.",
                {"candle_id": document.get("candle_id")},
            )
    return manifest_hash


async def _call_fetcher(fetcher: Any, **kwargs: Any) -> dict[str, Any]:
    if inspect.iscoroutinefunction(fetcher):
        return await fetcher(**kwargs)
    return await asyncio.to_thread(fetcher, **kwargs)


async def build_historical_backfill_plan(
    collection: Any,
    *,
    database_name: str,
    provider: str,
    exchange: str,
    canonical_symbol: str,
    timeframe: str,
    start: Any,
    end: Any,
    max_rows: int,
    include_incomplete: bool = False,
    acquisition_result: Mapping[str, Any] | None = None,
    fetcher: Any = fetch_historical_ohlcv,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_dt = _utc(now)
    start_dt, end_dt, timeframe_contract, _provider_timeframe = validate_request_range(
        start=start,
        end=end,
        provider=provider,
        timeframe=timeframe,
        limit=max_rows,
    )
    clean_exchange = str(exchange or "").strip().upper()
    clean_symbol = normalize_symbol(clean_exchange, canonical_symbol)
    request = {
        "provider": str(provider or "").strip().lower(),
        "exchange": clean_exchange,
        "canonical_symbol": clean_symbol,
        "timeframe": timeframe_contract.canonical,
        "start": canonical_utc_iso(start_dt),
        "end": canonical_utc_iso(end_dt),
        "include_incomplete": include_incomplete,
        "max_rows": max_rows,
    }
    if acquisition_result is None:
        acquisition_result = await _call_fetcher(
            fetcher,
            provider=request["provider"],
            exchange=clean_exchange,
            canonical_symbol=clean_symbol,
            timeframe=timeframe_contract.canonical,
            start=request["start"],
            end=request["end"],
            include_incomplete=include_incomplete,
            limit=max_rows,
        )
    acquisition = safe_json_value(dict(acquisition_result))
    candles = [dict(candle) for candle in acquisition.get("candles") or []]
    if len(candles) > max_rows:
        candles = candles[:max_rows]

    index_readiness = await classify_historical_index_readiness(collection)
    existing = await _find_existing_documents(collection, [str(candle.get("candle_id")) for candle in candles])

    actions: list[dict[str, Any]] = []
    inserts: list[dict[str, Any]] = []
    noops: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    candidate_documents: list[dict[str, Any]] = []
    input_fingerprints: list[str] = []

    preview_time = canonical_utc_iso(now_dt)
    run_id = _stable_hash(
        {
            "store_version": HISTORICAL_OHLCV_STORE_VERSION,
            "database_name": database_name,
            "collection": HISTORICAL_OHLCV_COLLECTION,
            "request": request,
            "created_at": preview_time,
        }
    )[:24]
    for candle in sorted(candles, key=lambda item: (str(item.get("candle_open_at")), str(item.get("candle_id")))):
        action = classify_persistence_action(
            candle,
            existing_document=existing.get(str(candle.get("candle_id"))),
        )
        actions.append(action)
        if action.get("canonical_content_fingerprint"):
            input_fingerprints.append(str(action["canonical_content_fingerprint"]))
        if action["action"] == "insert":
            doc = build_persisted_candle(
                candle,
                persistence_run_id=run_id,
                first_persisted_at=preview_time,
                preview_manifest_hash=None,
            )
            candidate_documents.append({**doc, "persistence_action": HISTORICAL_INSERT})
            inserts.append(action)
        elif action["action"] == "noop":
            noops.append(action)
        elif action["action"] == "conflict":
            conflicts.append(action)
        else:
            excluded.append(action)

    created_at = preview_time
    expires_at = canonical_utc_iso(now_dt + timedelta(seconds=HISTORICAL_OHLCV_PLAN_TTL_SECONDS))
    counts = {
        "provider_candles": len(candles),
        "candidate_candles": len(actions),
        "planned_inserts": len(inserts),
        "identical_noops": len(noops),
        "conflicts": len(conflicts),
        "excluded": len(excluded),
        "incomplete_excluded": sum(1 for item in excluded if item.get("code") == HISTORICAL_INCOMPLETE_CANDLE),
    }
    plan = {
        "schema_version": 1,
        "store_version": HISTORICAL_OHLCV_STORE_VERSION,
        "acquisition_schema_version": HISTORICAL_OHLCV_SCHEMA_VERSION,
        "target_database": database_name,
        "target_collection": HISTORICAL_OHLCV_COLLECTION,
        "collection": HISTORICAL_OHLCV_COLLECTION,
        "created_at": created_at,
        "expires_at": expires_at,
        "apply": False,
        "read_only": True,
        "preview_only": True,
        "mongo_writes_enabled": False,
        "overwrite_enabled": False,
        "index_creation_enabled": False,
        "required_operator_intent_header": None,
        "request": request,
        "source_acquisition": {
            "schema_version": acquisition.get("schema_version"),
            "provider": acquisition.get("provider"),
            "provider_symbol": acquisition.get("provider_symbol"),
            "counts": acquisition.get("counts") or {},
            "warnings": acquisition.get("warnings") or [],
            "errors": acquisition.get("errors") or [],
            "continuity": acquisition.get("continuity") or {},
        },
        "index_readiness": index_readiness,
        "counts": counts,
        "actions": safe_json_value(actions),
        "candidate_documents": safe_json_value(candidate_documents),
        "input_candle_fingerprints": sorted(input_fingerprints),
        "input_fingerprint_hash": _fingerprint_hash(input_fingerprints),
        "sanitized_examples": {
            "inserts": _sanitize_examples(inserts),
            "noops": _sanitize_examples(noops),
            "conflicts": _sanitize_examples(conflicts),
            "excluded": _sanitize_examples(excluded),
        },
        "safety": {
            "writes_performed": 0,
            "indexes_created": 0,
            "indexes_dropped": 0,
            "live_backfill_performed": False,
            "apply_requires_approval": True,
            "apply_requires_existing_unique_index": True,
            "apply_batch_size": HISTORICAL_OHLCV_APPLY_BATCH_SIZE,
            "live_preview_startup_safety": "LIVE_BACKFILL_PREVIEW_NOT_RUN_UNSAFE_STARTUP_NOT_PROVEN",
        },
        "apply_allowed": bool(index_readiness.get("ok")) and counts["conflicts"] == 0 and counts["excluded"] == 0,
        "blocked_reason_codes": [],
    }
    blocked = []
    if not index_readiness.get("ok"):
        blocked.append(index_readiness.get("code") or HISTORICAL_REQUIRED_INDEX_MISSING)
    if conflicts:
        blocked.append(HISTORICAL_CONFLICT_CONTENT)
    if excluded:
        blocked.extend(sorted({str(item.get("code")) for item in excluded if item.get("code")}))
    plan["blocked_reason_codes"] = sorted(set(blocked), key=blocked.index)
    manifest_hash = compute_historical_manifest_hash(plan)
    plan["manifest_hash"] = manifest_hash
    for document in plan["candidate_documents"]:
        document["persistence"]["preview_manifest_hash"] = manifest_hash
    return safe_json_value(plan)


def validate_historical_plan_for_apply(
    plan: Mapping[str, Any],
    *,
    expected_manifest_hash: str,
    expected_database: str,
    expected_collection: str = HISTORICAL_OHLCV_COLLECTION,
    now: datetime | None = None,
) -> dict[str, Any]:
    if plan.get("store_version") != HISTORICAL_OHLCV_STORE_VERSION:
        raise HistoricalPersistenceError(HISTORICAL_PLAN_HASH_MISMATCH, "Unsupported historical store version.")
    if plan.get("target_database") != expected_database:
        raise HistoricalPersistenceError(HISTORICAL_PLAN_HASH_MISMATCH, "Plan database does not match apply target.")
    if plan.get("target_collection") != expected_collection:
        raise HistoricalPersistenceError(HISTORICAL_PLAN_HASH_MISMATCH, "Plan collection does not match apply target.")
    manifest_hash = verify_historical_plan_hash(plan)
    if not expected_manifest_hash or expected_manifest_hash != manifest_hash:
        raise HistoricalPersistenceError(
            HISTORICAL_PLAN_HASH_MISMATCH,
            "Approved manifest hash does not match the plan.",
            {"expected": expected_manifest_hash, "actual": manifest_hash},
        )
    expires_at = _parse_utc(plan.get("expires_at"), field_name="plan.expires_at")
    if _utc(now) > expires_at:
        raise HistoricalPersistenceError(HISTORICAL_PLAN_EXPIRED, "Historical OHLCV plan has expired.")
    counts = plan.get("counts") or {}
    if int(counts.get("conflicts") or 0) > 0:
        raise HistoricalPersistenceError(HISTORICAL_CONFLICT_CONTENT, "Plan contains content conflicts.")
    if int(counts.get("excluded") or 0) > 0:
        raise HistoricalPersistenceError(HISTORICAL_EXCLUDED_INVALID, "Plan contains excluded candles.")
    documents = list(plan.get("candidate_documents") or [])
    seen: set[str] = set()
    for document in documents:
        candle_id = str(document.get("candle_id") or "")
        if not candle_id or candle_id in seen:
            raise HistoricalPersistenceError(HISTORICAL_PLAN_INPUT_CHANGED, "Plan contains duplicate or missing candle ids.")
        seen.add(candle_id)
        if document.get("store_version") != HISTORICAL_OHLCV_STORE_VERSION:
            raise HistoricalPersistenceError(HISTORICAL_PLAN_INPUT_CHANGED, "Candidate document store version changed.")
        if document.get("canonical_content_fingerprint") != persisted_content_fingerprint(document):
            raise HistoricalPersistenceError(HISTORICAL_PLAN_INPUT_CHANGED, "Candidate document content fingerprint changed.")
    return safe_json_value(dict(plan))


def validate_apply_approval(
    approval: Mapping[str, Any] | None,
    *,
    manifest_hash: str,
    expected_database: str,
    expected_collection: str,
) -> None:
    approval = approval or {}
    if approval.get("approved") is not True:
        raise HistoricalPersistenceError(HISTORICAL_APPROVAL_REQUIRED, "Apply mode requires approved=true.")
    if approval.get("approval_value") != HISTORICAL_OHLCV_APPLY_APPROVAL:
        raise HistoricalPersistenceError(HISTORICAL_APPROVAL_REQUIRED, "Historical OHLCV approval value is missing.")
    if approval.get("operator_acknowledgement") != HISTORICAL_OHLCV_APPLY_ACK:
        raise HistoricalPersistenceError(HISTORICAL_APPROVAL_REQUIRED, "Insert-only acknowledgement is required.")
    if approval.get("manifest_hash") != manifest_hash:
        raise HistoricalPersistenceError(HISTORICAL_PLAN_HASH_MISMATCH, "Approval manifest hash does not match plan.")
    if approval.get("database_name") != expected_database:
        raise HistoricalPersistenceError(HISTORICAL_APPROVAL_REQUIRED, "Approval database does not match apply target.")
    if approval.get("collection") != expected_collection:
        raise HistoricalPersistenceError(HISTORICAL_APPROVAL_REQUIRED, "Approval collection does not match apply target.")


async def _find_one_by_candle_id(collection: Any, candle_id: str) -> dict[str, Any] | None:
    find_one = getattr(collection, "find_one", None)
    if not callable(find_one):
        return None
    row = await _maybe_await(
        find_one({"schema_version": HISTORICAL_OHLCV_SCHEMA_VERSION, "candle_id": candle_id})
    )
    return dict(row) if row else None


async def apply_historical_backfill_plan(
    collection: Any,
    plan: Mapping[str, Any],
    *,
    approval: Mapping[str, Any],
    database_name: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    validated = validate_historical_plan_for_apply(
        plan,
        expected_manifest_hash=str((approval or {}).get("manifest_hash") or ""),
        expected_database=database_name,
        expected_collection=HISTORICAL_OHLCV_COLLECTION,
        now=now,
    )
    manifest_hash = str(validated["manifest_hash"])
    validate_apply_approval(
        approval,
        manifest_hash=manifest_hash,
        expected_database=database_name,
        expected_collection=HISTORICAL_OHLCV_COLLECTION,
    )
    index_readiness = await classify_historical_index_readiness(collection)
    if not index_readiness.get("ok"):
        raise HistoricalPersistenceError(
            index_readiness.get("code") or HISTORICAL_REQUIRED_INDEX_MISSING,
            "Required historical OHLCV unique index is not present; apply is blocked.",
            {"index_readiness": index_readiness},
        )

    update_one = getattr(collection, "update_one", None)
    if not callable(update_one):
        raise HistoricalPersistenceError(HISTORICAL_PLAN_STALE, "Collection does not support update_one.")

    apply_time = canonical_utc_iso(_utc(now))
    inserted: list[dict[str, Any]] = []
    noops: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    attempted = 0
    documents = [
        dict(document)
        for document in validated.get("candidate_documents") or []
        if document.get("persistence_action") == HISTORICAL_INSERT
    ]
    for document in documents:
        attempted += 1
        document = deepcopy(document)
        document.pop("persistence_action", None)
        persistence = document.setdefault("persistence", {})
        persistence["preview_manifest_hash"] = manifest_hash
        persistence["applied_at"] = apply_time
        candle_id = str(document.get("candle_id") or "")
        query = {"schema_version": HISTORICAL_OHLCV_SCHEMA_VERSION, "candle_id": candle_id}
        update = {"$setOnInsert": safe_json_value(document)}
        try:
            result = await _maybe_await(update_one(query, update, upsert=True))
            upserted_id = getattr(result, "upserted_id", None)
            upserted_count = int(getattr(result, "upserted_count", 1 if upserted_id is not None else 0) or 0)
            matched_count = int(getattr(result, "matched_count", 0) or 0)
            if upserted_id is not None or upserted_count > 0:
                inserted.append({"candle_id": candle_id, "canonical_content_fingerprint": document.get("canonical_content_fingerprint")})
                continue
            existing = await _find_one_by_candle_id(collection, candle_id)
            if existing and persisted_content_fingerprint(existing) == document.get("canonical_content_fingerprint"):
                noops.append({"candle_id": candle_id, "code": HISTORICAL_NOOP_IDENTICAL})
                continue
            code = HISTORICAL_CONFLICT_CONTENT if matched_count else HISTORICAL_PLAN_STALE
            conflicts.append(
                {
                    "candle_id": candle_id,
                    "code": code,
                    "existing_content_fingerprint": persisted_content_fingerprint(existing or {}),
                    "canonical_content_fingerprint": document.get("canonical_content_fingerprint"),
                }
            )
            break
        except Exception as exc:
            existing = await _find_one_by_candle_id(collection, candle_id)
            if existing and persisted_content_fingerprint(existing) == document.get("canonical_content_fingerprint"):
                noops.append({"candle_id": candle_id, "code": HISTORICAL_NOOP_IDENTICAL})
                continue
            failures.append({"candle_id": candle_id, "code": type(exc).__name__})
            break

    ok = not conflicts and not failures and (len(inserted) + len(noops) == len(documents))
    code = None
    if ok and not inserted:
        code = HISTORICAL_APPLY_IDEMPOTENT
    elif conflicts or failures:
        code = HISTORICAL_APPLY_PARTIAL_FAILURE
    return {
        "ok": ok,
        "apply": True,
        "store_version": HISTORICAL_OHLCV_STORE_VERSION,
        "target_database": database_name,
        "target_collection": HISTORICAL_OHLCV_COLLECTION,
        "manifest_hash_used": manifest_hash,
        "planned_inserts": len(documents),
        "attempted_inserts": attempted,
        "inserted_count": len(inserted),
        "idempotent_noop_count": len(noops),
        "conflict_count": len(conflicts),
        "failure_count": len(failures),
        "code": code,
        "inserted": inserted,
        "idempotent_noops": noops,
        "conflicts": conflicts,
        "failures": failures,
        "index_readiness": index_readiness,
        "overwrite_enabled": False,
        "update_operator": "$setOnInsert",
        "zero_indexes_created": True,
    }


async def historical_persistence_readiness(
    collection: Any,
    *,
    database_name: str,
) -> dict[str, Any]:
    index_readiness = await classify_historical_index_readiness(collection)
    return {
        "store_version": HISTORICAL_OHLCV_STORE_VERSION,
        "target_database": database_name,
        "target_collection": HISTORICAL_OHLCV_COLLECTION,
        "collection": HISTORICAL_OHLCV_COLLECTION,
        "status": "READY" if index_readiness.get("ok") else "BLOCKED",
        "ai_training_readiness": "NOT_SAFE",
        "reason": (
            "Historical persistence contract is preview/apply guarded; training readiness requires Phase 5B2 verification."
        ),
        "apply_batch_size": HISTORICAL_OHLCV_APPLY_BATCH_SIZE,
        "required_unique_index_present": bool(index_readiness.get("required_unique_index_present")),
        "index_readiness": index_readiness,
        "apply_contract": {
            "approval_value": HISTORICAL_OHLCV_APPLY_APPROVAL,
            "operator_acknowledgement": HISTORICAL_OHLCV_APPLY_ACK,
            "insert_only": True,
            "overwrite_enabled": False,
            "requires_manifest_hash": True,
            "requires_unexpired_plan": True,
        },
        "startup_index_registry_owned": False,
        "zero_writes_performed": True,
        "zero_indexes_created": True,
    }
