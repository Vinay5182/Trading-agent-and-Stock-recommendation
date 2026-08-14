import sys
import os
from datetime import datetime, timezone
import pymongo

def archive_paper_history():
    client = pymongo.MongoClient('mongodb://localhost:27017/')
    db = client['trading_agent_clean']

    target_collections = [
        'trade_journal',
        'paper_trades',
        'paper_signals',
        'paper_market_snapshots',
        'paper_update_runs',
        'paper_update_locks',
        'momentum_tv_confirmations',
        'swing_tv_confirmations',
        'tv_results'
    ]

    untouched_collections = [
        'market_candles',
        'historical_ohlcv',
        'market_context_daily',
        'sector_context_daily',
        'daily_trade_dataset',
        'ml_candidate_daily_progress',
        'ml_candidates',
        'ml_events_log',
        'ml_candidate_decisions',
        'ml_candidate_outcomes',
        'historical_scored_candidates'
    ]

    print("==========================================")
    print("      PAPER TRADING HISTORY ARCHIVAL      ")
    print("==========================================")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print()

    current_utc = datetime.now(timezone.utc).isoformat()

    stats = {}
    docs_to_archive = {}

    # PHASE 1: READ AND PREPARE BULK DATA
    print("--- PHASE 1: Reading collections & preparing bulk metadata ---")
    for col_name in target_collections:
        orig_col = db[col_name]
        docs = list(orig_col.find())
        orig_count = len(docs)
        
        # Attach metadata to every document while preserving original fields & _id
        for doc in docs:
            doc["archived_at"] = current_utc
            doc["archive_reason"] = "Fresh system reset before new trading cycle"
            
        docs_to_archive[col_name] = docs
        stats[col_name] = {
            "orig_count": orig_count,
            "archived_count": 0,
            "deleted_count": 0,
            "remaining_count": orig_count
        }
        print(f"Collection [{col_name:<28}]: Read {orig_count} documents")

    # PHASE 2: BULK INSERT TO ARCHIVE COLLECTIONS
    print("\n--- PHASE 2: Performing bulk insert to archive collections ---")
    for col_name in target_collections:
        archive_col_name = f"{col_name}_archive"
        archive_col = db[archive_col_name]
        docs = docs_to_archive[col_name]
        
        if docs:
            result = archive_col.insert_many(docs, ordered=True)
            inserted_count = len(result.inserted_ids)
        else:
            inserted_count = 0
            
        stats[col_name]["archived_count"] = inserted_count
        print(f"Archive    [{archive_col_name:<28}]: Inserted {inserted_count} documents")

    # PHASE 3: STRICT COUNT VERIFICATION
    print("\n--- PHASE 3: Verifying document counts (Original vs Archived) ---")
    verification_passed = True
    for col_name in target_collections:
        orig_cnt = stats[col_name]["orig_count"]
        arch_cnt = stats[col_name]["archived_count"]
        diff = orig_cnt - arch_cnt
        print(f"[{col_name:<28}] Original: {orig_cnt:<6} | Archived: {arch_cnt:<6} | Difference: {diff}")
        if diff != 0:
            verification_passed = False
            print(f"ERROR: Count mismatch in collection {col_name}! Original={orig_cnt}, Archived={arch_cnt}", file=sys.stderr)

    if not verification_passed:
        print("\nCRITICAL FAILURE: Count verification failed for one or more collections!", file=sys.stderr)
        print("ABORTING DELETION IMMEDIATELY. NO DATA WAS DELETED.", file=sys.stderr)
        sys.exit(1)

    print("\nVERIFICATION SUCCESSFUL: All original document counts match archived document counts exactly (0 difference).")

    # PHASE 4: DELETE FROM ORIGINAL COLLECTIONS
    print("\n--- PHASE 4: Clearing original collections ---")
    for col_name in target_collections:
        orig_col = db[col_name]
        if stats[col_name]["orig_count"] > 0:
            del_result = orig_col.delete_many({})
            deleted_count = del_result.deleted_count
        else:
            deleted_count = 0
            
        remaining = orig_col.count_documents({})
        stats[col_name]["deleted_count"] = deleted_count
        stats[col_name]["remaining_count"] = remaining
        print(f"Cleared   [{col_name:<28}]: Deleted {deleted_count} | Remaining: {remaining}")

    # PHASE 5: FINAL REPORT SUMMARY
    print("\n==========================================================")
    print("                   FINAL SUMMARY REPORT                   ")
    print("==========================================================")
    print(f"{'Collection':<28} | {'Original':<8} | {'Archived':<8} | {'Deleted':<8} | {'Remaining':<8}")
    print("-" * 75)
    for col_name in target_collections:
        s = stats[col_name]
        print(f"{col_name:<28} | {s['orig_count']:<8} | {s['archived_count']:<8} | {s['deleted_count']:<8} | {s['remaining_count']:<8}")
    print("-" * 75)

    print("\n=== VERIFICATION OF UNTOUCHED COLLECTIONS ===")
    for u_col in untouched_collections:
        cnt = db[u_col].count_documents({}) if u_col in db.list_collection_names() else 0
        print(f"Untouched [{u_col:<28}]: count={cnt}")

    print("\nArchival process completed successfully!")

if __name__ == '__main__':
    archive_paper_history()
