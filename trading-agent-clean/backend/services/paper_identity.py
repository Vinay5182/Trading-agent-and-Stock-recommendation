from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any


SETUP_ID_VERSION = "paper_setup:v2"
SOURCE_ID_FIELDS = (
    "source_confirmation_id",
    "tv_confirmation_id",
    "saved_confirmation_id",
    "source_id",
)
SETUP_DATE_FIELDS = (
    "source_trade_date",
    "trade_date",
    "session_date",
    "setup_date",
    "source_candle_at",
    "tv_confirmed_at",
    "confirmed_at",
    "swing_confirmed_at",
    "momentum_confirmed_at",
    "source_confirmation_updated_at",
    "updated_at",
    "source_confirmation_created_at",
    "source_created_at",
    "signal_created_at",
    "created_at",
    "entry_date",
)


def _clean_text(value: Any) -> str:
    return str(value or "").strip().upper()


def clean_symbol(value: Any) -> str:
    text = _clean_text(value)
    if ":" in text:
        text = text.split(":")[-1]
    if "." in text:
        text = text.split(".")[0]
    return text


def clean_source_type(document: dict) -> str:
    return _clean_text(
        document.get("source_signal_type")
        or document.get("signal_type")
        or document.get("strategy_type")
        or document.get("source")
    )


def clean_timeframe(value: Any) -> str:
    return _clean_text(value or "1D") or "1D"


def serialize_identity_value(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def first_identity_value(document: dict, fields: tuple[str, ...]) -> str | None:
    for field in fields:
        value = serialize_identity_value(document.get(field))
        if value:
            return value
    return None


def parse_setup_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    return text


def setup_date_for_document(document: dict) -> str | None:
    for field in SETUP_DATE_FIELDS:
        setup_date = parse_setup_date(document.get(field))
        if setup_date:
            return setup_date
    return None


def paper_setup_identity(document: dict) -> dict | None:
    symbol = clean_symbol(
        document.get("symbol")
        or document.get("canonical_symbol")
        or document.get("tradingview_symbol")
        or document.get("requested_tradingview_symbol")
    )
    source_type = clean_source_type(document)
    if not symbol or not source_type:
        return None

    identity = {
        "version": 2,
        "symbol": symbol,
        "source_signal_type": source_type,
        "timeframe": clean_timeframe(document.get("timeframe")),
        "paper_only": True,
        "setup_date": setup_date_for_document(document) or "UNKNOWN",
    }
    source_id = first_identity_value(document, SOURCE_ID_FIELDS)
    source_collection = _clean_text(document.get("source_collection") or document.get("tv_confirmation_collection"))
    if source_id:
        identity["source_confirmation_id"] = source_id
        if source_collection:
            identity["source_collection"] = source_collection
    return identity


def setup_id_from_identity(identity: dict) -> str:
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{SETUP_ID_VERSION}:{digest}"


def canonical_setup_id(document: dict) -> str | None:
    identity = paper_setup_identity(document)
    return setup_id_from_identity(identity) if identity else None


def setup_identity_fields(document: dict) -> dict:
    identity = paper_setup_identity(document)
    if not identity:
        return {}
    fields = {
        "setup_id": setup_id_from_identity(identity),
        "setup_identity": identity,
        "canonical_setup_id": setup_id_from_identity(identity),
    }
    if identity.get("setup_date"):
        fields["setup_date"] = identity["setup_date"]
    if identity.get("source_confirmation_id"):
        fields["source_confirmation_id"] = identity["source_confirmation_id"]
    if identity.get("source_collection"):
        fields["source_collection"] = identity["source_collection"]
    return fields


def apply_setup_identity(document: dict) -> dict:
    updated = dict(document)
    updated.update(setup_identity_fields(updated))
    updated.setdefault("state_version", 1)
    return updated


def paper_trade_setup_filter(document: dict) -> dict:
    setup_id = document.get("setup_id") or canonical_setup_id(document)
    if not setup_id:
        raise ValueError("paper trade setup identity requires symbol and source_signal_type")
    return {"paper_only": True, "setup_id": setup_id}


def legacy_paper_trade_identity(document: dict) -> dict:
    return {
        "symbol": document["symbol"],
        "source_signal_type": document["source_signal_type"],
        "paper_only": True,
    }


def source_fields_from_saved_row(row: dict, source_collection: str) -> dict:
    fields = {
        "source_collection": source_collection,
    }
    source_id = serialize_identity_value(row.get("_id"))
    if source_id:
        fields["source_confirmation_id"] = source_id
    source_created_at = row.get("created_at")
    if source_created_at not in (None, ""):
        fields["source_confirmation_created_at"] = source_created_at
    return fields
