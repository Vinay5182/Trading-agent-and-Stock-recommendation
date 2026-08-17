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
    evaluate_and_update_candidate_trade_outcomes,
)


async def run_evaluation(dry_run: bool = False, symbol: str = None, limit: int = 0) -> dict:
    await connect_to_mongo()
    db = get_database()

    query = {}
    if symbol:
        clean_sym = symbol.strip().upper().replace("NSE:", "")
        query["identity.canonical_symbol"] = clean_sym

    print(f"Starting outcome evaluation on candidate_trade_outcomes (dry_run={dry_run}, query={query})...")
    result = await evaluate_and_update_candidate_trade_outcomes(db, query=query, dry_run=dry_run)

    print("\n=== EVALUATION RESULTS ===")
    print(f"Total Candidates:   {result['total_candidates']}")
    print(f"Evaluated Count:    {result['evaluated_count']}")
    print(f"Updated Count:      {result['updated_count']}")
    print(f"Skipped Count:      {result['skipped_count']}")
    print(f"Error Count:        {result['error_count']}")
    print("Outcome Counts:")
    for outcome, cnt in result.get("outcome_counts", {}).items():
        print(f"  {outcome:15s}: {cnt}")

    await close_mongo_connection()
    return result


def main():
    parser = argparse.ArgumentParser(description="Evaluate outcomes for candidate_trade_outcomes against historical OHLCV candles.")
    parser.add_argument("--dry-run", action="store_true", help="Perform evaluation preview without modifying MongoDB.")
    parser.add_argument("--symbol", type=str, default=None, help="Filter candidate evaluation by canonical symbol.")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of candidates evaluated.")
    args = parser.parse_args()

    asyncio.run(run_evaluation(dry_run=args.dry_run, symbol=args.symbol, limit=args.limit))


if __name__ == "__main__":
    main()
