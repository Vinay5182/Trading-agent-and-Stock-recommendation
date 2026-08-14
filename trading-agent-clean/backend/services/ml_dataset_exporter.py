import hashlib
import json
import os
import sys
import inspect
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple
import pandas as pd

COLLECTION_NAME = "candidate_trade_outcomes"

FEATURE_COLUMNS = [
    # Price features
    "current_price",
    "previous_close",
    "open_price",
    "day_high",
    "day_low",
    "change_percent",
    "thirty_day_change_percent",
    # Momentum & volume features
    "score",
    "nse_score",
    "momentum_score",
    "relative_volume",
    "traded_volume",
    "traded_value",
    # Technical indicators
    "atr_14",
    "rsi_14",
    "volatility_pct",
    # Trade plan parameters
    "entry_price",
    "stop_loss",
    "target_1",
    "target_2",
    "target_3",
    "risk_reward_ratio",
    "position_size",
    # Market context
    "volatility_index",
    "market_trend",
    "sector_trend",
    "market_regime",
    # Binary flags
    "swing_candidate",
    "momentum_candidate",
]

TARGET_COLUMNS = [
    "trade_outcome",
    "binary_label",
    "future_return_5d",
    "future_return_10d",
    "future_return_20d",
    "best_return",
    "worst_drawdown",
]

METADATA_COLUMNS = [
    "candidate_id",
    "canonical_symbol",
    "symbol",
    "scan_date",
    "timeframe",
    "strategy_type",
]


async def fetch_candidate_trade_outcomes(db: Any) -> List[Dict[str, Any]]:
    coll = db[COLLECTION_NAME] if hasattr(db, "__getitem__") else db.get_collection(COLLECTION_NAME)
    cursor = coll.find({})
    if inspect.isawaitable(cursor):
        cursor = await cursor

    if hasattr(cursor, "to_list"):
        res = cursor.to_list(length=None)
        return await res if inspect.isawaitable(res) else res
    return list(cursor)


def parse_doc_to_row(doc: Dict[str, Any]) -> Dict[str, Any]:
    identity = doc.get("identity", {})
    tp = doc.get("trade_plan", {})
    mc = doc.get("market_context", {})
    cf = doc.get("candidate_features", {})
    score_inputs = cf.get("normalized_score_inputs", {})
    final_label = doc.get("final_label", {})
    ml_labels = doc.get("ml_labels", {})

    trade_outcome = final_label.get("trade_outcome", "PENDING")
    binary_label = 1 if trade_outcome == "WIN" else (0 if trade_outcome in {"LOSS", "NO_ENTRY"} else None)

    row = {
        # Metadata
        "candidate_id": identity.get("candidate_id"),
        "canonical_symbol": identity.get("canonical_symbol"),
        "symbol": identity.get("symbol"),
        "scan_date": identity.get("scan_date", "")[:10],
        "timeframe": identity.get("timeframe", "1D"),
        "strategy_type": cf.get("strategy_type", "MULTI"),
        # Price
        "current_price": float(score_inputs.get("current_price") or tp.get("entry_price") or 0.0),
        "previous_close": float(score_inputs.get("previous_close") or 0.0),
        "open_price": float(score_inputs.get("open_price") or 0.0),
        "day_high": float(score_inputs.get("day_high") or 0.0),
        "day_low": float(score_inputs.get("day_low") or 0.0),
        "change_percent": float(score_inputs.get("change_percent") or 0.0),
        "thirty_day_change_percent": float(score_inputs.get("thirty_day_change_percent") or 0.0),
        # Momentum & Volume
        "score": float(cf.get("score") or 0.0),
        "nse_score": float(cf.get("nse_score") or cf.get("score") or 0.0),
        "momentum_score": float(cf.get("momentum_score") or 0.0),
        "relative_volume": float(score_inputs.get("relative_volume") or 1.0),
        "traded_volume": float(score_inputs.get("traded_volume") or 0.0),
        "traded_value": float(score_inputs.get("traded_value") or 0.0),
        # Technicals
        "atr_14": float(score_inputs.get("atr_14") or tp.get("atr_14") or 0.0),
        "rsi_14": float(score_inputs.get("rsi_14") or 50.0),
        "volatility_pct": float(score_inputs.get("volatility_pct") or 1.5),
        # Trade Plan
        "entry_price": float(tp.get("entry_price") or 0.0),
        "stop_loss": float(tp.get("stop_loss") or 0.0),
        "target_1": float(tp.get("target_1") or 0.0),
        "target_2": float(tp.get("target_2") or 0.0),
        "target_3": float(tp.get("target_3") or 0.0),
        "risk_reward_ratio": float(tp.get("risk_reward_ratio") or 1.5),
        "position_size": int(tp.get("position_size") or 0),
        # Market Context
        "volatility_index": float(mc.get("volatility_index") or 15.0),
        "market_trend": mc.get("market_trend", "NEUTRAL"),
        "sector_trend": mc.get("sector_trend", "NEUTRAL"),
        "market_regime": mc.get("market_regime", "BALANCED"),
        # Flags
        "swing_candidate": bool(cf.get("swing_candidate", False)),
        "momentum_candidate": bool(cf.get("momentum_candidate", False)),
        # Targets
        "trade_outcome": trade_outcome,
        "binary_label": binary_label,
        "future_return_5d": ml_labels.get("future_return_5d"),
        "future_return_10d": ml_labels.get("future_return_10d"),
        "future_return_20d": ml_labels.get("future_return_20d"),
        "best_return": ml_labels.get("best_return"),
        "worst_drawdown": ml_labels.get("worst_drawdown"),
    }
    return row


