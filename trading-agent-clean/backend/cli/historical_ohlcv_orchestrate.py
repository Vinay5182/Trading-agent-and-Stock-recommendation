from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings
from services.historical_backfill_orchestrator import (
    build_historical_multi_symbol_backfill_plan,
    verify_historical_multi_symbol_backfill_plan,
)
from services.historical_ohlcv_store import (
    HISTORICAL_OHLCV_COLLECTION,
    HistoricalPersistenceError,
)
from services.migration_safety import safe_json_value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview, verify, or view multi-symbol historical backfill plans.")
    parser.add_argument("--mode", choices=("preview", "verify-plan", "status", "apply"), default="preview")
    parser.add_argument("--database-name", default=settings.DATABASE_NAME)
    parser.add_argument("--provider", default="yfinance")
    parser.add_argument("--exchange", default="NSE")
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--symbol-file", help="Path to text file containing one symbol per line.")
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--max-rows-per-symbol", type=int, default=30)
    parser.add_argument("--max-total-rows", type=int, default=90)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--plan-output")
    parser.add_argument("--plan-file")
    parser.add_argument("--manifest-hash")
    parser.add_argument("--expected-database")
    parser.add_argument("--expected-collection", default=HISTORICAL_OHLCV_COLLECTION)
    parser.add_argument("--apply", action="store_true", help="Triggers apply check.")
    return parser.parse_args(argv)


def _write_json(path: str | None, payload: Mapping[str, Any]) -> None:
    if not path:
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(safe_json_value(payload), indent=2, sort_keys=True), encoding="utf-8")


def _load_plan(path: str | None) -> dict[str, Any]:
    if not path:
        raise HistoricalPersistenceError("HISTORICAL_PLAN_FILE_REQUIRED", "--plan-file is required.")
    plan_path = Path(path)
    if not plan_path.exists():
        raise HistoricalPersistenceError("HISTORICAL_PLAN_FILE_REQUIRED", "Plan file does not exist.", {"path": str(plan_path)})
    try:
        return json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HistoricalPersistenceError("HISTORICAL_PLAN_FILE_INVALID", "Plan file must be valid JSON.") from exc


def _collection_for(db: Any) -> Any:
    if hasattr(db, "__getitem__"):
        try:
            return db[HISTORICAL_OHLCV_COLLECTION]
        except Exception:
            pass
    return getattr(db, HISTORICAL_OHLCV_COLLECTION)


def _database_name(db: Any, fallback: str) -> str:
    return str(getattr(db, "name", fallback) or fallback)


async def _open_live_db(database_name: str) -> tuple[Any, Any]:
    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(settings.MONGO_URI)
    return client, client[database_name]


def _read_symbols_file(path: str) -> list[str]:
    symbol_path = Path(path)
    if not symbol_path.exists():
        raise HistoricalPersistenceError("HISTORICAL_SYMBOL_FILE_INVALID", f"Symbols file '{path}' does not exist.")
    lines = symbol_path.read_text(encoding="utf-8").splitlines()
    symbols = []
    for line in lines:
        cleaned = line.strip()
        if cleaned and not cleaned.startswith("#"):
            symbols.append(cleaned)
    return symbols


async def run(
    args: argparse.Namespace,
    *,
    db_override: Any | None = None,
    fetcher: Any = None,
) -> dict[str, Any]:
    # Phase 5B3A Apply Mode Protection: Fail closed
    if args.mode == "apply" or args.apply:
        raise HistoricalPersistenceError(
            "MULTI_SYMBOL_APPLY_NOT_ENABLED",
            "Multi-symbol backfill execution is disabled/not supported in Phase 5B3A."
        )

    if args.mode == "verify-plan":
        plan = _load_plan(args.plan_file)
        verify_historical_multi_symbol_backfill_plan(
            plan,
            expected_database=args.expected_database or plan.get("target_database"),
            expected_collection=args.expected_collection or plan.get("target_collection"),
        )
        return {
            "ok": True,
            "mode": "verify-plan",
            "orchestration_plan_id": plan.get("orchestration_plan_id"),
            "aggregate_manifest_hash": plan.get("aggregate_manifest_hash"),
            "target_database": plan.get("target_database"),
            "target_collection": plan.get("target_collection"),
            "planned_inserts": plan.get("counts", {}).get("planned_inserts", 0),
            "zero_writes_performed": True,
        }

    if args.mode == "status":
        plan = _load_plan(args.plan_file)
        return {
            "orchestration_plan_id": plan.get("orchestration_plan_id"),
            "orchestration_contract_version": plan.get("orchestration_contract_version"),
            "state": plan.get("state"),
            "counts": plan.get("counts"),
            "aggregate_manifest_hash": plan.get("aggregate_manifest_hash"),
            "target_database": plan.get("target_database"),
            "target_collection": plan.get("target_collection"),
            "created_at": plan.get("created_at"),
            "expires_at": plan.get("expires_at"),
            "symbols_status": {
                sym: sym_data.get("status") for sym, sym_data in plan.get("symbols", {}).items()
            }
        }

    # preview mode
    # Resolve symbol list
    symbols = args.symbols or []
    if args.symbol_file:
        symbols.extend(_read_symbols_file(args.symbol_file))

    if not symbols:
        raise HistoricalPersistenceError(
            "HISTORICAL_BACKFILL_REQUEST_INVALID",
            "Symbols are required. Provide via --symbols or --symbol-file."
        )

    client = None
    db = db_override
    if db is None:
        client, db = await _open_live_db(args.database_name)
    try:
        collection = _collection_for(db)
        database_name = _database_name(db, args.database_name)

        plan = await build_historical_multi_symbol_backfill_plan(
            collection,
            database_name=database_name,
            provider=args.provider,
            exchange=args.exchange,
            symbols=symbols,
            timeframe=args.timeframe,
            start=args.start,
            end=args.end,
            max_rows_per_symbol=args.max_rows_per_symbol,
            max_total_candidate_rows=args.max_total_rows,
            batch_size=args.batch_size,
            max_concurrency=1, # Fixed to 1 for initial pilot
            include_incomplete=False,
            fetcher=fetcher,
        )
        _write_json(args.plan_output, plan)
        return plan
    finally:
        if client is not None:
            client.close()


def main() -> None:
    args = parse_args()
    try:
        res = asyncio.run(run(args))
        print(json.dumps(res, indent=2, sort_keys=True))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
