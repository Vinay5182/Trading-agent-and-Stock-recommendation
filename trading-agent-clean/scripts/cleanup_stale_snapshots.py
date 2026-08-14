import sys
import argparse
import asyncio
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, 'backend')
from config import settings


async def cleanup_stale_snapshots(cutoff_date_str: str = "2026-07-19T00:00:00Z", dry_run: bool = True):
    mongo_uri = getattr(settings, 'MONGO_URI', None) or getattr(settings, 'MONGODB_URI', 'mongodb://localhost:27017')
    db_name = getattr(settings, 'MONGO_DB_NAME', None) or getattr(settings, 'MONGODB_DB_NAME', 'trading_agent_clean')
    
    client = AsyncIOMotorClient(mongo_uri)
    db = client[db_name]
    coll = db['paper_market_snapshots']
    trades_coll = db['paper_trades']
    
    cutoff_dt = datetime.fromisoformat(cutoff_date_str.replace('Z', '+00:00'))
    
    print(f"=== STALE SNAPSHOT CLEANUP UTILITY ===")
    print(f"Database: {db_name}")
    print(f"Cutoff Date: {cutoff_dt.isoformat()}")
    print(f"Mode: {'DRY-RUN (No changes applied)' if dry_run else 'LIVE PURGE'}\n")
    
    total_docs = await coll.count_documents({})
    print(f"Total documents in paper_market_snapshots: {total_docs}")
    
    # Identify active trade IDs
    active_trades = await trades_coll.find({'status': {'$in': ['WAITING_FOR_ENTRY', 'ACTIVE']}}).to_list(length=1000)
    active_trade_ids = {str(t['_id']) for t in active_trades}
    active_setup_ids = {t.get('setup_id') for t in active_trades if t.get('setup_id')}
    
    print(f"Active trades in portfolio: {len(active_trade_ids)}")
    
    stale_count = 0
    stale_ids = []
    
    cursor = coll.find()
    async for doc in cursor:
        obs = doc.get('observed_at') or doc.get('created_at')
        if isinstance(obs, str):
            try:
                obs_dt = datetime.fromisoformat(obs.replace('Z', '+00:00'))
            except Exception:
                obs_dt = None
        elif isinstance(obs, datetime):
            obs_dt = obs if obs.tzinfo else obs.replace(tzinfo=timezone.utc)
        else:
            obs_dt = None
            
        if obs_dt and obs_dt < cutoff_dt:
            # Check if this snapshot belongs to an active open trade
            trade_id = str(doc.get('paper_trade_id') or '')
            setup_id = doc.get('setup_id')
            if trade_id not in active_trade_ids and setup_id not in active_setup_ids:
                stale_count += 1
                stale_ids.append(doc['_id'])
                
    print(f"Found {stale_count} stale snapshots before {cutoff_date_str} belonging to closed/archived campaigns.")
    
    if stale_count == 0:
        print("No stale snapshots to purge.")
        return
        
    if dry_run:
        print(f"\n[DRY-RUN COMPLETE] {stale_count} snapshots identified for purge. Run with --execute to perform deletion.")
    else:
        print(f"\n[EXECUTING PURGE] Deleting {stale_count} documents...")
        result = await coll.delete_many({'_id': {'$in': stale_ids}})
        print(f"Successfully deleted {result.deleted_count} stale snapshot documents.")


def main():
    parser = argparse.ArgumentParser(description="Cleanup stale historical paper_market_snapshots")
    parser.add_argument("--cutoff", type=str, default="2026-07-19T00:00:00Z", help="ISO cutoff date (default: 2026-07-19T00:00:00Z)")
    parser.add_argument("--execute", action="store_true", help="Execute live deletion (default: dry-run)")
    args = parser.parse_args()
    
    asyncio.run(cleanup_stale_snapshots(cutoff_date_str=args.cutoff, dry_run=not args.execute))


if __name__ == "__main__":
    main()
