import asyncio
import json
import os
import sys
from datetime import datetime, timezone
import dateutil.parser

# Ensure backend path is loaded
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from database import connect_to_mongo, get_database
from routes.paper import evaluate_paper_trade_chronologically, is_terminal_trade, parse_datetime_value

def parse_dt(val):
    if not val:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)
    try:
        dt = dateutil.parser.isoparse(str(val))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def norm_sym(s):
    if not s:
        return ""
    s = str(s).upper().strip()
    if s.startswith("NSE:"):
        s = s[4:]
    return s

async def backup_mongo_collections(db):
    timestamp_str = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "mongo_backups", f"paper_trades_repair_backup_{timestamp_str}"))
    os.makedirs(backup_dir, exist_ok=True)
    
    collections_to_backup = ["paper_trades", "trade_journal", "daily_trade_dataset", "scored_candidates"]
    backup_stats = {}
    
    print(f"=== STEP 1: CREATING FULL MONGODB BACKUP ===")
    print(f"Backup Directory: {backup_dir}")
    
    for coll_name in collections_to_backup:
        docs = await db[coll_name].find({}).to_list(length=50000)
        file_path = os.path.join(backup_dir, f"{coll_name}.json")
        
        # Serialize ObjectId and datetime objects
        serialized_docs = json.loads(json.dumps(docs, default=str))
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(serialized_docs, f, indent=2)
            
        count = len(docs)
        file_size = os.path.getsize(file_path)
        backup_stats[coll_name] = {"count": count, "size_bytes": file_size}
        print(f"  Backup Collection '{coll_name}': {count} documents written ({file_size} bytes)")
        
        if coll_name == "paper_trades" and count == 0:
            raise RuntimeError("CRITICAL ERROR: Backup failed! paper_trades collection returned 0 documents.")
            
    print("[OK] Backup integrity verified successfully.\n")
    return backup_dir, backup_stats

async def fetch_market_snapshots_for_trade(db, trade):
    sym = norm_sym(trade.get("canonical_symbol") or trade.get("symbol") or trade.get("tradingview_symbol"))
    trade_id = str(trade.get("_id"))
    setup_id = trade.get("canonical_setup_id") or trade.get("setup_id")
    
    pms_query = {"$or": [{"symbol": sym}, {"canonical_symbol": sym}]}
    if trade_id:
        pms_query["$or"].append({"paper_trade_id": trade_id})
    if setup_id:
        pms_query["$or"].append({"setup_id": setup_id})
        
    snapshots = await db.paper_market_snapshots.find(pms_query).sort("observed_at", 1).to_list(length=10000)
    return snapshots

