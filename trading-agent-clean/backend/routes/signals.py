from datetime import datetime

from fastapi import APIRouter, Query

from database import get_database
from services.tradingview_manager import tradingview_manager
from tv_confirmation import PAPER_PLAN_FIELDS, confirm_momentum_symbol_timeframe, confirm_symbol_timeframe


router = APIRouter()


@router.post("/build-tv-confirmed")
async def build_tv_confirmed_signals(
    limit: int = Query(default=5, ge=1, le=25),
    timeframe: str = Query(default="1D"),
    scan_run_id: str | None = Query(default=None),
    save: bool = Query(default=False),
) -> dict:
    db = get_database()
    active_scan_run_id = scan_run_id
    if active_scan_run_id is None:
        latest_run = await db.scan_runs.find_one({}, {"_id": 0, "scan_run_id": 1}, sort=[("created_at", -1)])
        active_scan_run_id = latest_run["scan_run_id"] if latest_run else None
    if active_scan_run_id is None:
        return {
            "scan_run_id": None,
            "processed": 0,
            "signals_count": 0,
            "upserted_count": 0,
            "modified_count": 0,
            "signals": [],
            "saved": False,
            "storage": "response_only",
        }

    cursor = db.scan_rows.find(
        {
            "scan_run_id": active_scan_run_id,
            "selected_for_tv": True,
            "status": {"$in": ["SCORED", "BELOW_THRESHOLD"]},
        },
        {"_id": 0},
    ).sort("score", -1).limit(limit)

    signals = []
    processed = 0
    async for row in cursor:
        processed += 1
        confirmation = await tradingview_manager.run_sync(
            "signals.confirm_symbol_timeframe",
            confirm_symbol_timeframe,
            row["tradingview_symbol"],
            timeframe,
            retries=1,
        )
        if not confirmation["tv_confirmed"] or not confirmation.get("paper_plan_valid"):
            continue
        now = datetime.utcnow().isoformat()
        signals.append(
            {
                "symbol": row["tradingview_symbol"],
                "timeframe": timeframe,
                "signal_type": "SWING_TV_CONFIRMED",
                "paper_only": True,
                "tv_confirmed": True,
                "score": row.get("score"),
                "nse_score": row.get("nse_score"),
                "momentum_score": row.get("momentum_score"),
                "last_close": confirmation.get("last_close"),
                "previous_close": confirmation.get("previous_close"),
                "last_volume": confirmation.get("last_volume"),
                "avg_volume_20": confirmation.get("avg_volume_20"),
                "reason": "TV_CONFIRMED",
                "status": "CONFIRMED_SIGNAL",
                "entry": confirmation.get("paper_entry_price"),
                "sl": confirmation.get("paper_stop_loss"),
                "t1": confirmation.get("paper_target_1"),
                "rr": confirmation.get("paper_rr_1"),
                "next_action": confirmation.get("next_action_for_paper_trade"),
                **{field: confirmation.get(field) for field in PAPER_PLAN_FIELDS},
                "created_at": now,
                "updated_at": now,
                "source": "tradingview",
            }
        )

    upserted_count = 0
    modified_count = 0
    if save and signals:
        await db.paper_signals.create_index(
            [("symbol", 1), ("timeframe", 1), ("signal_type", 1), ("paper_only", 1), ("source", 1)],
            unique=True,
        )
        for signal in signals:
            identity = {
                "symbol": signal["symbol"],
                "timeframe": signal["timeframe"],
                "signal_type": signal["signal_type"],
                "paper_only": signal["paper_only"],
                "source": signal["source"],
            }
            created_at = signal["created_at"]
            update_doc = signal.copy()
            update_doc.pop("created_at", None)
            result = await db.paper_signals.update_one(
                identity,
                {"$set": update_doc, "$setOnInsert": {"created_at": created_at}},
                upsert=True,
            )
            upserted_count += 1 if result.upserted_id is not None else 0
            modified_count += result.modified_count

    return {
        "scan_run_id": active_scan_run_id,
        "processed": processed,
        "signals": signals,
        "signals_count": len(signals),
        "count": len(signals),
        "saved": save,
        "upserted_count": upserted_count,
        "modified_count": modified_count,
        "storage": "paper_signals" if save else "response_only",
    }


