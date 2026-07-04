from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.historical_training_bridge import (
    DEFAULT_HORIZON,
    DEFAULT_LOOKBACK,
    DEFAULT_STRATEGIES,
    build_historical_training_preview_from_collection,
    parse_strategies,
)
from config import settings
from services.historical_ohlcv_store import HISTORICAL_OHLCV_COLLECTION
from services.migration_safety import safe_json_value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview historical OHLCV training rows without writes or training.")
    parser.add_argument("--mode", choices=("preview",), default="preview")
    parser.add_argument("--database-name", default=settings.DATABASE_NAME)
    parser.add_argument("--provider", default="yfinance")
    parser.add_argument("--exchange", default="NSE")
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK)
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    parser.add_argument("--strategies", nargs="+", default=list(DEFAULT_STRATEGIES))
    parser.add_argument("--limit", type=int)
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

    client = AsyncIOMotorClient(settings.MONGO_URI)
    return client, client[database_name]


def _collection_for(db: Any) -> Any:
    if hasattr(db, "__getitem__"):
        try:
            return db[HISTORICAL_OHLCV_COLLECTION]
        except Exception:
            pass
    return getattr(db, HISTORICAL_OHLCV_COLLECTION)


async def run(args: argparse.Namespace, *, db_override: Any | None = None) -> dict[str, Any]:
    strategies = parse_strategies(args.strategies)
    client = None
    db = db_override
    if db is None:
        client, db = await _open_live_db(args.database_name)
    try:
        result = await build_historical_training_preview_from_collection(
            _collection_for(db),
            provider=args.provider,
            exchange=args.exchange,
            timeframe=args.timeframe,
            symbols=args.symbols,
            lookback=args.lookback,
            horizon=args.horizon,
            strategies=strategies,
            limit=args.limit,
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
            "Historical training preview",
            f"ok={payload.get('ok')}",
            f"setup_rows={payload.get('setup_rows')}",
            f"eligible_rows={payload.get('eligible_rows')}",
            f"excluded_rows={payload.get('excluded_rows')}",
            f"unlabeled_rows={payload.get('unlabeled_rows')}",
            f"mongo_writes_performed={payload.get('mongo_writes_performed')}",
            f"training_executed={payload.get('training_executed')}",
            f"tradingview_calls={payload.get('tradingview_calls')}",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = asyncio.run(run(args))
    except Exception as exc:
        print(f"historical-training-preview failed: {exc}", file=sys.stderr)
        return 1
    print(console_summary(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
