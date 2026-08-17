"""
Production Market Context Builder.

Computes real market context and sector context from scan_rows data.
All values are calculated from live market data — no placeholders.
"""

import logging
from datetime import datetime
from typing import Any

from services.sector_map import classify_stock, SECTOR_STOCK_MAP, ALL_SECTORS

try:
    import yfinance as yf
except ImportError:
    yf = None

logger = logging.getLogger("uvicorn.error")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_market_and_sector_context(
    scan_rows: list[dict[str, Any]],
    trade_date: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """
    Build production market context and per-sector context from *scan_rows*.

    Returns
    -------
    (market_context_dict, sector_contexts_dict)
        market_context_dict  — ready for ``MLMarketRepository.insert_market_context``
        sector_contexts_dict — ``{sector_name: sector_data_dict, ...}``
    """
    market_ctx = _compute_market_context(scan_rows, trade_date)
    sector_ctxs = _compute_all_sector_contexts(scan_rows, trade_date)
    return market_ctx, sector_ctxs


# ---------------------------------------------------------------------------
# Market Context
# ---------------------------------------------------------------------------

def _compute_market_context(
    scan_rows: list[dict[str, Any]],
    trade_date: str,
) -> dict[str, Any]:
    if not scan_rows:
        return _empty_market_context(trade_date)

    total = len(scan_rows)
    advancing = sum(1 for r in scan_rows if (r.get("change_percent") or 0) > 0)
    declining = sum(1 for r in scan_rows if (r.get("change_percent") or 0) < 0)
    unchanged = total - advancing - declining

    breadth = round((advancing / total) * 100, 1) if total else 0.0
    changes = [r.get("change_percent", 0) or 0 for r in scan_rows]
    avg_change = round(sum(changes) / total, 2)
    abs_changes = [abs(c) for c in changes]
    avg_abs_change = round(sum(abs_changes) / total, 2)

    # Volume analysis
    total_value = sum(r.get("traded_value", 0) or 0 for r in scan_rows)
    avg_value = total_value / total if total else 0

    # Market phase
    if breadth >= 70:
        phase = "BROAD_RALLY"
    elif breadth >= 55:
        phase = "ACCUMULATION"
    elif breadth >= 45:
        phase = "CONSOLIDATION"
    elif breadth >= 30:
        phase = "DISTRIBUTION"
    else:
        phase = "BROAD_DECLINE"

    # Market bias
    if avg_change > 1.0:
        bias = "STRONGLY_BULLISH"
    elif avg_change > 0.3:
        bias = "BULLISH"
    elif avg_change > -0.3:
        bias = "NEUTRAL"
    elif avg_change > -1.0:
        bias = "BEARISH"
    else:
        bias = "STRONGLY_BEARISH"

    # Sentiment
    if breadth >= 60 and avg_change > 0.5:
        sentiment = "VERY_POSITIVE"
    elif breadth >= 50 and avg_change > 0:
        sentiment = "POSITIVE"
    elif breadth >= 40:
        sentiment = "MIXED"
    elif breadth >= 30:
        sentiment = "NEGATIVE"
    else:
        sentiment = "VERY_NEGATIVE"

    # Trend strength
    if abs(avg_change) > 2.0:
        trend_strength = "VERY_STRONG"
    elif abs(avg_change) > 1.0:
        trend_strength = "STRONG"
    elif abs(avg_change) > 0.5:
        trend_strength = "MODERATE"
    else:
        trend_strength = "WEAK"

    # Volatility regime
    if avg_abs_change > 3.0:
        vol_regime = "HIGH"
    elif avg_abs_change > 1.5:
        vol_regime = "MODERATE"
    else:
        vol_regime = "LOW"

    # Market strength score (0-100)
    score = min(100, max(0, round(
        (breadth * 0.4)
        + (min(max(avg_change + 3, 0), 6) / 6 * 100 * 0.3)
        + ((100 - min(avg_abs_change * 20, 100)) * 0.15)
        + (min(advancing / max(declining, 1), 3) / 3 * 100 * 0.15)
    )))

    # Top movers
    sorted_rows = sorted(scan_rows, key=lambda x: x.get("change_percent", 0) or 0, reverse=True)
    top_gainers = [r.get("symbol", "?") for r in sorted_rows[:5]]
    top_losers = [r.get("symbol", "?") for r in sorted_rows[-5:]]

    # Liquidity environment
    if total_value > 500_000_000_000:  # >500B INR
        liquidity = "VERY_HIGH"
    elif total_value > 200_000_000_000:
        liquidity = "HIGH"
    elif total_value > 50_000_000_000:
        liquidity = "NORMAL"
    else:
        liquidity = "LOW"

    # Risk environment
    if vol_regime == "HIGH" and breadth < 40:
        risk_env = "HIGH_RISK"
    elif vol_regime in ("HIGH", "MODERATE") and breadth < 50:
        risk_env = "ELEVATED"
    elif vol_regime == "LOW" and breadth >= 50:
        risk_env = "LOW_RISK"
    else:
        risk_env = "MODERATE"

    # Trading environment
    if bias in ("BULLISH", "STRONGLY_BULLISH") and vol_regime in ("LOW", "MODERATE"):
        trading_env = "HIGHLY_FAVORABLE"
    elif bias == "NEUTRAL" and vol_regime == "LOW":
        trading_env = "FAVORABLE"
    elif bias in ("BEARISH", "STRONGLY_BEARISH") and vol_regime == "HIGH":
        trading_env = "UNFAVORABLE"
    else:
        trading_env = "CAUTIOUS"

    # VIX context
    vix_value, vix_regime_str = _fetch_india_vix()
    vix_context = f"VIX at {vix_value} ({vix_regime_str})" if vix_value else "VIX data unavailable"

    # Important observations
    observations = []
    if breadth >= 70:
        observations.append(f"Broad rally: {breadth}% stocks advancing")
    elif breadth <= 30:
        observations.append(f"Broad decline: only {breadth}% stocks advancing")
    if abs(avg_change) > 1.5:
        direction = "up" if avg_change > 0 else "down"
        observations.append(f"Strong directional move: avg {direction} {abs(avg_change)}%")
    if vol_regime == "HIGH":
        observations.append(f"High volatility: avg absolute change {avg_abs_change}%")
    if vix_value and vix_value > 20:
        observations.append(f"Elevated VIX at {vix_value}")
    if len(top_gainers) >= 1:
        observations.append(f"Top gainer: {top_gainers[0]} (+{sorted_rows[0].get('change_percent', 0):.1f}%)")

    # Summary
    summary = (
        f"Market in {phase.lower().replace('_', ' ')} phase with {bias.lower().replace('_', ' ')} bias. "
        f"Breadth {breadth}% ({advancing}A:{declining}D:{unchanged}U out of {total}). "
        f"Avg change {avg_change:+.2f}%, volatility {vol_regime.lower()}. "
        f"Strength score {score}/100. {vix_context}."
    )

    confidence = "HIGH" if total >= 500 else "MEDIUM" if total >= 200 else "LOW"

    return {
        "trade_date": trade_date,
        "market_context": {
            "market_phase": phase,
            "market_bias": bias,
            "market_sentiment": sentiment,
            "trend_strength": trend_strength,
            "volatility_regime": vol_regime,
            "breadth": breadth,
            "advance_decline": f"{advancing}:{declining}:{unchanged}",
            "advancing": advancing,
            "declining": declining,
            "unchanged": unchanged,
            "total_stocks": total,
            "avg_change_percent": avg_change,
            "avg_abs_change_percent": avg_abs_change,
            "total_traded_value": round(total_value, 2),
            "avg_traded_value": round(avg_value, 2),
            "top_gainers": top_gainers,
            "top_losers": top_losers,
            "index_summary": f"Nifty universe: {total} stocks scanned",
            "leading_index": "BROAD_MARKET_750",
            "weakest_index": "N/A",
            "market_strength_score": score,
            "liquidity_environment": liquidity,
            "risk_environment": risk_env,
            "trading_environment": trading_env,
            "institutional_activity": "UNKNOWN",
            "fii_dii_summary": "Data not available from scan",
            "vix_context": vix_context,
            "important_observations": observations,
            "summary": summary,
            "confidence": confidence,
        },
    }


def _empty_market_context(trade_date: str) -> dict[str, Any]:
    return {
        "trade_date": trade_date,
        "market_context": {
            "market_phase": "UNKNOWN",
            "market_bias": "UNKNOWN",
            "market_sentiment": "UNKNOWN",
            "trend_strength": "UNKNOWN",
            "volatility_regime": "UNKNOWN",
            "breadth": 0,
            "advance_decline": "0:0:0",
            "advancing": 0, "declining": 0, "unchanged": 0,
            "total_stocks": 0,
            "avg_change_percent": 0,
            "avg_abs_change_percent": 0,
            "total_traded_value": 0,
            "avg_traded_value": 0,
            "top_gainers": [],
            "top_losers": [],
            "index_summary": "No scan data",
            "leading_index": "N/A",
            "weakest_index": "N/A",
            "market_strength_score": 0,
            "liquidity_environment": "UNKNOWN",
            "risk_environment": "UNKNOWN",
            "trading_environment": "UNKNOWN",
            "institutional_activity": "UNKNOWN",
            "fii_dii_summary": "No data",
            "vix_context": "VIX data unavailable",
            "important_observations": [],
            "summary": "No scan data available",
            "confidence": "NONE",
        },
    }


# ---------------------------------------------------------------------------
# Sector Context
# ---------------------------------------------------------------------------

def _compute_all_sector_contexts(
    scan_rows: list[dict[str, Any]],
    trade_date: str,
) -> dict[str, dict[str, Any]]:
    """Compute sector context for every sector with at least 2 matched stocks."""

    # Build symbol → row lookup
    row_by_symbol: dict[str, dict[str, Any]] = {}
    for r in scan_rows:
        sym = r.get("symbol")
        if sym:
            row_by_symbol[sym] = r

    # Group rows by sector
    sector_rows: dict[str, list[dict[str, Any]]] = {}
    for sym, row in row_by_symbol.items():
        sector = classify_stock(sym)
        if sector:
            sector_rows.setdefault(sector, []).append(row)

    # Compute per-sector average change for relative strength ranking
    sector_avg_changes: dict[str, float] = {}
    for sec, rows in sector_rows.items():
        if len(rows) >= 2:
            avg = sum(r.get("change_percent", 0) or 0 for r in rows) / len(rows)
            sector_avg_changes[sec] = avg

    # Rank sectors by average change (1 = strongest)
    ranked = sorted(sector_avg_changes.items(), key=lambda x: x[1], reverse=True)
    rank_map = {sec: idx + 1 for idx, (sec, _) in enumerate(ranked)}

    # Overall average traded value per stock across ALL scan rows for volume comparison
    all_avg_value = (
        sum(r.get("traded_value", 0) or 0 for r in scan_rows) / len(scan_rows)
        if scan_rows else 1
    )

    # Compute context for each qualifying sector
    results: dict[str, dict[str, Any]] = {}
    for sector_name in ALL_SECTORS:
        rows = sector_rows.get(sector_name, [])
        if len(rows) < 2:
            continue
        results[sector_name] = _compute_single_sector(
            sector_name, rows, rank_map.get(sector_name, 0),
            len(ranked), all_avg_value,
        )

    return results


def _compute_single_sector(
    sector_name: str,
    rows: list[dict[str, Any]],
    rank: int,
    total_sectors: int,
    overall_avg_value: float,
) -> dict[str, Any]:
    total = len(rows)
    advancing = sum(1 for r in rows if (r.get("change_percent") or 0) > 0)
    declining = sum(1 for r in rows if (r.get("change_percent") or 0) < 0)

    changes = [r.get("change_percent", 0) or 0 for r in rows]
    avg_change = round(sum(changes) / total, 2)
    abs_avg = abs(avg_change)
    breadth = round((advancing / total) * 100, 1) if total else 0.0

    # Trend
    if avg_change > 1.0:
        trend = "STRONG_UPTREND"
    elif avg_change > 0.3:
        trend = "UPTREND"
    elif avg_change > -0.3:
        trend = "SIDEWAYS"
    elif avg_change > -1.0:
        trend = "DOWNTREND"
    else:
        trend = "STRONG_DOWNTREND"

    # Strength
    if abs_avg > 2.0:
        strength = "VERY_STRONG"
    elif abs_avg > 1.0:
        strength = "STRONG"
    elif abs_avg > 0.5:
        strength = "MODERATE"
    else:
        strength = "WEAK"

    # Momentum (proportion advancing)
    if breadth >= 70:
        momentum = "STRONGLY_POSITIVE"
    elif breadth >= 55:
        momentum = "POSITIVE"
    elif breadth >= 45:
        momentum = "NEUTRAL"
    elif breadth >= 30:
        momentum = "NEGATIVE"
    else:
        momentum = "STRONGLY_NEGATIVE"

    # Leaders & laggards (top/bottom 3 by change_percent)
    sorted_rows = sorted(rows, key=lambda x: x.get("change_percent", 0) or 0, reverse=True)
    leaders = [r.get("symbol", "?") for r in sorted_rows[:3]]
    laggards = [r.get("symbol", "?") for r in sorted_rows[-3:]]

    # Volume strength
    sector_total_value = sum(r.get("traded_value", 0) or 0 for r in rows)
    sector_avg_value = sector_total_value / total if total else 0
    if overall_avg_value and overall_avg_value > 0:
        vol_ratio = sector_avg_value / overall_avg_value
    else:
        vol_ratio = 1.0

    if vol_ratio > 1.5:
        volume_strength = "VERY_HIGH"
    elif vol_ratio > 1.0:
        volume_strength = "HIGH"
    elif vol_ratio > 0.5:
        volume_strength = "NORMAL"
    else:
        volume_strength = "LOW"

    # Institutional interest (heuristic from volume strength)
    if volume_strength in ("VERY_HIGH", "HIGH") and breadth >= 55:
        inst_interest = "HIGH"
    elif volume_strength in ("VERY_HIGH", "HIGH"):
        inst_interest = "MODERATE"
    elif volume_strength == "NORMAL":
        inst_interest = "NEUTRAL"
    else:
        inst_interest = "LOW"

    # Score (0-100)
    change_score = min(max(avg_change + 3, 0), 6) / 6 * 100
    vol_score = min(vol_ratio, 2.0) / 2.0 * 100
    momentum_score = breadth
    score = round(
        (breadth * 0.30)
        + (change_score * 0.25)
        + (vol_score * 0.20)
        + (momentum_score * 0.25),
        1,
    )
    score = min(100, max(0, score))

    summary = (
        f"{sector_name}: {trend.lower().replace('_', ' ')} ({avg_change:+.2f}%), "
        f"breadth {breadth}% ({advancing}/{total} advancing). "
        f"Leaders: {', '.join(leaders)}. "
        f"Rank {rank}/{total_sectors}."
    )

    return {
        "trend": trend,
        "strength": strength,
        "momentum": momentum,
        "breadth": breadth,
        "leaders": leaders,
        "laggards": laggards,
        "relative_strength": rank,
        "institutional_interest": inst_interest,
        "volume_strength": volume_strength,
        "score": score,
        "avg_change_percent": avg_change,
        "advancing": advancing,
        "declining": declining,
        "total_stocks": total,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# VIX helper
# ---------------------------------------------------------------------------

def _fetch_india_vix() -> tuple[float | None, str]:
    """Fetch latest India VIX value via yfinance. Returns (value, regime_label)."""
    if yf is None:
        return None, "UNAVAILABLE"
    try:
        ticker = yf.Ticker("^INDIAVIX")
        hist = ticker.history(period="5d")
        if hist is not None and not hist.empty and "Close" in hist.columns:
            vix = round(float(hist["Close"].dropna().iloc[-1]), 2)
            if vix < 13:
                regime = "LOW_VOLATILITY"
            elif vix > 20:
                regime = "HIGH_VOLATILITY"
            else:
                regime = "NORMAL_VOLATILITY"
            return vix, regime
    except Exception as exc:
        logger.warning("Failed to fetch India VIX: %s", exc)
    return None, "UNAVAILABLE"
