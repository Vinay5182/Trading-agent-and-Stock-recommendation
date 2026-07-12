import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from ai.historical_candidate_generator import dry_run_historical_candidate_generation, persist_historical_candidate_rows, calculate_historical_features, build_historical_candidate_rows
from services.daily_dataset import build_daily_dataset_candidate_snapshot_run
from database import get_database

logger = logging.getLogger(__name__)

async def run_data_collection_batch(
    db: Any,
    date_from: str,
    date_to: str,
    max_symbols: int = 100,
    chunk_days: int = 30,
    dry_run: bool = True,
    persist_historical_candidates: bool = True,
    bridge_to_daily_dataset: bool = True,
    stop_on_error: bool = True,
) -> dict[str, Any]:
    start_date = datetime.strptime(date_from[:10], "%Y-%m-%d")
    end_date = datetime.strptime(date_to[:10], "%Y-%m-%d")
    current_start = start_date
    
    total_generated = 0
    total_persisted_insert = 0
    total_persisted_update = 0
    total_bridge_insert = 0
    total_bridge_update = 0
    all_validation_errors = []
    
    run_id = f"dcr_v1_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    started_at = datetime.utcnow().isoformat() + "Z"
    
    # --- OPTIMIZATION: Pre-fetch and cache DataFrames ---
    cached_dfs = {}
    if persist_historical_candidates and not dry_run:
        pipeline = [{"$group": {"_id": "$canonical_symbol"}}, {"$sort": {"_id": 1}}]
        if max_symbols > 0:
            pipeline.append({"$limit": max_symbols})
        symbols_docs = await db["historical_ohlcv"].aggregate(pipeline).to_list(None)
        symbols = [doc["_id"] for doc in symbols_docs]
        
        for symbol in symbols:
            ohlcv_rows = await db["historical_ohlcv"].find({"canonical_symbol": symbol}).sort("candle_open_at", 1).to_list(None)
            if not ohlcv_rows:
                continue
            df = calculate_historical_features(ohlcv_rows, symbol)
            exchange = ohlcv_rows[0].get("exchange", "NSE")
            cached_dfs[symbol] = {"df": df, "exchange": exchange}
    # ----------------------------------------------------
    
    while current_start <= end_date:
        current_end = current_start + timedelta(days=chunk_days - 1)
        if current_end > end_date:
            current_end = end_date
            
        chunk_date_from = current_start.strftime("%Y-%m-%d")
        chunk_date_to = current_end.strftime("%Y-%m-%d")
        
        logger.info(f"Running chunk: {chunk_date_from} to {chunk_date_to}")
        
        # 1. Generate Historical Candidates (skipped redundant dry-run block in optimized mode)
        total_generated += 0
        
        if persist_historical_candidates and not dry_run:
            all_candidates = []
            for symbol, data in cached_dfs.items():
                df = data["df"]
                exchange = data["exchange"]
                # Filter df for the chunk
                chunk_df = df[(df["candle_open_at"] >= f"{chunk_date_from}T00:00:00Z") & (df["candle_open_at"] <= f"{chunk_date_to}T23:59:59Z")]
                if not chunk_df.empty:
                    candidates = build_historical_candidate_rows(chunk_df, exchange)
                    all_candidates.extend(candidates)
                
            if all_candidates:
                persist_res = await persist_historical_candidate_rows(db, all_candidates, run_id)
                total_persisted_insert += persist_res.get("inserted_count", 0)
                total_persisted_update += persist_res.get("updated_count", 0)
                if persist_res.get("validation_errors"):
                    all_validation_errors.extend(persist_res["validation_errors"])
                    if stop_on_error:
                        break
                    
        # 2. Bridge to daily trade dataset
        if bridge_to_daily_dataset and not dry_run:
            bridge_date = current_start
            while bridge_date <= current_end:
                date_str = bridge_date.strftime("%Y-%m-%d")
                bridge_res = await build_daily_dataset_candidate_snapshot_run(
                    db,
                    trade_date=date_str,
                    limit=3000,
                    dry_run=False,
                    source_collection_name="historical_scored_candidates"
                )
                total_bridge_insert += bridge_res.get("inserted_count", 0)
                total_bridge_update += bridge_res.get("updated_count", 0)
                if bridge_res.get("validation_errors"):
                    all_validation_errors.extend(bridge_res["validation_errors"])
                    if stop_on_error:
                        break
                bridge_date += timedelta(days=1)
                
        if all_validation_errors and stop_on_error:
            break
            
        current_start = current_end + timedelta(days=1)
        
    completed_at = datetime.utcnow().isoformat() + "Z"
    status = "COMPLETED" if not all_validation_errors else "FAILED"
    if not stop_on_error and all_validation_errors:
        status = "PARTIAL_SUCCESS"
        
    manifest = {
        "run_id": run_id,
        "run_type": "DATA_COLLECTION_ONLY",
        "date_from": date_from,
        "date_to": date_to,
        "max_symbols": max_symbols,
        "chunk_days": chunk_days,
        "source_collection": "historical_ohlcv",
        "target_collection": "daily_trade_dataset",
        "dry_run": dry_run,
        "historical_candidates_inserted": total_persisted_insert,
        "historical_candidates_updated": total_persisted_update,
        "daily_dataset_inserted": total_bridge_insert,
        "daily_dataset_updated": total_bridge_update,
        "validation_errors": all_validation_errors,
        "started_at": started_at,
        "completed_at": completed_at,
        "status": status
    }
    
    if not dry_run:
        await db["dataset_build_runs"].insert_one(manifest)
        
    manifest.pop("_id", None)
    return manifest
