import sys
import os
import argparse
import asyncio
from datetime import datetime, timezone

# Add project root to sys.path
sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "backend"))

from motor.motor_asyncio import AsyncIOMotorClient
from backend.utils.symbol_utils import normalize_symbol, build_tradingview_symbol
from pymongo.errors import DuplicateKeyError

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME = os.environ.get("DB_NAME", "trading_agent_clean")

async def run_canonical_symbol_migration(dry_run: bool = True):
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]
    
    print("=" * 100)
    print(f"CANONICAL SYMBOL MIGRATION SCRIPT FOR ALL COLLECTIONS (Dry Run = {dry_run})")
    print("=" * 100)
    
    all_collections = sorted(await db.list_collection_names())
    total_migrated_docs = 0

    for col_name in all_collections:
        coll = db[col_name]
        
        # Find any doc where symbol, canonical_symbol, or tradingview_symbol contains hyphens '-'
        query = {
            "$or": [
                {"symbol": {"$regex": "-"}},
                {"canonical_symbol": {"$regex": "-"}},
                {"tradingview_symbol": {"$regex": "NSE:[^:]*-"}},
            ]
        }
        
        cursor = coll.find(query)
        matching_docs = await cursor.to_list(length=50000)
        
        count = len(matching_docs)
        
        if count == 0:
            continue
            
        print(f"\nCollection '{col_name:<32}': Found {count} document(s) with non-canonical hyphenated symbols.")
        print("-" * 100)
        
        updated_in_col = 0
        deleted_duplicates = 0
        sample_before_after = []
        
        for doc in matching_docs:
            doc_id = doc["_id"]
            set_payload = {}
            
            orig_sym = doc.get("symbol")
            orig_canon = doc.get("canonical_symbol")
            orig_tv = doc.get("tradingview_symbol")
            
            if orig_sym and "-" in orig_sym:
                new_sym = normalize_symbol("NSE", orig_sym)
                set_payload["symbol"] = new_sym
            else:
                new_sym = orig_sym or orig_canon
                
            if orig_canon and "-" in orig_canon:
                new_canon = normalize_symbol("NSE", orig_canon)
                set_payload["canonical_symbol"] = new_canon
            elif "symbol" in set_payload:
                set_payload["canonical_symbol"] = set_payload["symbol"]
                
            if orig_tv and "-" in orig_tv:
                sym_for_tv = set_payload.get("symbol") or set_payload.get("canonical_symbol") or orig_sym
                new_tv = build_tradingview_symbol("NSE", sym_for_tv)
                set_payload["tradingview_symbol"] = new_tv

            if not set_payload:
                continue
                
            set_payload["updated_at"] = datetime.now(timezone.utc).isoformat()
            
            if len(sample_before_after) < 5:
                sample_before_after.append((orig_sym or orig_canon, set_payload.get("symbol") or set_payload.get("canonical_symbol")))
                
            if not dry_run:
                try:
                    res = await coll.update_one({"_id": doc_id}, {"$set": set_payload})
                    updated_in_col += res.modified_count
                except DuplicateKeyError:
                    del_res = await coll.delete_one({"_id": doc_id})
                    deleted_duplicates += del_res.deleted_count
                    print(f"  [Duplicate Removed] Deleted duplicate doc {doc_id} ('{orig_sym}') in '{col_name}'.")
            else:
                updated_in_col += 1
                
        total_migrated_docs += (updated_in_col + deleted_duplicates)
        print(f"  Processed {updated_in_col} updates, {deleted_duplicates} duplicate removals in '{col_name}'. Sample: {sample_before_after}")

    print("\n" + "=" * 100)
    print(f"MIGRATION SUMMARY: Total {total_migrated_docs} document(s) processed across all collections.")
    print("=" * 100)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Canonical Symbol Migration Script")
    parser.add_argument("--apply", action="store_true", help="Apply changes to MongoDB (default is dry run)")
    args = parser.parse_args()
    
    asyncio.run(run_canonical_symbol_migration(dry_run=not args.apply))
