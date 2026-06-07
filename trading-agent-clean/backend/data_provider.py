import asyncio
from datetime import datetime
from typing import Any
import os
import tempfile
import urllib.parse
import urllib.request
import json
from types import SimpleNamespace


SOURCE_NSE_PRIMARY = "NSE_PRIMARY"
SOURCE_YFINANCE_FIELD_FALLBACK = "YFINANCE_FIELD_FALLBACK"
SOURCE_MONGO_CACHE_FALLBACK = "MONGO_CACHE_FALLBACK"
SOURCE_NSE_PLUS_YFINANCE = "NSE_PLUS_YFINANCE_FIELD_FALLBACK"
SOURCE_YFINANCE_ONLY = "YFINANCE_ONLY"
SOURCE_FETCH_FAILED = "FETCH_FAILED"
SOURCE_SKIPPED_INVALID_SYMBOL = "SKIPPED_INVALID_SYMBOL"

REQUIRED_FIELDS = [
    "current_price",
    "previous_close",
    "open_price",
    "day_high",
    "day_low",
    "traded_volume",
]

SCORING_FIELDS = [
    "traded_value",
    "change_percent",
    "relative_volume",
    "thirty_day_change_percent",
]


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat()


def clean_number(value: Any) -> float | int | None:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = value.replace(",", "").strip()
            if not value or value in {"-", "NA", "N/A"}:
                return None
        number = float(value)
        if number != number:
            return None
        return int(number) if number.is_integer() else number
    except Exception:
        return None


def normalize_symbol(exchange: str, symbol: str) -> str:
    clean = (symbol or "").strip().upper()
    exchange_prefix = f"{(exchange or '').strip().upper()}:"
    if clean.startswith(exchange_prefix):
        clean = clean[len(exchange_prefix):]
    for suffix in (".NS", ".BO"):
        if clean.endswith(suffix):
            clean = clean[: -len(suffix)]
    return clean.replace(" ", "")


def is_valid_market_symbol(symbol: str) -> bool:
    clean = (symbol or "").strip().upper()
    if not clean:
        return False
    canonical = normalize_symbol("NSE", clean)
    if not canonical:
        return False
    placeholder_tokens = ("DUMMY", "PLACEHOLDER", "FAKE_SYMBOL", "TEST_SYMBOL")
    return not any(token in canonical for token in placeholder_tokens)


def build_tradingview_symbol(exchange: str, symbol: str) -> str:
    clean_exchange = (exchange or "").strip().upper()
    return f"{clean_exchange}:{normalize_symbol(clean_exchange, symbol)}"


def missing_fields(row: dict[str, Any], fields: list[str]) -> list[str]:
    return [field for field in fields if row.get(field) is None]


def required_missing_fields(row: dict[str, Any]) -> list[str]:
    return missing_fields(row, REQUIRED_FIELDS)


def scoring_missing_fields(row: dict[str, Any]) -> list[str]:
    return missing_fields(row, SCORING_FIELDS)


def merge_field_fallback(primary_row: dict[str, Any], fallback_row: dict[str, Any]) -> dict[str, Any]:
    merged = primary_row.copy()
    field_sources = dict(primary_row.get("field_sources") or {})
    fallback_sources = fallback_row.get("field_sources") or {}
    used_fallback = False

    for field in REQUIRED_FIELDS + SCORING_FIELDS:
        if merged.get(field) is None and fallback_row.get(field) is not None:
            merged[field] = fallback_row[field]
            field_sources[field] = fallback_sources.get(field, "YFINANCE")
            used_fallback = True
        elif merged.get(field) is not None and field not in field_sources:
            field_sources[field] = "NSE"

    merged["field_sources"] = field_sources
    if used_fallback and primary_row.get("source_used") == SOURCE_NSE_PRIMARY:
        merged["source_used"] = SOURCE_NSE_PLUS_YFINANCE
    elif used_fallback and not primary_row.get("source_used"):
        merged["source_used"] = SOURCE_YFINANCE_ONLY
    return merged


