from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any
from urllib.parse import quote

import requests

from data_provider import clean_number, normalize_symbol

logger = logging.getLogger(__name__)

NSE_INDEX_NAME_MAP = {
    "NIFTY_50": "NIFTY 50",
    "NIFTY_NEXT_50": "NIFTY NEXT 50",
    "NIFTY_100": "NIFTY 100",
    "NIFTY_200": "NIFTY 200",
    "NIFTY_500": "NIFTY 500",
    "NIFTY_MIDCAP_50": "NIFTY MIDCAP 50",
    "NIFTY_MIDCAP_100": "NIFTY MIDCAP 100",
    "NIFTY_MIDCAP_150": "NIFTY MIDCAP 150",
    "NIFTY_MIDCAP_SELECT": "NIFTY MIDCAP SELECT",
    "NIFTY_SMALLCAP_50": "NIFTY SMALLCAP 50",
    "NIFTY_SMALLCAP_100": "NIFTY SMALLCAP 100",
    "NIFTY_SMALLCAP_250": "NIFTY SMALLCAP 250",
    "NIFTY_MICROCAP_250": "NIFTY MICROCAP 250",
    "NIFTY_MIDSMALLCAP_400": "NIFTY MIDSMALLCAP 400",
    "NIFTY_TOTAL_MARKET": "NIFTY TOTAL MARKET",
    "BROAD_MARKET_750": "NIFTY TOTAL MARKET",
    "ALL_SUPPORTED": "NIFTY TOTAL MARKET",
}

BROAD_MARKET_COMPONENT_INDEXES = [
    "NIFTY_50",
    "NIFTY_NEXT_50",
    "NIFTY_MIDCAP_150",
    "NIFTY_500",
    "NIFTY_SMALLCAP_250",
    "NIFTY_MICROCAP_250",
    "NIFTY_MIDSMALLCAP_400",
]

NSE_INDEX_ALIASES = {
    "NIFTY_SMALLCAP_50": ["NIFTY SMALLCAP 50", "NIFTY SMLCAP 50"],
    "NIFTY_SMALLCAP_100": ["NIFTY SMALLCAP 100", "NIFTY SMLCAP 100"],
    "NIFTY_SMALLCAP_250": ["NIFTY SMALLCAP 250", "NIFTY SMLCAP 250"],
    "NIFTY_MICROCAP_250": ["NIFTY MICROCAP 250", "NIFTY MICROCAP250"],
    "NIFTY_MIDSMALLCAP_400": ["NIFTY MIDSMALLCAP 400", "NIFTY MIDSMALLCAP400"],
}


@dataclass
class NseBatchResult:
    ok: bool
    index_name: str
    nse_index_name: str
    quote_map: dict[str, dict[str, Any]]
    error: str | None = None
    status_code: int | None = None
    url: str | None = None
    diagnostics: dict[str, Any] | None = None
    clean_index_name: str | None = None
    attempted_aliases: list[dict[str, Any]] | None = None
    selected_alias: str | None = None

    @property
    def quotes_count(self) -> int:
        return len(self.quote_map)

    @property
    def nse_batch_url(self) -> str | None:
        return self.url

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "quotes_count": self.quotes_count,
            "quote_map": self.quote_map,
            "error": self.error,
            "status_code": self.status_code,
            "nse_batch_url": self.url,
            "nse_index_name": self.nse_index_name,
            "diagnostics": self.diagnostics or {},
            "clean_index_name": self.clean_index_name or self.index_name,
            "attempted_aliases": self.attempted_aliases or [],
            "selected_alias": self.selected_alias,
        }


def nse_headers() -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/market-data/live-equity-market",
        "Connection": "keep-alive",
    }


def create_nse_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(nse_headers())
    return session


