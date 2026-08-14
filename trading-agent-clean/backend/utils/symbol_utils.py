import re
from typing import Any


def normalize_symbol(exchange: str, symbol: str) -> str:
    """
    Core canonical symbol normalization function across the entire repository.
    Enforces internal canonical symbol format (uppercase with underscores).
    Replaces internal hyphens '-' with underscores '_' and strips exchange prefixes & suffixes (.NS, .BO, -EQ).
    """
    clean = str(symbol or "").strip().upper()
    if not clean:
        return ""
    
    exchange_prefix = f"{str(exchange or '').strip().upper()}:"
    if clean.startswith(exchange_prefix):
        clean = clean[len(exchange_prefix):]
        
    for suffix in (".NS", ".BO", "-EQ"):
        if clean.endswith(suffix):
            clean = clean[:-len(suffix)]
            
    # Convert internal hyphens to underscores for canonical internal symbol representation
    clean = clean.replace("-", "_").replace(" ", "")
    return clean


def clean_symbol_parts(exchange: str, symbol: str) -> tuple[str, str]:
    clean_exchange = str(exchange or "").strip().upper() or "NSE"
    clean_symbol = normalize_symbol(clean_exchange, symbol)
    return clean_exchange, clean_symbol


def build_tradingview_symbol(exchange: str, symbol: str) -> str:
    clean_exchange, clean_symbol = clean_symbol_parts(exchange, symbol)
    return f"{clean_exchange}:{clean_symbol}"


def get_market_candle_symbol_query(symbol: str, exchange: str = "NSE") -> dict:
    """
    Returns a unified MongoDB query dictionary matching both bare and exchange-prefixed symbol names in market_candles.
    Example: get_market_candle_symbol_query("HEXT") -> {"symbol": {"$in": ["HEXT", "NSE:HEXT"]}}
    """
    clean_ex, clean_sym = clean_symbol_parts(exchange, symbol)
    return {"symbol": {"$in": [clean_sym, f"{clean_ex}:{clean_sym}"]}}