def build_market_data_document(
    exchange: str,
    symbol: str,
    index_name: str | None = None,
    data: dict[str, Any] | None = None,
    source_used: str = SOURCE_NSE_PRIMARY,
    field_sources: dict[str, str] | None = None,
) -> dict[str, Any]:
    clean_exchange = (exchange or "").strip().upper()
    canonical_symbol = normalize_symbol(clean_exchange, symbol)
    row = {
        "exchange": clean_exchange,
        "symbol": canonical_symbol,
        "canonical_symbol": canonical_symbol,
        "tradingview_symbol": build_tradingview_symbol(clean_exchange, canonical_symbol),
        "index_name": index_name,
        "index_memberships": data.get("index_memberships", []) if data else [],
        "current_price": None,
        "previous_close": None,
        "open_price": None,
        "day_high": None,
        "day_low": None,
        "traded_volume": None,
        "traded_value": None,
        "relative_volume": None,
        "change_percent": None,
        "thirty_day_change_percent": None,
        "source_used": source_used,
        "field_sources": field_sources or {},
    }
    if data:
        for field in REQUIRED_FIELDS + SCORING_FIELDS:
            if field in data:
                row[field] = data[field]
        for field in (
            "primary_source",
            "fallback_source",
            "nse_source_index",
            "nse_source_indexes",
            "yfinance_symbol",
            "yfinance_ok",
            "history_enriched_at",
            "history_enrichment_date",
            "history_source",
        ):
            if field in data:
                row[field] = data[field]
        row["field_sources"].update(data.get("field_sources") or {})
        row["source_used"] = data.get("source_used", row["source_used"])

    row["missing_fields"] = required_missing_fields(row)
    row["missing_fields_after_fallback"] = data.get("missing_fields_after_fallback", row["missing_fields"]) if data else row["missing_fields"]
    row["missing_fields_before_fallback"] = data.get("missing_fields_before_fallback", []) if data else []
    row["nse_ok"] = data.get("nse_ok") if data else None
    row["nse_error"] = data.get("nse_error") if data else None
    row["yfinance_error"] = data.get("yfinance_error") if data else None
    for field in REQUIRED_FIELDS + SCORING_FIELDS:
        row["field_sources"].setdefault(field, "MISSING" if row.get(field) is None else "UNKNOWN")
    row["is_complete"] = not row["missing_fields"]
    row["updated_at"] = utc_now_iso()
    return row


def fetch_nse_quote(symbol: str) -> dict[str, Any]:
    canonical_symbol = normalize_symbol("NSE", symbol)
    row = build_market_data_document("NSE", canonical_symbol, source_used=SOURCE_NSE_PRIMARY)
    row.update({"nse_ok": False, "nse_error": None})
    try:
        opener = urllib.request.build_opener()
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.nseindia.com/get-quotes/equity",
        }
        opener.open(urllib.request.Request("https://www.nseindia.com", headers=headers), timeout=6).read()
        url = "https://www.nseindia.com/api/quote-equity?symbol=" + urllib.parse.quote(canonical_symbol)
        payload = opener.open(urllib.request.Request(url, headers=headers), timeout=8).read().decode("utf-8")
        data = json.loads(payload)
        price = data.get("priceInfo") or {}
        trade = ((data.get("marketDeptOrderBook") or {}).get("tradeInfo") or {})
        intraday = price.get("intraDayHighLow") or {}
        row.update({
            "current_price": clean_number(price.get("lastPrice")),
            "previous_close": clean_number(price.get("previousClose")),
            "open_price": clean_number(price.get("open")),
            "day_high": clean_number(intraday.get("max") or price.get("intraDayHighLowMax")),
            "day_low": clean_number(intraday.get("min") or price.get("intraDayHighLowMin")),
            "traded_volume": clean_number(trade.get("totalTradedVolume") or price.get("totalTradedVolume")),
            "traded_value": clean_number(trade.get("totalTradedValue") or price.get("totalTradedValue")),
            "change_percent": clean_number(price.get("pChange")),
            "nse_ok": True,
            "field_sources": {},
        })
        for field in REQUIRED_FIELDS + SCORING_FIELDS:
            if row.get(field) is not None:
                row["field_sources"][field] = "NSE"
    except Exception as exc:
        row["nse_error"] = str(exc)
    return row


