from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pymongo.errors import DuplicateKeyError, OperationFailure

from services.paper_identity import (
    SOURCE_ID_FIELDS,
    clean_source_type,
    clean_symbol,
    clean_timeframe,
    first_identity_value,
    paper_setup_identity,
    setup_id_from_identity,
)


logger = logging.getLogger("uvicorn.error")
OBSOLETE_PAPER_TRADE_INDEXES = (
    "symbol_1_timeframe_1_source_signal_type_1_paper_only_1_status_1",
    "symbol_1_source_signal_type_1_paper_only_1_entry_price_1_stop_loss_1",
    "paper_trade_sync_identity",
)

TERMINAL_RANKS = {
    "COMPLETED": 100,
    "T3_HIT": 100,
    "TARGET_3_HIT": 100,
    "WON_T3": 100,
    "T2_HIT": 85,
    "TARGET_2_HIT": 85,
    "WON_T2": 85,
    "SL_HIT": 80,
    "STOP_HIT": 80,
    "LOST_SL": 80,
    "STOPPED": 80,
    "STOPPED_AFTER_T1": 80,
    "T1_HIT": 70,
    "TARGET_1_HIT_FINAL": 70,
    "WON_T1": 70,
    "AMBIGUOUS": 65,
    "CLOSED": 60,
    "EXPIRED": 55,
}
OPEN_RANKS = {
    "T2_PARTIAL": 75,
    "TARGET_1_HIT": 65,
    "T1_PARTIAL": 65,
    "ACTIVE": 50,
    "WAITING_FOR_ENTRY": 20,
    "NOT_TRIGGERED": 20,
    "PLANNED": 20,
    "WAITING": 20,
}


def _status_values(trade: dict) -> set[str]:
    return {
        str(value or "").upper()
        for value in (trade.get("status"), trade.get("outcome_status"), trade.get("state"))
        if value
    }


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if value in (None, ""):
        return datetime.min
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return datetime.min


def trade_progress_rank(trade: dict) -> tuple[int, datetime, datetime, str]:
    statuses = _status_values(trade)
    rank = 0
    for status in statuses:
        rank = max(rank, TERMINAL_RANKS.get(status, OPEN_RANKS.get(status, 0)))
    if trade.get("partial_exit_3"):
        rank = max(rank, 100)
    elif trade.get("partial_exit_2"):
        rank = max(rank, 85)
    elif trade.get("partial_exit_1"):
        rank = max(rank, 70)
    elif trade.get("entry_triggered") is True:
        rank = max(rank, 50)
    return (
        rank,
        _parse_datetime(trade.get("status_updated_at") or trade.get("updated_at")),
        _parse_datetime(trade.get("created_at")),
        str(trade.get("_id") or ""),
    )


def most_advanced_trade(trades: list[dict]) -> dict:
    return max(trades, key=trade_progress_rank)


def migration_setup_identity(trade: dict) -> dict | None:
    identity = paper_setup_identity(trade)
    if not identity:
        return None
    if first_identity_value(trade, SOURCE_ID_FIELDS):
        return identity
    return {
        "version": 1,
        "symbol": clean_symbol(
            trade.get("symbol")
            or trade.get("canonical_symbol")
            or trade.get("tradingview_symbol")
            or trade.get("requested_tradingview_symbol")
        ),
        "source_signal_type": clean_source_type(trade),
        "timeframe": clean_timeframe(trade.get("timeframe")),
        "paper_only": True,
        "legacy_identity_scope": "symbol_source_timeframe",
    }


def migration_setup_identity_fields(trade: dict) -> dict:
    identity = migration_setup_identity(trade)
    if not identity:
        return {}
    setup_id = setup_id_from_identity(identity)
    fields = {
        "setup_id": setup_id,
        "setup_identity": identity,
        "canonical_setup_id": setup_id,
    }
    if identity.get("source_confirmation_id"):
        fields["source_confirmation_id"] = identity["source_confirmation_id"]
    if identity.get("source_collection"):
        fields["source_collection"] = identity["source_collection"]
    if identity.get("setup_date"):
        fields["setup_date"] = identity["setup_date"]
    return fields


async def ensure_paper_trade_setup_index(db) -> dict:
    collection = getattr(db, "paper_trades", None)
    if collection is None or not hasattr(collection, "create_index"):
        return {"created": False, "reason": "NO_PAPER_TRADES_COLLECTION"}
    await collection.create_index(
        [("setup_id", 1)],
        unique=True,
        name="paper_trades_setup_id_unique_v1",
        partialFilterExpression={
            "paper_only": True,
            "setup_id": {"$exists": True, "$type": "string", "$gt": ""},
        },
    )
    return {
        "created": True,
        "name": "paper_trades_setup_id_unique_v1",
        "fields": ["setup_id"],
        "unique": True,
        "partialFilterExpression": {
            "paper_only": True,
            "setup_id": {"$exists": True, "$type": "string", "$gt": ""},
        },
    }