@router.get("/paper")
async def get_paper_signals(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    cursor = get_database().paper_signals.find(
        {"paper_only": True},
        {"_id": 0},
    ).sort("updated_at", -1).limit(limit)
    signals = [row async for row in cursor]
    return {"count": len(signals), "signals": signals}


@router.post("/build-momentum-tv-confirmed")
async def build_momentum_tv_confirmed_signals(
    limit: int = Query(default=5, ge=1, le=25),
    timeframe: str = Query(default="1D"),
    scan_run_id: str | None = Query(default=None),
    save: bool = Query(default=False),
) -> dict:
    db = get_database()
    active_scan_run_id = scan_run_id
    if active_scan_run_id is None:
        latest_run = await db.scan_runs.find_one({}, {"_id": 0, "scan_run_id": 1}, sort=[("created_at", -1)])
        active_scan_run_id = latest_run["scan_run_id"] if latest_run else None
    if active_scan_run_id is None:
        return {"processed": 0, "signals_count": 0, "saved": save, "upserted_count": 0, "modified_count": 0, "signals": []}

    cursor = db.scan_rows.find(
        {
            "scan_run_id": active_scan_run_id,
            "momentum_candidate": True,
            "momentum_score": {"$gte": 70},
        },
        {"_id": 0},
    ).sort([("momentum_score", -1), ("relative_volume", -1), ("traded_value", -1)]).limit(limit)

    processed = 0
    signals = []
    async for row in cursor:
        processed += 1
        confirmation = await tradingview_manager.run_sync(
            "signals.confirm_momentum_symbol_timeframe",
            confirm_momentum_symbol_timeframe,
            row["tradingview_symbol"],
            timeframe,
            retries=1,
        )
        if not (
            confirmation.get("momentum_confirmed") is True
            and confirmation.get("reason") == "MOMENTUM_CONFIRMED"
            and (confirmation.get("candles_count") or 0) >= 50
            and confirmation.get("paper_plan_valid") is True
        ):
            continue
        now = datetime.utcnow().isoformat()
        signals.append(
            {
                "symbol": row["tradingview_symbol"],
                "timeframe": timeframe,
                "signal_type": "MOMENTUM_TV_CONFIRMED",
                "paper_only": True,
                "tv_confirmed": True,
                "momentum_confirmed": True,
                "score": row.get("score"),
                "nse_score": row.get("nse_score"),
                "momentum_score": row.get("momentum_score"),
                "last_close": confirmation.get("last_close"),
                "previous_close": confirmation.get("previous_close"),
                "last_volume": confirmation.get("last_volume"),
                "avg_volume_20": confirmation.get("avg_volume_20"),
                "close_change_5d_percent": confirmation.get("close_change_5d_percent"),
                "recent_high_20": confirmation.get("recent_high_20"),
                "reason": "MOMENTUM_CONFIRMED",
                "status": "MOMENTUM_CONFIRMED",
                "entry": confirmation.get("paper_entry_price"),
                "sl": confirmation.get("paper_stop_loss"),
                "t1": confirmation.get("paper_target_1"),
                "rr": confirmation.get("paper_rr_1"),
                "next_action": confirmation.get("next_action_for_paper_trade"),
                **{field: confirmation.get(field) for field in PAPER_PLAN_FIELDS},
                "created_at": now,
                "updated_at": now,
                "source": "tradingview",
            }
        )

    upserted_count = 0
    modified_count = 0
    if save and signals:
        await db.paper_signals.create_index(
            [("symbol", 1), ("timeframe", 1), ("signal_type", 1), ("paper_only", 1), ("source", 1)],
            unique=True,
        )
        for signal in signals:
            identity = {
                "symbol": signal["symbol"],
                "timeframe": signal["timeframe"],
                "signal_type": signal["signal_type"],
                "paper_only": signal["paper_only"],
                "source": signal["source"],
            }
            created_at = signal["created_at"]
            update_doc = signal.copy()
            update_doc.pop("created_at", None)
            result = await db.paper_signals.update_one(
                identity,
                {"$set": update_doc, "$setOnInsert": {"created_at": created_at}},
                upsert=True,
            )
            upserted_count += 1 if result.upserted_id is not None else 0
            modified_count += result.modified_count

    return {
        "scan_run_id": active_scan_run_id,
        "processed": processed,
        "signals_count": len(signals),
        "count": len(signals),
        "saved": save,
        "upserted_count": upserted_count,
        "modified_count": modified_count,
        "signals": signals,
    }