def fetch_yfinance_quote_for_missing_fields(symbol: str, missing_fields: list[str]) -> dict[str, Any]:
    canonical_symbol = normalize_symbol("NSE", symbol)
    wanted = set(missing_fields)
    row = build_market_data_document("NSE", canonical_symbol, source_used=SOURCE_YFINANCE_ONLY)
    row["field_sources"] = {}
    row["yfinance_symbol"] = f"{canonical_symbol}.NS"
    row["yfinance_ok"] = False
    try:
        import yfinance as yf

        cache_dir = os.path.join(tempfile.gettempdir(), "trading-agent-clean-yfinance-cache")
        os.makedirs(cache_dir, exist_ok=True)
        if hasattr(yf, "set_tz_cache_location"):
            yf.set_tz_cache_location(cache_dir)
        if hasattr(yf, "cache") and hasattr(yf.cache, "set_cache_location"):
            yf.cache.set_cache_location(cache_dir)

        ticker = yf.Ticker(row["yfinance_symbol"])
        history = None
        try:
            history = ticker.history(period="2mo", interval="1d", timeout=8)
        except TypeError:
            history = ticker.history(period="2mo", interval="1d")

        last = None
        previous = None
        if history is not None and not history.empty:
            last = history.iloc[-1]
            previous = history.iloc[-2] if len(history) >= 2 else None

        if wanted & {"current_price", "previous_close", "open_price", "day_high", "day_low", "traded_volume", "traded_value", "change_percent"}:
            mapping = {
                "current_price": last.get("Close") if last is not None else None,
                "previous_close": previous.get("Close") if previous is not None else None,
                "open_price": last.get("Open") if last is not None else None,
                "day_high": last.get("High") if last is not None else None,
                "day_low": last.get("Low") if last is not None else None,
                "traded_volume": last.get("Volume") if last is not None else None,
            }
            for field, value in mapping.items():
                if field in wanted:
                    row[field] = clean_number(value)
            if "traded_value" in wanted and row.get("current_price") is not None and row.get("traded_volume") is not None:
                row["traded_value"] = row["current_price"] * row["traded_volume"]
            if "change_percent" in wanted and row.get("current_price") is not None and row.get("previous_close"):
                row["change_percent"] = ((row["current_price"] - row["previous_close"]) / row["previous_close"]) * 100
        if wanted & {"relative_volume", "thirty_day_change_percent"}:
            if history is not None and not history.empty:
                if "relative_volume" in wanted and len(history) >= 21:
                    avg_volume = clean_number(history["Volume"].tail(21).iloc[:-1].mean())
                    last_volume = clean_number(last.get("Volume"))
                    if avg_volume:
                        row["relative_volume"] = last_volume / avg_volume
                if "thirty_day_change_percent" in wanted and len(history) >= 31:
                    close_30 = clean_number(history["Close"].iloc[-31])
                    last_close = clean_number(last.get("Close"))
                    if close_30:
                        row["thirty_day_change_percent"] = ((last_close - close_30) / close_30) * 100
        for field in wanted:
            if row.get(field) is not None:
                row["field_sources"][field] = "YFINANCE"
        row["yfinance_ok"] = any(row.get(field) is not None for field in REQUIRED_FIELDS + SCORING_FIELDS)
    except Exception as exc:
        row["yfinance_error"] = str(exc)
    return row


def _configure_yfinance_cache(yf) -> None:
    cache_dir = os.path.join(tempfile.gettempdir(), "trading-agent-clean-yfinance-cache")
    os.makedirs(cache_dir, exist_ok=True)
    if hasattr(yf, "set_tz_cache_location"):
        yf.set_tz_cache_location(cache_dir)
    if hasattr(yf, "cache") and hasattr(yf.cache, "set_cache_location"):
        yf.cache.set_cache_location(cache_dir)


def _safe_series_value(row: Any, field: str) -> Any:
    try:
        return row.get(field)
    except Exception:
        return None