async def drop_obsolete_paper_trade_identity_indexes(db) -> dict:
    collection = getattr(db, "paper_trades", None)
    if collection is None or not hasattr(collection, "drop_index"):
        return {"dropped": [], "skipped": list(OBSOLETE_PAPER_TRADE_INDEXES), "errors": []}
    dropped = []
    skipped = []
    errors = []
    for name in OBSOLETE_PAPER_TRADE_INDEXES:
        try:
            await collection.drop_index(name)
            dropped.append(name)
        except OperationFailure as exc:
            message = str(exc)
            if "index not found" in message.lower() or "can't find index" in message.lower():
                skipped.append(name)
            else:
                errors.append({"index": name, "error": message})
        except Exception as exc:
            errors.append({"index": name, "error": str(exc)})
    return {"dropped": dropped, "skipped": skipped, "errors": errors}


def _archived_setup_id(canonical_setup_id: str, trade: dict) -> str:
    return f"{canonical_setup_id}:duplicate:{trade.get('_id')}"


def _changed_fields(trade: dict, update_fields: dict) -> dict:
    return {
        key: value
        for key, value in update_fields.items()
        if trade.get(key) != value
    }


async def migrate_paper_trade_setup_ids(db, *, apply: bool = False, limit: int | None = None) -> dict:
    collection = getattr(db, "paper_trades", None)
    if collection is None:
        return {
            "ok": False,
            "apply": apply,
            "scanned": 0,
            "failures": [{"reason": "NO_PAPER_TRADES_COLLECTION"}],
        }

    cursor = collection.find({"paper_only": True}).sort("updated_at", -1)
    if limit is not None:
        cursor = cursor.limit(limit)

    groups: dict[str, list[dict]] = {}
    failures: list[dict] = []
    scanned = already_archived = 0
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

    setup_ids_added = duplicates_archived = winners_updated = 0
    duplicate_groups = 0
    now = datetime.utcnow().isoformat()
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
                update_fields["setup_migrated_at"] = now
            if not is_winner:
                update_fields.update(
                    {
                        "setup_id": _archived_setup_id(setup_id, trade),
                        "canonical_setup_id": setup_id,
                        "duplicate_of_setup_id": setup_id,
                        "duplicate_of_paper_trade_id": str(winner.get("_id")),
                        "duplicate_resolution": "ARCHIVED_DUPLICATE_RETAINED_HISTORY",
                        "duplicate_archived_at": now,
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

            if not apply:
                setup_ids_added += int(not trade.get("setup_id"))
                duplicates_archived += int(not is_winner)
                winners_updated += int(is_winner)
                continue

            try:
                result = await collection.update_one(
                    {"_id": trade["_id"], "paper_only": True},
                    {"$set": changed_fields},
                    upsert=False,
                )
                modified = int(getattr(result, "modified_count", 0))
                setup_ids_added += int(modified > 0 and not trade.get("setup_id"))
                duplicates_archived += int(modified > 0 and not is_winner)
                winners_updated += int(modified > 0 and is_winner)
            except Exception as exc:
                failures.append({"trade_id": str(trade.get("_id")), "reason": str(exc)})

    index = None
    obsolete_indexes = None
    if apply and not failures:
        try:
            index = await ensure_paper_trade_setup_index(db)
        except DuplicateKeyError as exc:
            failures.append({"reason": "SETUP_ID_INDEX_DUPLICATE", "error": str(exc)})
        except Exception as exc:
            failures.append({"reason": "SETUP_ID_INDEX_FAILED", "error": str(exc)})
    if apply and not failures:
        obsolete_indexes = await drop_obsolete_paper_trade_identity_indexes(db)
        for error in obsolete_indexes.get("errors", []):
            failures.append({"reason": "OBSOLETE_INDEX_DROP_FAILED", **error})

    result = {
        "ok": not failures,
        "apply": apply,
        "scanned": scanned,
        "already_archived": already_archived,
        "setup_ids_added": setup_ids_added,
        "duplicate_groups": duplicate_groups,
        "duplicates_archived": duplicates_archived,
        "winners_updated": winners_updated,
        "failures_count": len(failures),
        "failures": failures,
        "index": index,
        "obsolete_indexes": obsolete_indexes,
    }
    logger.info("paper setup_id migration result=%s", result)
    return result
