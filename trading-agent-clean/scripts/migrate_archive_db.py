import sys
import os
from datetime import datetime, timezone
import pymongo
from pymongo import ReplaceOne

def migrate_archive_db():
    client = pymongo.MongoClient('mongodb://localhost:27017/')
    source_db = client['trading_agent_clean']
    target_db = client['trading_agent_archive']

    archive_mapping = [
        ('trade_journal_archive', 'trade_journal'),
        ('paper_trades_archive', 'paper_trades'),
        ('paper_signals_archive', 'paper_signals'),
        ('paper_market_snapshots_archive', 'paper_market_snapshots'),
        ('paper_update_runs_archive', 'paper_update_runs'),
        ('paper_update_locks_archive', 'paper_update_locks'),
        ('momentum_tv_confirmations_archive', 'momentum_tv_confirmations'),
        ('swing_tv_confirmations_archive', 'swing_tv_confirmations'),
        ('tv_results_archive', 'tv_results')
    ]

    snapshot_date = "2026-07-23"
    snapshot_id = "campaign_2026_07_23"

    print("==========================================================")
    print("      ARCHIVE DATABASE REORGANIZATION & MIGRATION        ")
    print("==========================================================")
    print(f"Timestamp   : {datetime.now(timezone.utc).isoformat()}")
    print(f"Source DB   : trading_agent_clean")
    print(f"Target DB   : trading_agent_archive")
    print(f"Snapshot ID : {snapshot_id} (Date: {snapshot_date})")
    print()

    stats = {}
    docs_to_migrate = {}
    indexes_to_migrate = {}

    # PHASE 1: READ SOURCE DATA & PREPARE METADATA
    print("--- PHASE 1: Reading source archive collections ---")
    source_cols = source_db.list_collection_names()

    for src_col, tgt_col in archive_mapping:
        exists = src_col in source_cols
        if exists:
            docs = list(source_db[src_col].find())
            try:
                indexes = list(source_db[src_col].list_indexes())
            except Exception:
                indexes = []
        else:
            docs = []
            indexes = []

        src_count = len(docs)

        # Enrich each document with campaign snapshot metadata while preserving _id & fields
        for doc in docs:
            doc["snapshot_date"] = snapshot_date
            doc["snapshot_id"] = snapshot_id

        docs_to_migrate[src_col] = docs
        indexes_to_migrate[src_col] = indexes

        stats[src_col] = {
            "target_col": tgt_col,
            "source_count": src_count,
            "dest_count": 0,
            "verified": False,
            "dropped_from_source": False
        }
        print(f"Source [{src_col:<36}]: Read {src_count} documents")

    # PHASE 2: BULK UPSERT TO TARGET DATABASE
    print("\n--- PHASE 2: Bulk upserting documents to trading_agent_archive ---")
    for src_col, tgt_col in archive_mapping:
        docs = docs_to_migrate[src_col]
        tgt_collection = target_db[tgt_col]

        if docs:
            operations = [ReplaceOne({'_id': doc['_id']}, doc, upsert=True) for doc in docs]
            bulk_res = tgt_collection.bulk_write(operations)
            upserted_or_modified = bulk_res.upserted_count + bulk_res.modified_count + bulk_res.matched_count
        else:
            upserted_or_modified = 0

        # Copy non-_id index specifications
        src_indexes = indexes_to_migrate[src_col]
        for idx_spec in src_indexes:
            idx_name = idx_spec.get('name')
            if idx_name == '_id_':
                continue
            key_pattern = list(idx_spec.get('key').items())
            options = {k: v for k, v in idx_spec.items() if k not in ['v', 'key', 'ns']}
            try:
                tgt_collection.create_index(key_pattern, **options)
            except Exception as e:
                pass

        dest_count = tgt_collection.count_documents({"snapshot_id": snapshot_id})
        stats[src_col]["dest_count"] = dest_count
        print(f"Target [{target_db.name}.{tgt_col:<28}]: Processed {len(docs)} | Total matching snapshot={dest_count}")

    # PHASE 3: STRICT COUNT VERIFICATION
    print("\n--- PHASE 3: Verifying counts (Source vs Destination) ---")
    verification_passed = True

    for src_col, tgt_col in archive_mapping:
        s_cnt = stats[src_col]["source_count"]
        d_cnt = stats[src_col]["dest_count"]
        diff = s_cnt - d_cnt
        is_valid = (diff == 0)
        stats[src_col]["verified"] = is_valid
        print(f"Verification [{src_col:<36} -> {tgt_col:<28}]: Source={s_cnt:<6} | Dest={d_cnt:<6} | Diff={diff}")
        if not is_valid:
            verification_passed = False
            print(f"ERROR: Verification failed for {src_col}! Source={s_cnt}, Dest={d_cnt}", file=sys.stderr)

    if not verification_passed:
        print("\nCRITICAL FAILURE: Count verification failed for one or more collections!", file=sys.stderr)
        print("ABORTING SOURCE DROPS IMMEDIATELY. NO SOURCE DATA WAS DROPPED.", file=sys.stderr)
        sys.exit(1)

    print("\nVERIFICATION SUCCESSFUL: All source document counts match destination document counts exactly (0 difference).")

    # PHASE 4: DROP ARCHIVE COLLECTIONS FROM SOURCE DATABASE
    print("\n--- PHASE 4: Dropping *_archive collections from trading_agent_clean ---")
    for src_col, tgt_col in archive_mapping:
        if src_col in source_db.list_collection_names():
            source_db.drop_collection(src_col)
            stats[src_col]["dropped_from_source"] = True
            print(f"Dropped [{source_db.name}.{src_col:<36}]")
        else:
            stats[src_col]["dropped_from_source"] = True
            print(f"Notice  [{source_db.name}.{src_col:<36}] was empty / not present")

    # PHASE 5: FINAL REPORT & AUDIT
    print("\n==========================================================")
    print("                   FINAL SUMMARY REPORT                   ")
    print("==========================================================")
    print(f"{'Source Collection':<36} | {'Target Collection':<28} | {'Src Count':<9} | {'Dest Count':<10} | {'Verified':<8} | {'Dropped?':<8}")
    print("-" * 115)
    for src_col, tgt_col in archive_mapping:
        s = stats[src_col]
        print(f"{src_col:<36} | {tgt_col:<28} | {s['source_count']:<9} | {s['dest_count']:<10} | {str(s['verified']):<8} | {str(s['dropped_from_source']):<8}")
    print("-" * 115)

    print("\n=== FINAL AUDIT OF trading_agent_clean ===")
    remaining_clean_cols = source_db.list_collection_names()
    remaining_clean_archives = [c for c in remaining_clean_cols if c.endswith('_archive')]
    print(f"Remaining *_archive collections in trading_agent_clean: {len(remaining_clean_archives)}")
    if remaining_clean_archives:
        print(f"WARNING: Unexpected archive collections remaining: {remaining_clean_archives}")
    else:
        print("CONFIRMED: trading_agent_clean contains only active live collections!")

    print("\n=== FINAL AUDIT OF trading_agent_archive ===")
    archive_db_cols = target_db.list_collection_names()
    print("Collections present in trading_agent_archive:")
    for col in sorted(archive_db_cols):
        cnt = target_db[col].count_documents({})
        snap_cnt = target_db[col].count_documents({"snapshot_id": snapshot_id})
        print(f" - {col:<32}: total_count={cnt:<6} | snapshot_count={snap_cnt}")

    print("\nMigration completed successfully!")

if __name__ == '__main__':
    migrate_archive_db()