def _history_to_yfinance_row(symbol: str, history: Any, wanted: set[str]) -> dict[str, Any]:
    canonical_symbol = normalize_symbol("NSE", symbol)
    row = build_market_data_document("NSE", canonical_symbol, source_used=SOURCE_YFINANCE_ONLY)
    row["field_sources"] = {}
    row["yfinance_symbol"] = f"{canonical_symbol}.NS"
    row["yfinance_ok"] = False
    if history is None or getattr(history, "empty", True):
        row["yfinance_error"] = "NO_YFINANCE_HISTORY"
        return row

    history = history.dropna(how="all")
    if history.empty:
        row["yfinance_error"] = "EMPTY_YFINANCE_HISTORY"
        return row

    last = history.iloc[-1]
    previous = history.iloc[-2] if len(history) >= 2 else None
    mapping = {
        "current_price": _safe_series_value(last, "Close"),
        "previous_close": _safe_series_value(previous, "Close") if previous is not None else None,
        "open_price": _safe_series_value(last, "Open"),
        "day_high": _safe_series_value(last, "High"),
        "day_low": _safe_series_value(last, "Low"),
        "traded_volume": _safe_series_value(last, "Volume"),
    }
    for field, value in mapping.items():
        if field in wanted:
            row[field] = clean_number(value)

    if "traded_value" in wanted and row.get("current_price") is not None and row.get("traded_volume") is not None:
        row["traded_value"] = row["current_price"] * row["traded_volume"]
    if "change_percent" in wanted and row.get("current_price") is not None and row.get("previous_close"):
        row["change_percent"] = ((row["current_price"] - row["previous_close"]) / row["previous_close"]) * 100
    if "relative_volume" in wanted and len(history) >= 21:
        avg_volume = clean_number(history["Volume"].tail(21).iloc[:-1].mean())
        last_volume = clean_number(_safe_series_value(last, "Volume"))
        if avg_volume and last_volume is not None:
            row["relative_volume"] = last_volume / avg_volume
    if "thirty_day_change_percent" in wanted and len(history) >= 31:
        close_30 = clean_number(history["Close"].iloc[-31])
        last_close = clean_number(_safe_series_value(last, "Close"))
        if close_30 and last_close is not None:
            row["thirty_day_change_percent"] = ((last_close - close_30) / close_30) * 100

    for field in wanted:
        if row.get(field) is not None:
            row["field_sources"][field] = "YFINANCE"
    row["yfinance_ok"] = any(row.get(field) is not None for field in REQUIRED_FIELDS + SCORING_FIELDS)
    return row


def fetch_yfinance_batch_for_missing(symbols: list[str], missing_by_symbol: dict[str, list[str]]) -> dict[str, dict[str, Any]]:
    clean_symbols = []
    for symbol in symbols:
        canonical_symbol = normalize_symbol("NSE", symbol)
        if canonical_symbol and canonical_symbol not in clean_symbols:
            clean_symbols.append(canonical_symbol)
    if not clean_symbols:
        return {}

    rows = {
        symbol: build_market_data_document("NSE", symbol, source_used=SOURCE_YFINANCE_ONLY)
        for symbol in clean_symbols
    }
    try:
        import yfinance as yf

        _configure_yfinance_cache(yf)
        tickers = [f"{symbol}.NS" for symbol in clean_symbols]
        history = yf.download(
            tickers=tickers,
            period="2mo",
            interval="1d",
            group_by="ticker",
            threads=True,
            progress=False,
            auto_adjust=False,
        )
        for symbol in clean_symbols:
            yf_symbol = f"{symbol}.NS"
            wanted = set(missing_by_symbol.get(symbol) or REQUIRED_FIELDS + SCORING_FIELDS)
            symbol_history = None
            try:
                if len(clean_symbols) == 1:
                    symbol_history = history
                elif hasattr(history, "columns") and yf_symbol in history.columns.get_level_values(0):
                    symbol_history = history[yf_symbol]
            except Exception:
                symbol_history = None
            rows[symbol] = _history_to_yfinance_row(symbol, symbol_history, wanted)
    except Exception as exc:
        for symbol in clean_symbols:
            rows[symbol]["field_sources"] = {}
            rows[symbol]["yfinance_symbol"] = f"{symbol}.NS"
            rows[symbol]["yfinance_ok"] = False
            rows[symbol]["yfinance_error"] = str(exc)
    return rows


