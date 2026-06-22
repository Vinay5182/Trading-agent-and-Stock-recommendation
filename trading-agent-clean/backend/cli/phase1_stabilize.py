from __future__ import annotations

import argparse
import asyncio
import json

from database import close_mongo_connection, connect_to_mongo, get_database
from services.paper_migration import migrate_paper_trade_setup_ids
from services.trade_journal import sync_completed_trades_to_journal


EXPECTED_SETUP_INDEX = {
    "paper_only": True,
    "setup_id": {"$exists": True, "$type": "string", "$gt": ""},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Paper-trading stabilization migration and verification")
    parser.add_argument("--apply", action="store_true", help="Apply setup_id migration and journal backfill")
    parser.add_argument("--limit", type=int, default=None, help="Optional paper_trades scan limit")
    parser.add_argument("--journal-limit", type=int, default=5000, help="Completed paper_trades journal backfill limit")
    return parser.parse_args()


async def collection_names(db) -> set[str]:
    return set(await db.list_collection_names())


async def index_names_and_defs(collection) -> dict:
    indexes = {}
    async for index in collection.list_indexes():
        indexes[index["name"]] = dict(index)
    return indexes


async def validate_indexes(db) -> dict:
    names = await collection_names(db)
    paper_indexes = await index_names_and_defs(db.paper_trades) if "paper_trades" in names else {}
    journal_indexes = await index_names_and_defs(db.trade_journal) if "trade_journal" in names else {}
    setup_index = paper_indexes.get("paper_trades_setup_id_unique_v1")
    journal_index = journal_indexes.get("trade_journal_paper_trade_id_unique")
    return {
        "paper_trades_exists": "paper_trades" in names,
        "trade_journal_exists": "trade_journal" in names,
        "setup_index_exists": setup_index is not None,
        "setup_index_unique": bool(setup_index and setup_index.get("unique")),
        "setup_index_partial_filter": setup_index.get("partialFilterExpression") if setup_index else None,
        "setup_index_filter_valid": bool(
            setup_index
            and setup_index.get("unique")
            and setup_index.get("partialFilterExpression") == EXPECTED_SETUP_INDEX
        ),
        "trade_journal_unique_index_exists": journal_index is not None,
        "trade_journal_unique_index_unique": bool(journal_index and journal_index.get("unique")),
        "paper_trade_index_names": sorted(paper_indexes),
        "trade_journal_index_names": sorted(journal_indexes),
    }


async def run() -> dict:
    args = parse_args()
    await connect_to_mongo()
    try:
        db = get_database()
        setup_result = await migrate_paper_trade_setup_ids(db, apply=args.apply, limit=args.limit)
        journal_result = None
        if args.apply:
            journal_result = await sync_completed_trades_to_journal(db, limit=args.journal_limit)
        validation = await validate_indexes(db)
        return {
            "apply": args.apply,
            "setup_id_migration": setup_result,
            "journal_backfill": journal_result,
            "journal_backfill_skipped": not args.apply,
            "index_validation": validation,
        }
    finally:
        await close_mongo_connection()


def main() -> None:
    print(json.dumps(asyncio.run(run()), indent=2, default=str))


if __name__ == "__main__":
    main()
