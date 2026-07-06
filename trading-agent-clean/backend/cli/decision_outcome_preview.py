from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings
from services.decision_outcome_dataset import build_decision_outcome_preview_from_db
from services.migration_safety import safe_json_value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview decision-outcome rows without writes, training, or TradingView calls.")
    parser.add_argument("--mode", choices=("preview",), default="preview")
    parser.add_argument("--database-name", default=settings.DATABASE_NAME)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--history-limit", type=int)
    parser.add_argument("--examples-per-bucket", type=int, default=5)
    parser.add_argument("--strategy-type", choices=("momentum", "swing", "other"))
    parser.add_argument("--symbol")
    parser.add_argument("--timeframe")
    parser.add_argument("--result-output")
    return parser.parse_args(argv)


def _write_json(path: str | None, payload: Mapping[str, Any]) -> None:
    if not path:
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(safe_json_value(payload), indent=2, sort_keys=True), encoding="utf-8")


async def _open_live_db(database_name: str) -> tuple[Any, Any]:
    from motor.motor_asyncio import AsyncIOMotorClient

    client = AsyncIOMotorClient(settings.MONGO_URI, serverSelectionTimeoutMS=3000)
    return client, client[database_name]


async def run(args: argparse.Namespace, *, db_override: Any | None = None) -> dict[str, Any]:
    client = None
    db = db_override
    if db is None:
        client, db = await _open_live_db(args.database_name)
    try:
        result = await build_decision_outcome_preview_from_db(
            db,
            limit=args.limit,
            history_limit=args.history_limit,
            examples_per_bucket=args.examples_per_bucket,
            strategy_type=args.strategy_type,
            symbol=args.symbol,
            timeframe=args.timeframe,
        )
        result["database_name"] = args.database_name
        _write_json(args.result_output, result)
        return result
    finally:
        if client is not None:
            client.close()


def console_summary(payload: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "Decision outcome preview",
            f"total_decision_events={payload.get('total_decision_events')}",
            f"usable_rows={payload.get('usable_rows')}",
            f"incomplete_rows={payload.get('incomplete_rows')}",
            f"mongo_writes_enabled={payload.get('mongo_writes_enabled')}",
            f"training_executed={payload.get('training_executed')}",
            f"model_files_created={payload.get('model_files_created')}",
            f"tradingview_calls={payload.get('tradingview_calls')}",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = asyncio.run(run(args))
    except Exception as exc:
        print(f"decision-outcome-preview failed: {exc}", file=sys.stderr)
        return 1
    print(console_summary(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
