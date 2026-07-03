from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.historical_ohlcv import HISTORICAL_OHLCV_MAX_ROWS, HistoricalOHLCVError
from config import settings
from services.historical_ohlcv_store import (
    HISTORICAL_OHLCV_APPLY_ACK,
    HISTORICAL_OHLCV_APPLY_APPROVAL,
    HISTORICAL_OHLCV_COLLECTION,
    HistoricalPersistenceError,
    apply_historical_backfill_plan,
    build_historical_backfill_plan,
    historical_persistence_readiness,
    validate_historical_plan_for_apply,
    verify_historical_plan_hash,
)
from services.migration_safety import safe_json_value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview, verify, or apply historical OHLCV insert-only backfill plans.")
    parser.add_argument("--mode", choices=("preview", "verify-plan", "apply"), default="preview")
    parser.add_argument("--database-name", default=settings.DATABASE_NAME)
    parser.add_argument("--provider")
    parser.add_argument("--exchange")
    parser.add_argument("--symbol")
    parser.add_argument("--canonical-symbol")
    parser.add_argument("--timeframe")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--max-rows", type=int, default=200)
    parser.add_argument("--include-incomplete", action="store_true")
    parser.add_argument("--plan-output")
    parser.add_argument("--plan-file")
    parser.add_argument("--result-output")
    parser.add_argument("--manifest-hash")
    parser.add_argument("--expected-database")
    parser.add_argument("--expected-collection", default=HISTORICAL_OHLCV_COLLECTION)
    parser.add_argument("--apply", action="store_true", help="Required with --mode apply.")
    parser.add_argument("--approval-value")
    parser.add_argument("--operator-acknowledgement")
    parser.add_argument("--approved", action="store_true")
    return parser.parse_args(argv)


def _write_json(path: str | None, payload: MappingOrDict) -> None:
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


def _validate_preview_args(args: argparse.Namespace) -> None:
    missing = [
        name
        for name in ("provider", "exchange", "timeframe", "start", "end")
        if not getattr(args, name, None)
    ]
    symbol = args.canonical_symbol or args.symbol
    if not symbol:
        missing.append("symbol")
    if args.max_rows < 1 or args.max_rows > HISTORICAL_OHLCV_MAX_ROWS:
        missing.append("max_rows")
    if missing:
        raise HistoricalPersistenceError(
            "HISTORICAL_BACKFILL_REQUEST_INVALID",
            "Preview requires explicit provider, exchange, symbol, timeframe, start, end, and max_rows.",
            {"missing": sorted(set(missing))},
        )


async def _open_live_db(database_name: str) -> tuple[Any, Any]:
    from motor.motor_asyncio import AsyncIOMotorClient

    client = AsyncIOMotorClient(settings.MONGO_URI)
    return client, client[database_name]


async def run(
    args: argparse.Namespace,
    *,
    db_override: Any | None = None,
    acquisition_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if args.mode == "verify-plan":
        plan = _load_plan(args.plan_file)
        manifest_hash = verify_historical_plan_hash(plan)
        if args.manifest_hash:
            validate_historical_plan_for_apply(
                plan,
                expected_manifest_hash=args.manifest_hash,
                expected_database=args.expected_database or plan.get("target_database"),
                expected_collection=args.expected_collection,
            )
        return {
            "ok": True,
            "mode": "verify-plan",
            "manifest_hash": manifest_hash,
            "target_database": plan.get("target_database"),
            "target_collection": plan.get("target_collection"),
            "planned_inserts": (plan.get("counts") or {}).get("planned_inserts", 0),
            "zero_writes_performed": True,
        }

    if args.mode == "preview":
        _validate_preview_args(args)

    if args.mode == "apply" and not args.apply:
        raise HistoricalPersistenceError("HISTORICAL_APPLY_FLAG_REQUIRED", "--apply is required with --mode apply.")

    client = None
    db = db_override
    if db is None:
        client, db = await _open_live_db(args.database_name)
    try:
        collection = _collection_for(db)
        database_name = _database_name(db, args.database_name)
        if args.mode == "preview":
            plan = await build_historical_backfill_plan(
                collection,
                database_name=database_name,
                provider=args.provider,
                exchange=args.exchange,
                canonical_symbol=args.canonical_symbol or args.symbol,
                timeframe=args.timeframe,
                start=args.start,
                end=args.end,
                max_rows=args.max_rows,
                include_incomplete=args.include_incomplete,
                acquisition_result=acquisition_result,
            )
            _write_json(args.plan_output, plan)
            return plan
        if args.mode == "apply":
            plan = _load_plan(args.plan_file)
            expected_database = args.expected_database or database_name
            approval = {
                "approved": args.approved,
                "approval_value": args.approval_value,
                "operator_acknowledgement": args.operator_acknowledgement,
                "manifest_hash": args.manifest_hash,
                "database_name": expected_database,
                "collection": args.expected_collection,
            }
            result = await apply_historical_backfill_plan(
                collection,
                plan,
                approval=approval,
                database_name=expected_database,
            )
            _write_json(args.result_output, result)
            return result
        return await historical_persistence_readiness(collection, database_name=database_name)
    finally:
        if client is not None:
            client.close()


MappingOrDict = dict[str, Any]


def console_summary(payload: dict[str, Any]) -> str:
    if payload.get("apply") is True:
        return "\n".join(
            [
                "Historical OHLCV apply",
                f"ok={payload.get('ok')}",
                f"planned_inserts={payload.get('planned_inserts')}",
                f"inserted_count={payload.get('inserted_count')}",
                f"idempotent_noop_count={payload.get('idempotent_noop_count')}",
                f"conflict_count={payload.get('conflict_count')}",
                f"manifest_hash={payload.get('manifest_hash_used')}",
            ]
        )
    if payload.get("mode") == "verify-plan":
        return "\n".join(
            [
                "Historical OHLCV plan verification",
                f"ok={payload.get('ok')}",
                f"planned_inserts={payload.get('planned_inserts')}",
                f"manifest_hash={payload.get('manifest_hash')}",
                "zero_writes_performed=True",
            ]
        )
    counts = payload.get("counts") or {}
    return "\n".join(
        [
            "Historical OHLCV backfill preview",
            f"database={payload.get('target_database')}",
            f"planned_inserts={counts.get('planned_inserts')}",
            f"identical_noops={counts.get('identical_noops')}",
            f"conflicts={counts.get('conflicts')}",
            f"excluded={counts.get('excluded')}",
            f"manifest_hash={payload.get('manifest_hash')}",
            "zero_writes_performed=True",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        payload = asyncio.run(run(args))
        print(console_summary(payload))
        if args.mode == "apply" and not payload.get("ok"):
            return 2
        return 0
    except HistoricalOHLCVError as exc:
        print(f"HISTORICAL_BACKFILL_REFUSED: {exc.code}: {exc.message}", file=sys.stderr)
        return 3
    except HistoricalPersistenceError as exc:
        print(f"HISTORICAL_BACKFILL_REFUSED: {exc.code}: {exc.message}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
