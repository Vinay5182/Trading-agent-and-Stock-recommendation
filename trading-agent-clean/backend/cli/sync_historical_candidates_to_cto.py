import argparse
import asyncio
import os
import sys

# Ensure backend directory is in sys.path
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from database import connect_to_mongo, close_mongo_connection, get_database
from services.candidate_trade_outcomes_service import (
    create_candidate_trade_outcome_doc,
    persist_candidate_trade_outcome_records,
)


async def sync_historical_candidates(dry_run: bool = False) -> dict:
    await connect_to_mongo()
    db = get_database()

    hsc_collection = db["historical_scored_candidates"]
    candidates = await hsc_collection.find({}).to_list(length=None)

    total_candidates = len(candidates)
    print(f"Loaded {total_candidates} historical candidate records from historical_scored_candidates.")

    if total_candidates == 0:
        await close_mongo_connection()
        return {"processed": 0, "inserted": 0, "updated": 0, "dry_run": dry_run}

    outcome_docs = []
    for c in candidates:
        trade_plan = {
            "entry_price": c.get("normalized_score_inputs", {}).get("current_price", 0.0),
            "stop_loss": c.get("normalized_score_inputs", {}).get("current_price", 0.0) * 0.98,
            "target_1": c.get("normalized_score_inputs", {}).get("current_price", 0.0) * 1.05,
            "target_2": c.get("normalized_score_inputs", {}).get("current_price", 0.0) * 1.10,
            "target_3": c.get("normalized_score_inputs", {}).get("current_price", 0.0) * 1.15,
            "risk_reward_ratio": 2.5,
            "position_size": 100,
        }
        selection_status = {
            "selected_for_trade": c.get("selected_for_tv", False) or c.get("swing_candidate", False) or c.get("momentum_candidate", False),
            "selection_reason": "QUALIFIED_BY_SCANNER" if (c.get("swing_candidate") or c.get("momentum_candidate")) else "REJECTED_LOW_SCORE",
            "rejection_reason": None if (c.get("swing_candidate") or c.get("momentum_candidate")) else "SCORE_BELOW_THRESHOLD",
        }
        doc = create_candidate_trade_outcome_doc(
            symbol=c.get("symbol", ""),
            scan_date=c.get("trade_date", ""),
            candidate_features=c,
            trade_plan=trade_plan,
            selection_status=selection_status,
        )
        outcome_docs.append(doc)

    if dry_run:
        print(f"[DRY-RUN] Would process and persist {len(outcome_docs)} CTO documents.")
        await close_mongo_connection()
        return {"processed": total_candidates, "inserted": 0, "updated": 0, "dry_run": True}

    res = await persist_candidate_trade_outcome_records(db, outcome_docs)
    inserted = res.get("inserted_count", 0)
    updated = res.get("updated_count", 0)

    print(f"Persisted to candidate_trade_outcomes: Inserted={inserted}, Updated={updated}")
    if "error" in res:
        print(f"Errors encountered during persistence: {res['error']}")

    await close_mongo_connection()
    return {"processed": total_candidates, "inserted": inserted, "updated": updated, "dry_run": False}


def main():
    parser = argparse.ArgumentParser(description="Sync historical_scored_candidates to candidate_trade_outcomes.")
    parser.add_argument("--dry-run", action="store_true", help="Perform dry run without writing to MongoDB.")
    args = parser.parse_args()

    result = asyncio.run(sync_historical_candidates(dry_run=args.dry_run))
    print("Sync Result:", result)


if __name__ == "__main__":
    main()