async def fetch_market_data_for_symbol(
    symbol: str,
    index_name: str | None = None,
    index_memberships: list[str] | None = None,
    nse_quote: dict[str, Any] | None = None,
) -> dict[str, Any]:
    canonical_symbol = normalize_symbol("NSE", symbol)
    if not is_valid_market_symbol(canonical_symbol):
        return {
            "exchange": "NSE",
            "symbol": canonical_symbol,
            "canonical_symbol": canonical_symbol,
            "tradingview_symbol": build_tradingview_symbol("NSE", canonical_symbol),
            "index_name": index_name,
            "index_memberships": index_memberships or ([index_name] if index_name else []),
            "source_used": SOURCE_SKIPPED_INVALID_SYMBOL,
            "is_complete": False,
            "error": "INVALID_SYMBOL",
            "nse_ok": False,
            "yfinance_ok": False,
            "updated_at": utc_now_iso(),
        }
    nse_row = nse_quote.copy() if nse_quote else await asyncio.to_thread(fetch_nse_quote, symbol)
    if nse_quote:
        nse_row.setdefault("nse_ok", True)
        nse_row.setdefault("nse_error", None)
        nse_row.setdefault("source_used", SOURCE_NSE_PRIMARY)
    nse_row["index_name"] = index_name
    nse_row["index_memberships"] = index_memberships or ([index_name] if index_name else [])
    missing_before = required_missing_fields(nse_row) + scoring_missing_fields(nse_row)
    fallback_row = {}
    if missing_before:
        fallback_row = await asyncio.to_thread(fetch_yfinance_quote_for_missing_fields, symbol, missing_before)
        merged = merge_field_fallback(nse_row, fallback_row)
    else:
        merged = nse_row
    missing_after = required_missing_fields(merged) + scoring_missing_fields(merged)
    nse_has_any = any(nse_row.get(field) is not None for field in REQUIRED_FIELDS + SCORING_FIELDS)
    yfinance_has_any = any(fallback_row.get(field) is not None for field in REQUIRED_FIELDS + SCORING_FIELDS)
    if nse_has_any and yfinance_has_any:
        source_used = SOURCE_NSE_PLUS_YFINANCE
    elif nse_has_any:
        source_used = SOURCE_NSE_PRIMARY
    elif yfinance_has_any:
        source_used = SOURCE_YFINANCE_ONLY
    else:
        source_used = SOURCE_FETCH_FAILED
    merged.update({
        "source_used": source_used,
        "missing_fields_before_fallback": missing_before,
        "missing_fields_after_fallback": missing_after,
        "missing_fields": required_missing_fields(merged),
        "is_complete": not required_missing_fields(merged),
        "index_name": index_name,
        "index_memberships": index_memberships or ([index_name] if index_name else []),
        "nse_ok": nse_row.get("nse_ok", False),
        "nse_error": nse_row.get("nse_error"),
        "yfinance_symbol": fallback_row.get("yfinance_symbol", f"{normalize_symbol('NSE', symbol)}.NS"),
        "yfinance_ok": fallback_row.get("yfinance_ok", False),
        "yfinance_error": fallback_row.get("yfinance_error"),
        "updated_at": utc_now_iso(),
    })
    for field in REQUIRED_FIELDS + SCORING_FIELDS:
        merged.setdefault("field_sources", {}).setdefault(field, "MISSING" if merged.get(field) is None else "UNKNOWN")
    return merged


async def ensure_market_data_indexes(db) -> None:
    await db.market_data.create_index([("exchange", 1), ("canonical_symbol", 1)], unique=True)
    await db.market_data.create_index("index_name")
    await db.market_data.create_index("updated_at")


async def upsert_market_data(db, row: dict[str, Any]):
    await ensure_market_data_indexes(db)
    if not is_valid_market_symbol(row.get("canonical_symbol") or row.get("symbol")):
        return SimpleNamespace(upserted_id=None, modified_count=0, matched_count=0)
    document = build_market_data_document(
        row["exchange"],
        row["canonical_symbol"],
        row.get("index_name"),
        row,
        row.get("source_used", SOURCE_NSE_PRIMARY),
        row.get("field_sources"),
    )
    created_at = row.get("created_at") or utc_now_iso()
    identity = {
        "exchange": document["exchange"],
        "canonical_symbol": document["canonical_symbol"],
    }
    if document.get("source_used") == SOURCE_FETCH_FAILED:
        existing = await db.market_data.find_one(identity, {"is_complete": 1})
        if existing and existing.get("is_complete"):
            return await db.market_data.update_one(
                identity,
                {
                    "$set": {
                        "last_fetch_failed_at": utc_now_iso(),
                        "last_fetch_error": document.get("nse_error") or document.get("yfinance_error"),
                    }
                },
            )
    return await db.market_data.update_one(
        identity,
        {"$set": document, "$setOnInsert": {"created_at": created_at}},
        upsert=True,
    )