def extract_quote(row: dict[str, Any]) -> dict[str, Any]:
    symbol = normalize_symbol("NSE", row.get("symbol") or row.get("identifier") or "")
    meta = row.get("meta") or {}
    quote = {
        "exchange": "NSE",
        "symbol": symbol,
        "canonical_symbol": symbol,
        "current_price": clean_number(row.get("lastPrice")),
        "previous_close": clean_number(row.get("previousClose")),
        "open_price": clean_number(row.get("open")),
        "day_high": clean_number(row.get("dayHigh")),
        "day_low": clean_number(row.get("dayLow")),
        "traded_volume": clean_number(row.get("totalTradedVolume")),
        "traded_value": clean_number(row.get("totalTradedValue")),
        "change_percent": clean_number(row.get("pChange")),
        "thirty_day_change_percent": clean_number(row.get("perChange30d")),
        "company_name": meta.get("companyName"),
        "industry": meta.get("industry"),
        "isin": meta.get("isin"),
        "nse_ok": True,
        "nse_error": None,
        "field_sources": {},
    }
    for field in (
        "current_price",
        "previous_close",
        "open_price",
        "day_high",
        "day_low",
        "traded_volume",
        "traded_value",
        "change_percent",
        "thirty_day_change_percent",
    ):
        if quote.get(field) is not None:
            quote["field_sources"][field] = "NSE"
    return quote


def _canonical_index_key(index_name: str) -> str:
    clean_index = index_name.strip().upper()
    if clean_index in NSE_INDEX_NAME_MAP:
        return clean_index
    for key, display_name in NSE_INDEX_NAME_MAP.items():
        if clean_index == display_name.upper():
            return key
    return clean_index.replace(" ", "_")


def _index_display_name(index_name: str) -> str:
    clean_index = _canonical_index_key(index_name)
    return NSE_INDEX_NAME_MAP.get(clean_index, clean_index.replace("_", " "))


def _index_aliases(index_name: str) -> list[str]:
    clean_index = _canonical_index_key(index_name)
    return NSE_INDEX_ALIASES.get(clean_index, [_index_display_name(clean_index)])


def fetch_nse_index_quotes(index_name: str, timeout: int = 10, retries: int = 2) -> NseBatchResult:
    clean_index = _canonical_index_key(index_name)
    primary_index_name = _index_display_name(clean_index)
    attempted_aliases = []
    last_error = None
    status_code = None
    last_url = None
    for alias in _index_aliases(clean_index):
        url = "https://www.nseindia.com/api/equity-stock-indices?index=" + quote(alias)
        last_url = url
        attempt = {
            "nse_index_name": alias,
            "url": url,
            "status_code": None,
            "quotes_count": 0,
            "error": None,
        }
        for _ in range(retries):
            try:
                session = create_nse_session()
                session.get("https://www.nseindia.com/market-data/live-equity-market", timeout=timeout)
                response = session.get(url, timeout=timeout)
                status_code = response.status_code
                attempt["status_code"] = status_code
                response.raise_for_status()
                payload = response.json()
                data = payload.get("data") or []
                if not data:
                    last_error = "EMPTY_NSE_INDEX_PAYLOAD"
                    attempt["quotes_count"] = 0
                    attempt["error"] = None
                    continue
                quote_map = {}
                for row in data:
                    quote_row = extract_quote(row)
                    if quote_row["canonical_symbol"]:
                        quote_row["nse_source_index"] = alias
                        quote_row["nse_source_indexes"] = [alias]
                        quote_map[quote_row["canonical_symbol"]] = quote_row
                attempt["quotes_count"] = len(quote_map)
                attempted_aliases.append(attempt)
                return NseBatchResult(
                    True,
                    clean_index,
                    alias,
                    quote_map,
                    None,
                    status_code,
                    url,
                    clean_index_name=clean_index,
                    attempted_aliases=attempted_aliases,
                    selected_alias=alias,
                )
            except Exception as exc:
                last_error = str(exc)
                attempt["error"] = last_error
        attempted_aliases.append(attempt)
    return NseBatchResult(
        False,
        clean_index,
        primary_index_name,
        {},
        last_error,
        status_code,
        last_url,
        clean_index_name=clean_index,
        attempted_aliases=attempted_aliases,
        selected_alias=None,
    )


def _index_diagnostics(batch: NseBatchResult) -> dict[str, Any]:
    return {
        "clean_index_name": batch.clean_index_name or batch.index_name,
        "nse_index_name": batch.nse_index_name,
        "url": batch.url,
        "status_code": batch.status_code,
        "ok": batch.ok,
        "quotes_count": len(batch.quote_map),
        "error": batch.error,
        "first_symbols_sample": list(batch.quote_map.keys())[:10],
        "attempted_aliases": batch.attempted_aliases or [],
        "selected_alias": batch.selected_alias,
    }


