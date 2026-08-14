import argparse
import asyncio
import os
import sys
import pandas as pd
import inspect

backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from database import connect_to_mongo, close_mongo_connection, get_database
from services.candidate_trade_outcomes_service import (
    COLLECTION_NAME,
    evaluate_candidate_trade_outcome_doc,
)
from pymongo import UpdateOne


def compute_ohlcv_technical_indicators(ohlcv_rows: list[dict]) -> pd.DataFrame:
    if not ohlcv_rows:
        return pd.DataFrame()
    df = pd.DataFrame(ohlcv_rows)
    df = df.sort_values(by="candle_open_at").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["previous_close"] = df["close"].shift(1)
    df["thirty_day_change_percent"] = ((df["close"] / df["close"].shift(30)) - 1) * 100
    df["change_percent"] = ((df["close"] / df["previous_close"]) - 1) * 100

    high_low = df["high"] - df["low"]
    high_prev_close = (df["high"] - df["previous_close"]).abs()
    low_prev_close = (df["low"] - df["previous_close"]).abs()
    df["tr"] = pd.concat([high_low, high_prev_close, low_prev_close], axis=1).max(axis=1)
    df["atr_14"] = df["tr"].rolling(window=14, min_periods=5).mean().shift(1)

    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14, min_periods=5).mean().shift(1)
    loss = (-delta.where(delta < 0, 0)).rolling(window=14, min_periods=5).mean().shift(1)
    rs = gain / loss.replace(0, 1e-6)
    df["rsi_14"] = 100 - (100 / (1 + rs))
    df["volatility_pct"] = (df["atr_14"] / df["close"]) * 100.0

    return df