def build_ml_dataset_dataframe(docs: List[Dict[str, Any]]) -> pd.DataFrame:
    valid_docs = []
    seen_candidate_ids = set()
    for d in docs:
        cid = (d.get("identity", {}) or {}).get("candidate_id") or d.get("candidate_id")
        if not cid:
            continue
        if cid in seen_candidate_ids:
            continue
        seen_candidate_ids.add(cid)
        valid_docs.append(d)

    rows = [parse_doc_to_row(d) for d in valid_docs]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    # Chronological sorting by scan_date and candidate_id
    df = df.sort_values(by=["scan_date", "candidate_id"]).reset_index(drop=True)
    return df



def split_ml_dataset(
    df: pd.DataFrame,
    train_ratio: float = 0.70,
    val_ratio: float = 0.15,
    test_ratio: float = 0.15,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Chronological dataset splitting to prevent temporal lookahead leakage.
    """
    if df.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    n = len(df)
    n_train = int(n * train_ratio)
    n_val = int(n * val_ratio)

    df_train = df.iloc[:n_train].copy().reset_index(drop=True)
    df_val = df.iloc[n_train : n_train + n_val].copy().reset_index(drop=True)
    df_test = df.iloc[n_train + n_val :].copy().reset_index(drop=True)

    return df_train, df_val, df_test


def compute_schema_hash(df: pd.DataFrame) -> str:
    schema_str = f"{list(df.columns)}|{list(df.dtypes)}"
    return hashlib.sha256(schema_str.encode("utf-8")).hexdigest()[:16]


async def export_versioned_ml_dataset(
    db: Any,
    output_dir: str = "datasets",
    version: str = "v1.0.0",
    format_parquet: bool = True,
    format_csv: bool = True,
    train_only: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    docs = await fetch_candidate_trade_outcomes(db)
    df_all = build_ml_dataset_dataframe(docs)

    if df_all.empty:
        return {"total_records": 0, "error": "No records found in candidate_trade_outcomes"}

    df_train, df_val, df_test = split_ml_dataset(df_all)

    schema_hash = compute_schema_hash(df_all)
    now_iso = datetime.now(timezone.utc).isoformat()

    manifest = {
        "dataset_version": version,
        "export_timestamp": now_iso,
        "source_collection": COLLECTION_NAME,
        "schema_hash": schema_hash,
        "total_records": len(df_all),
        "split_counts": {
            "train": len(df_train),
            "val": len(df_val),
            "test": len(df_test),
        },
        "date_ranges": {
            "train": [df_train["scan_date"].min(), df_train["scan_date"].max()] if not df_train.empty else [],
            "val": [df_val["scan_date"].min(), df_val["scan_date"].max()] if not df_val.empty else [],
            "test": [df_test["scan_date"].min(), df_test["scan_date"].max()] if not df_test.empty else [],
        },
        "feature_count": len(FEATURE_COLUMNS),
        "feature_manifest": FEATURE_COLUMNS,
        "target_count": len(TARGET_COLUMNS),
        "target_manifest": TARGET_COLUMNS,
        "metadata_manifest": METADATA_COLUMNS,
    }

    if dry_run:
        return {
            "dry_run": True,
            "manifest": manifest,
            "df_shapes": {
                "all": df_all.shape,
                "train": df_train.shape,
                "val": df_val.shape,
                "test": df_test.shape,
            },
        }

    os.makedirs(output_dir, exist_ok=True)

    exported_files = []

    # Export Splits
    splits = [("training", df_train)]
    if not train_only:
        splits.extend([("validation", df_val), ("test", df_test)])

    for split_name, df_split in splits:
        if format_parquet:
            pq_path = os.path.join(output_dir, f"{split_name}_dataset_{version}.parquet")
            try:
                df_split.to_parquet(pq_path, index=False)
                exported_files.append(pq_path)
            except Exception as pe:
                manifest["parquet_warning"] = f"Parquet export skipped: {pe}"

        if format_csv or "parquet_warning" in manifest:
            csv_path = os.path.join(output_dir, f"{split_name}_dataset_{version}.csv")
            df_split.to_csv(csv_path, index=False)
            exported_files.append(csv_path)

    # Export Full Dataset
    if format_parquet:
        all_pq = os.path.join(output_dir, f"full_dataset_{version}.parquet")
        try:
            df_all.to_parquet(all_pq, index=False)
            exported_files.append(all_pq)
        except Exception:
            pass

    if format_csv or "parquet_warning" in manifest:
        all_csv = os.path.join(output_dir, f"full_dataset_{version}.csv")
        df_all.to_csv(all_csv, index=False)
        exported_files.append(all_csv)


    # Export Manifest JSON
    manifest_path = os.path.join(output_dir, f"dataset_manifest_{version}.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    exported_files.append(manifest_path)

    return {
        "dry_run": False,
        "manifest": manifest,
        "exported_files": exported_files,
        "output_directory": os.path.abspath(output_dir),
    }