def _merge_nse_quote(
    quote_map: dict[str, dict[str, Any]],
    quote_row: dict[str, Any],
    source_index: str,
) -> None:
    symbol = quote_row.get("canonical_symbol")
    if not symbol:
        return
    incoming = quote_row.copy()
    incoming["nse_source_index"] = source_index
    incoming["nse_source_indexes"] = [source_index]
    existing = quote_map.get(symbol)
    if not existing:
        quote_map[symbol] = incoming
        return

    source_indexes = existing.setdefault("nse_source_indexes", [])
    if source_index not in source_indexes:
        source_indexes.append(source_index)
    for field in (
        "current_price",
        "previous_close",
        "open_price",
        "day_high",
        "day_low",
        "traded_volume",
        "traded_value",
        "change_percent",
        "thirty_day_change_percent",
    ):
        if existing.get(field) is None and incoming.get(field) is not None:
            existing[field] = incoming[field]
            existing.setdefault("field_sources", {})[field] = "NSE"


def fetch_broad_market_nse_quotes(timeout: int = 10, retries: int = 2) -> NseBatchResult:
    primary_index = "NIFTY_TOTAL_MARKET"
    primary_name = _index_display_name(primary_index)
    requested_indexes = [primary_name]
    successful_indexes = []
    failed_indexes = []
    empty_indexes = []
    errors_by_index = {}
    per_index_diagnostics = []
    quote_map: dict[str, dict[str, Any]] = {}
    total_nse_quotes = 0

    primary = fetch_nse_index_quotes(primary_index, timeout=timeout, retries=retries)
    per_index_diagnostics.append(_index_diagnostics(primary))
    primary_empty = not bool(primary.quote_map)
    if primary.quote_map:
        successful_indexes.append(primary_name)
        total_nse_quotes += len(primary.quote_map)
        for quote_row in primary.quote_map.values():
            _merge_nse_quote(quote_map, quote_row, primary_name)
    else:
        if primary.error == "EMPTY_NSE_INDEX_PAYLOAD":
            empty_indexes.append(primary_name)
        else:
            failed_indexes.append(primary_name)
        errors_by_index[primary_name] = primary.error or "EMPTY_NSE_INDEX_PAYLOAD"
        logger.warning("NIFTY_TOTAL_MARKET_EMPTY")
        for index_key in BROAD_MARKET_COMPONENT_INDEXES:
            index_name = _index_display_name(index_key)
            requested_indexes.append(index_name)
            batch = fetch_nse_index_quotes(index_key, timeout=timeout, retries=retries)
            per_index_diagnostics.append(_index_diagnostics(batch))
            logger.info("NSE index %s returned: %s", index_name, len(batch.quote_map))
            if batch.quote_map:
                selected_index = batch.selected_alias or batch.nse_index_name
                successful_indexes.append(selected_index)
                total_nse_quotes += len(batch.quote_map)
                for quote_row in batch.quote_map.values():
                    _merge_nse_quote(quote_map, quote_row, selected_index)
            else:
                if batch.error == "EMPTY_NSE_INDEX_PAYLOAD":
                    empty_indexes.append(index_name)
                else:
                    failed_indexes.append(index_name)
                errors_by_index[index_name] = batch.error or "EMPTY_NSE_INDEX_PAYLOAD"

    diagnostics = {
        "strategy": "TOTAL_MARKET_THEN_COMPONENT_INDEXES",
        "primary_index": primary_name,
        "primary_empty": primary_empty,
        "requested_indexes": requested_indexes,
        "successful_indexes": successful_indexes,
        "failed_indexes": failed_indexes,
        "empty_indexes": empty_indexes,
        "total_nse_quotes": total_nse_quotes,
        "deduped_nse_quotes": len(quote_map),
        "per_index_diagnostics": per_index_diagnostics,
        "errors_by_index": errors_by_index,
    }
    if primary_empty and not quote_map:
        logger.warning("NSE_COMPONENTS_EMPTY_YFINANCE_FULL_FALLBACK")
    return NseBatchResult(
        ok=bool(quote_map),
        index_name="BROAD_MARKET_750",
        nse_index_name=primary_name,
        quote_map=quote_map,
        error=None if quote_map else errors_by_index.get(primary_name),
        status_code=primary.status_code,
        url=primary.url,
        diagnostics=diagnostics,
    )


def get_nse_index_quotes(index_name: str) -> dict[str, Any]:
    return fetch_nse_index_quotes(index_name).as_dict()