async def run_production_repair(dry_run: bool = False):
    await connect_to_mongo()
    db = get_database()
    
    # 1. Backup
    backup_dir, backup_stats = await backup_mongo_collections(db)
    
    # 2. Load all paper trades
    trades = await db.paper_trades.find({}).to_list(length=10000)
    print(f"=== STEP 2: QUERYING PAPER TRADES ===")
    print(f"Total paper trades loaded from DB: {len(trades)}\n")
    
    # Target status filter
    target_statuses = {"ACTIVE", "SL_HIT", "STOPPED", "T1_PARTIAL", "T2_PARTIAL", "COMPLETED", "WAITING_FOR_ENTRY", "WAITING_FOR_CAPITAL"}
    filtered_trades = [t for t in trades if str(t.get("status") or "").upper() in target_statuses or str(t.get("outcome_status") or "").upper() in target_statuses]
    print(f"Filtered trades for evaluation: {len(filtered_trades)}\n")
    
    repaired_records = []
    unchanged_count = 0
    ambiguous_count = 0
    
    print(f"=== STEP 3 & 4: RECOMPUTING TRADES FROM FIRST PRINCIPLES ===")
    
    for orig_trade in filtered_trades:
        trade_id = str(orig_trade.get("_id"))
        symbol = norm_sym(orig_trade.get("canonical_symbol") or orig_trade.get("symbol") or orig_trade.get("tradingview_symbol"))
        old_status = str(orig_trade.get("status") or "").upper()
        old_pnl = float(orig_trade.get("paper_pnl") or orig_trade.get("realized_pnl") or 0.0)
        old_qty = int(orig_trade.get("quantity") or orig_trade.get("quantity_remaining") or 0)
        
        # Prepare clean initial plan state (resetting execution fields for fresh replay)
        initial_plan = orig_trade.copy()
        initial_plan["status"] = "WAITING_FOR_ENTRY"
        initial_plan["outcome_status"] = "WAITING_FOR_ENTRY"
        initial_plan["state"] = "WAITING_FOR_ENTRY"
        initial_plan["entry_triggered"] = False
        initial_plan.pop("entry_triggered_at", None)
        initial_plan.pop("entry_time", None)
        initial_plan.pop("closed_time", None)
        initial_plan.pop("exit_price", None)
        initial_plan.pop("exit_reason", None)
        initial_plan.pop("sl_hit_time", None)
        initial_plan.pop("t1_hit_time", None)
        initial_plan.pop("t2_hit_time", None)
        initial_plan.pop("t3_hit_time", None)
        
        # Replay candles chronologically using CURRENT production engine
        recomputed_update = await evaluate_paper_trade_chronologically(
            db,
            initial_plan,
            market_row=None,
            current_balance=1000000.0,
            available_margin=1000000.0,
            open_margin=0.0,
            combined_open_risk=0.0,
        )
        
        # Determine final state
        new_status = str(recomputed_update.get("status") or initial_plan.get("status")).upper()
        new_pnl = float(recomputed_update.get("paper_pnl", 0.0))
        new_qty = int(recomputed_update.get("quantity_remaining", recomputed_update.get("quantity", initial_plan.get("quantity", 0))))
        
        # Check if trade never entered (terminal non-active or still waiting)
        is_never_entered = new_status in {"EXPIRED", "ENTRY_INVALIDATED", "ENTRY_MISSED_GAP_UP", "ENTRY_MISSED_GAP_DOWN", "WAITING_FOR_ENTRY", "WAITING_FOR_CAPITAL"}
        
        # Construct update payload
        final_doc_fields = {
            "status": new_status,
            "outcome_status": recomputed_update.get("outcome_status", new_status),
            "state": recomputed_update.get("state", new_status),
            "last_checked_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }
        
        unset_fields = {}
        
        if is_never_entered:
            final_doc_fields["entry_triggered"] = False
            final_doc_fields["paper_pnl"] = 0.0
            final_doc_fields["paper_pnl_percent"] = 0.0
            final_doc_fields["realized_pnl"] = 0.0
            final_doc_fields["total_trade_pnl"] = 0.0
            final_doc_fields["initial_margin_reserved"] = 0.0
            final_doc_fields["margin_remaining"] = 0.0
            final_doc_fields["open_sl_risk"] = 0.0
            final_doc_fields["initial_sl_risk"] = 0.0
            if new_status != "WAITING_FOR_ENTRY" and new_status != "WAITING_FOR_CAPITAL":
                final_doc_fields["quantity_remaining"] = 0
                
            unset_fields.update({
                "entry_triggered_at": "",
                "entry_time": "",
                "closed_time": "",
                "sl_hit_time": "",
                "t1_hit_time": "",
                "t2_hit_time": "",
                "t3_hit_time": "",
                "exit_price": "",
                "partial_exit_1": "",
                "partial_exit_2": "",
                "partial_exit_3": "",
                "stop_exit": "",
            })
            if recomputed_update.get("exit_reason"):
                final_doc_fields["exit_reason"] = recomputed_update.get("exit_reason")
            if recomputed_update.get("capital_rejection_reason"):
                final_doc_fields["capital_rejection_reason"] = recomputed_update.get("capital_rejection_reason")
            if recomputed_update.get("activation_blocked_reason"):
                final_doc_fields["activation_blocked_reason"] = recomputed_update.get("activation_blocked_reason")
        else:
            # Entered trade - update computed fields
            for key in ["entry_triggered", "entry_triggered_at", "entry_time", "closed_time", "exit_price", "exit_reason",
                        "sl_hit_time", "t1_hit_time", "t2_hit_time", "t3_hit_time", "paper_pnl", "paper_pnl_percent",
                        "realized_pnl", "total_trade_pnl", "quantity", "quantity_remaining", "initial_margin_reserved",
                        "margin_remaining", "open_sl_risk", "initial_sl_risk", "partial_exit_1", "partial_exit_2", "partial_exit_3"]:
                if key in recomputed_update:
                    final_doc_fields[key] = recomputed_update[key]

        # Determine if document state actually changed
        modified_fields = []
        for k, v in final_doc_fields.items():
            if orig_trade.get(k) != v:
                modified_fields.append(k)
        for k in unset_fields.keys():
            if k in orig_trade:
                modified_fields.append(f"-{k}")
                
        if modified_fields or old_status != new_status:
            repaired_records.append({
                "trade_id": trade_id,
                "symbol": symbol,
                "old_status": old_status,
                "new_status": new_status,
                "old_pnl": old_pnl,
                "new_pnl": new_pnl,
                "old_quantity": old_qty,
                "new_quantity": new_qty,
                "modified_fields": modified_fields,
                "reason": f"State Machine Recomputation ({old_status} -> {new_status})",
                "final_doc_fields": final_doc_fields,
                "unset_fields": unset_fields,
            })
        else:
            unchanged_count += 1
            
    print(f"Recomputation Complete:")
    print(f"  Repaired / Updated Trades: {len(repaired_records)}")
    print(f"  Unchanged Trades: {unchanged_count}")
    print(f"  Ambiguous Trades: {ambiguous_count}\n")
    
    # 5. Output Log of Changes
    print("=== STEP 5: DETAILED LOG OF REPAIRED TRADES ===")
    for rec in repaired_records:
        print(f"Trade ID:   {rec['trade_id']}")
        print(f"Symbol:     {rec['symbol']}")
        print(f"Status:     {rec['old_status']} -> {rec['new_status']}")
        print(f"PnL:        {rec['old_pnl']:.2f} -> {rec['new_pnl']:.2f}")
        print(f"Quantity:   {rec['old_quantity']} -> {rec['new_quantity']}")
        print(f"Fields:     {', '.join(rec['modified_fields'])}")
        print(f"Reason:     {rec['reason']}")
        print("-" * 60)
        
    # 6. Special Cases Audit (TIPSMUSIC, SANSERA, SENCO, COLPAL, ZEEL, MARICO)
    print("\n=== STEP 6: SPECIAL CASES AUDIT ===")
    special_symbols = ["TIPSMUSIC", "SANSERA", "SENCO", "COLPAL", "ZEEL", "MARICO"]
    for sym in special_symbols:
        matched = [r for r in repaired_records if r['symbol'] == sym]
        if not matched:
            # Check if it was unchanged
            in_orig = [t for t in trades if norm_sym(t.get('canonical_symbol') or t.get('symbol')) == sym]
            st = in_orig[0].get('status') if in_orig else 'NOT_FOUND'
            print(f"  Symbol {sym:<10}: UNCHANGED (Current Status: {st})")
        else:
            for m in matched:
                print(f"  Symbol {m['symbol']:<10}: {m['old_status']} -> {m['new_status']} (PnL: {m['old_pnl']:.2f} -> {m['new_pnl']:.2f})")
                
    # Apply DB Updates if not dry run
    if not dry_run:
        print("\n=== APPLYING ATOMIC MONGODB UPDATES ===")
        updated_in_db = 0
        for rec in repaired_records:
            update_op = {"$set": rec["final_doc_fields"]}
            if rec["unset_fields"]:
                update_op["$unset"] = rec["unset_fields"]
            
            orig_doc = next((t for t in trades if str(t.get("_id")) == rec["trade_id"]), None)
            doc_id = orig_doc["_id"] if orig_doc else rec["trade_id"]
            res = await db.paper_trades.update_one({"_id": doc_id}, update_op)
            
            # Clean up stale trade_journal entries if reverted to non-completed
            if rec["new_status"] in {"EXPIRED", "ENTRY_INVALIDATED", "ENTRY_MISSED_GAP_UP", "ENTRY_MISSED_GAP_DOWN", "WAITING_FOR_ENTRY"}:
                await db.trade_journal.delete_many({"paper_trade_id": rec["trade_id"]})
                
            updated_in_db += 1
        print(f"[OK] Successfully applied {updated_in_db} atomic document updates in MongoDB.\n")
    else:
        print("\n[DRY RUN MODE] No MongoDB updates applied.\n")
        
    # 7. Recompute Global Statistics
    print("=== STEP 7 & 8: RECOMPUTE GLOBAL STATS & INTEGRITY VALIDATION ===")
    all_trades_current = await db.paper_trades.find({}).to_list(length=10000)
    
    st_counts = {}
    total_portfolio_pnl = 0.0
    total_reserved_margin = 0.0
    integrity_failures = []
    
    for t in all_trades_current:
        st = str(t.get("status") or "").upper()
        st_counts[st] = st_counts.get(st, 0) + 1
        pnl = float(t.get("paper_pnl") or t.get("realized_pnl") or 0.0)
        total_portfolio_pnl += pnl
        res_m = float(t.get("initial_margin_reserved") or t.get("margin_remaining") or 0.0)
        total_reserved_margin += res_m
        
        # Integrity rules
        qty_rem = int(t.get("quantity_remaining") or 0)
        entry_trig = bool(t.get("entry_triggered"))
        entry_t = t.get("entry_triggered_at") or t.get("entry_time")
        
        if st == "ACTIVE" and qty_rem <= 0:
            integrity_failures.append(f"ACTIVE trade {t.get('_id')} ({t.get('symbol')}) has quantity_remaining <= 0")
        if st in {"EXPIRED", "ENTRY_INVALIDATED", "ENTRY_MISSED_GAP_UP"} and entry_trig:
            integrity_failures.append(f"{st} trade {t.get('_id')} ({t.get('symbol')}) has entry_triggered = True")
        if st in {"EXPIRED", "ENTRY_INVALIDATED"} and pnl != 0.0:
            integrity_failures.append(f"{st} trade {t.get('_id')} ({t.get('symbol')}) has non-zero PnL: {pnl}")
        if st in {"WAITING_FOR_ENTRY", "WAITING_FOR_CAPITAL"} and entry_t:
            integrity_failures.append(f"{st} trade {t.get('_id')} ({t.get('symbol')}) has entry timestamp: {entry_t}")
        if st in {"SL_HIT", "STOPPED", "COMPLETED"} and not entry_trig and not entry_t:
            integrity_failures.append(f"{st} trade {t.get('_id')} ({t.get('symbol')}) lacks entry confirmation")

    print(f"Current Global Status Distribution:")
    for st, count in sorted(st_counts.items()):
        print(f"  {st:<22}: {count}")
    print(f"\nGlobal Recomputed Metrics:")
    print(f"  Total Portfolio Realized PnL: Rupee {total_portfolio_pnl:,.2f}")
    print(f"  Total Reserved Margin:       Rupee {total_reserved_margin:,.2f}")
    print(f"  Available Capital:           Rupee {1000000.0 - total_reserved_margin:,.2f}")
    print(f"  Integrity Validation Errors:  {len(integrity_failures)}")
    
    if integrity_failures:
        print("  CRITICAL INTEGRITY ERRORS FOUND:")
        for err in integrity_failures:
            print(f"    - {err}")
    else:
        print("[OK] Database Integrity Verification 100% CLEAN.")
        
    return repaired_records, st_counts, integrity_failures

if __name__ == "__main__":
    asyncio.run(run_production_repair(dry_run=False))