async def upgrade_candidate_trade_outcomes(dry_run: bool = False, limit: int = 0) -> dict:
    await connect_to_mongo()
    db = get_database()

    coll = db[COLLECTION_NAME]
    ohlcv_coll = db["historical_ohlcv"]

    cursor = coll.find({})
    if inspect.isawaitable(cursor):
        cursor = await cursor

    if hasattr(cursor, "to_list"):
        res = cursor.to_list(length=None)
        docs = await res if inspect.isawaitable(res) else res
    else:
        docs = list(cursor)

    if limit > 0:
        docs = docs[:limit]

    total_candidates = len(docs)
    upgraded_count = 0
    ops = []

    print(f"Starting trade plan & feature upgrades for {total_candidates} documents (dry_run={dry_run})...")

    # Group docs by canonical symbol for batch candle fetching
    symbols = list({d.get("identity", {}).get("canonical_symbol") for d in docs if d.get("identity", {}).get("canonical_symbol")})

    symbol_df_map = {}
    for sym in symbols:
        c_cursor = ohlcv_coll.find({"canonical_symbol": sym}).sort("candle_open_at", 1)
        if inspect.isawaitable(c_cursor):
            c_cursor = await c_cursor

        if hasattr(c_cursor, "to_list"):
            c_res = c_cursor.to_list(length=None)
            c_rows = await c_res if inspect.isawaitable(c_res) else c_res
        else:
            c_rows = list(c_cursor)

        symbol_df_map[sym] = (c_rows, compute_ohlcv_technical_indicators(c_rows))

    outcome_counts = {}

    for doc in docs:
        candidate_id = doc.get("identity", {}).get("candidate_id")
        sym = doc.get("identity", {}).get("canonical_symbol")
        scan_date = doc.get("identity", {}).get("scan_date", "")[:10]

        if not candidate_id or not sym or not scan_date:
            continue

        c_rows, df_tech = symbol_df_map.get(sym, ([], pd.DataFrame()))

        # Locate scan date row in df_tech
        atr_val = 0.0
        rsi_val = 50.0
        vol_pct_val = 1.5
        t30_chg_val = 0.0
        day_chg_val = 0.0
        scan_close = float(doc.get("trade_plan", {}).get("entry_price") or doc.get("candidate_features", {}).get("normalized_score_inputs", {}).get("current_price", 0.0))

        if not df_tech.empty:
            df_tech["date_str"] = df_tech["candle_open_at"].astype(str).str[:10]
            scan_rows = df_tech[df_tech["date_str"] == scan_date]
            if not scan_rows.empty:
                s_row = scan_rows.iloc[0]
                scan_close = float(s_row["close"]) if pd.notna(s_row.get("close")) else scan_close
                atr_val = float(s_row["atr_14"]) if pd.notna(s_row.get("atr_14")) else 0.0
                rsi_val = float(s_row["rsi_14"]) if pd.notna(s_row.get("rsi_14")) else 50.0
                vol_pct_val = float(s_row["volatility_pct"]) if pd.notna(s_row.get("volatility_pct")) else 1.5
                t30_chg_val = float(s_row["thirty_day_change_percent"]) if pd.notna(s_row.get("thirty_day_change_percent")) else 0.0
                day_chg_val = float(s_row["change_percent"]) if pd.notna(s_row.get("change_percent")) else 0.0

        if atr_val <= 0 and scan_close > 0:
            atr_val = scan_close * 0.02

        # Production-Quality ATR Trade Plan Calculations
        risk_per_share = round(1.5 * atr_val, 4) if atr_val > 0 else round(scan_close * 0.02, 4)
        stop_loss = max(0.01, round(scan_close - risk_per_share, 4))
        target_1 = round(scan_close + (1.5 * risk_per_share), 4)
        target_2 = round(scan_close + (2.5 * risk_per_share), 4)
        target_3 = round(scan_close + (4.0 * risk_per_share), 4)
        rr_ratio = 1.5

        pos_size = int((100000.0 * 0.02) / risk_per_share) if risk_per_share > 0 else 100
        pos_size = max(1, min(pos_size, 5000))

        upgraded_trade_plan = {
            "entry_price": scan_close,
            "stop_loss": stop_loss,
            "target_1": target_1,
            "target_2": target_2,
            "target_3": target_3,
            "risk_reward_ratio": rr_ratio,
            "risk_percent": 2.0,
            "position_size": pos_size,
            "atr_14": round(atr_val, 4),
        }

        upgraded_market_context = {
            "market_trend": "BULLISH" if t30_chg_val > 2.0 else ("BEARISH" if t30_chg_val < -2.0 else "NEUTRAL"),
            "sector_trend": "BULLISH" if day_chg_val > 0 else "NEUTRAL",
            "market_regime": "HIGH_VOLATILITY" if vol_pct_val > 3.0 else ("LOW_VOLATILITY" if vol_pct_val < 1.0 else "BALANCED"),
            "volatility_index": round(vol_pct_val * 10, 2),
        }

        # Update candidate_features.normalized_score_inputs
        score_inputs = dict(doc.get("candidate_features", {}).get("normalized_score_inputs", {}))
        score_inputs["atr_14"] = round(atr_val, 4)
        score_inputs["rsi_14"] = round(rsi_val, 2)
        score_inputs["volatility_pct"] = round(vol_pct_val, 2)

        upgraded_candidate_features = dict(doc.get("candidate_features", {}))
        upgraded_candidate_features["normalized_score_inputs"] = score_inputs

        # Construct doc with upgraded trade plan to re-evaluate outcomes
        doc_copy = dict(doc)
        doc_copy["trade_plan"] = upgraded_trade_plan
        doc_copy["market_context"] = upgraded_market_context
        doc_copy["candidate_features"] = upgraded_candidate_features

        evaluated_sections = evaluate_candidate_trade_outcome_doc(doc_copy, c_rows)
        outcome = evaluated_sections["final_label"]["trade_outcome"]
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
        upgraded_count += 1

        if not dry_run:
            update = {
                "$set": {
                    "trade_plan": upgraded_trade_plan,
                    "market_context": upgraded_market_context,
                    "candidate_features": upgraded_candidate_features,
                    "entry_info": evaluated_sections["entry_info"],
                    "price_evolution": evaluated_sections["price_evolution"],
                    "target_tracking": evaluated_sections["target_tracking"],
                    "stoploss": evaluated_sections["stoploss"],
                    "holding_info": evaluated_sections["holding_info"],
                    "exit": evaluated_sections["exit"],
                    "final_label": evaluated_sections["final_label"],
                    "ml_labels": evaluated_sections["ml_labels"],
                    "audit.updated_at": evaluated_sections["audit"]["updated_at"],
                    "audit.last_evaluated_at": evaluated_sections["audit"]["last_evaluated_at"],
                }
            }
            ops.append(UpdateOne({"identity.candidate_id": candidate_id}, update))

    if ops and not dry_run:
        raw_res = coll.bulk_write(ops, ordered=False)
        if inspect.isawaitable(raw_res):
            res = await raw_res
        else:
            res = raw_res
        modified_count = getattr(res, "modified_count", 0)
    else:
        modified_count = 0

    print("\n=== UPGRADE & RE-EVALUATION RESULTS ===")
    print(f"Total Candidates:   {total_candidates}")
    print(f"Upgraded Candidates:{upgraded_count}")
    print(f"MongoDB Modified:   {modified_count}")
    print("New Outcome Distribution:")
    for outcome, cnt in outcome_counts.items():
        print(f"  {outcome:15s}: {cnt}")

    await close_mongo_connection()
    return {
        "total_candidates": total_candidates,
        "upgraded_count": upgraded_count,
        "modified_count": modified_count,
        "outcome_counts": outcome_counts,
    }


def main():
    parser = argparse.ArgumentParser(description="Upgrade trade plans and technical features in candidate_trade_outcomes.")
    parser.add_argument("--dry-run", action="store_true", help="Preview trade plan upgrades without modifying MongoDB.")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of candidates upgraded.")
    args = parser.parse_args()

    asyncio.run(upgrade_candidate_trade_outcomes(dry_run=args.dry_run, limit=args.limit))


if __name__ == "__main__":
    main()
