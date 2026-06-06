from datetime import datetime
from math import floor

from fastapi import APIRouter, Query
from pymongo.errors import DuplicateKeyError

from database import get_database
from routes.signals import build_tv_confirmed_signals
from tv_client import TradingViewClient
from tv_confirmation import PAPER_PLAN_FIELDS, build_price_action_paper_plan_from_candles, confirm_from_candles, confirm_momentum_from_candles


router = APIRouter()
TRACKABLE_STATUSES = ["PLANNED", "ACTIVE", "TARGET_1_HIT"]
OPEN_STATUSES = ["PLANNED", "ACTIVE", "TARGET_1_HIT"]
CLOSED_STATUSES = ["TARGET_2_HIT", "STOPPED", "STOPPED_AFTER_T1"]


def build_plan_from_candles(signal: dict, candles: list[dict], paper_capital: float, risk_percent: float) -> dict | None:
    if len(candles) < 10:
        return None
    signal_has_paper_plan = "paper_plan_valid" in signal
    if signal.get("paper_plan_valid") is True:
        paper_plan = {field: signal.get(field) for field in PAPER_PLAN_FIELDS}
    elif signal_has_paper_plan:
        return None
    else:
        signal_type = signal.get("signal_type") or signal.get("source_signal_type", "SWING_TV_CONFIRMED")
        strategy = "momentum" if signal_type == "MOMENTUM_TV_CONFIRMED" else "swing"
        status = "MOMENTUM_CONFIRMED" if strategy == "momentum" else "CONFIRMED_SIGNAL"
        paper_plan = build_price_action_paper_plan_from_candles(candles, strategy, status, signal.get("timeframe", "1D"))
        if not paper_plan.get("paper_plan_valid"):
            return None

    entry_price = paper_plan.get("paper_entry_price")
    stop_loss = paper_plan.get("paper_stop_loss")
    target_1 = paper_plan.get("paper_target_1")
    target_2 = paper_plan.get("paper_target_2")
    target_3 = paper_plan.get("paper_target_3")
    if None in (entry_price, stop_loss, target_1, target_2, target_3):
        return None
    risk_per_share = entry_price - stop_loss
    if risk_per_share <= 0:
        return None
    risk_amount = paper_capital * risk_percent / 100
    quantity = floor(risk_amount / risk_per_share)
    if quantity <= 0:
        return None
    now = datetime.utcnow().isoformat()
    return {
        "symbol": signal["symbol"],
        "timeframe": signal["timeframe"],
        "source_signal_type": signal.get("signal_type", "SWING_TV_CONFIRMED"),
        "paper_only": True,
        "status": "PLANNED",
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "target_1": target_1,
        "target_2": target_2,
        "target_3": target_3,
        "risk_per_share": risk_per_share,
        "risk_reward_1": paper_plan.get("paper_rr_1"),
        "risk_reward_2": paper_plan.get("paper_rr_2"),
        "risk_reward_3": paper_plan.get("paper_rr_3"),
        **{field: paper_plan.get(field) for field in PAPER_PLAN_FIELDS},
        "avoid_condition": paper_plan.get("invalidation_condition"),
        "paper_only_note": "Paper plan only. No live trading, broker API, or order placement.",
        "paper_capital": paper_capital,
        "risk_percent": risk_percent,
        "risk_amount": risk_amount,
        "quantity": quantity,
        "created_at": now,
        "updated_at": now,
        "source": "tradingview",
    }


def calculate_pnl(plan: dict, latest_close: float, exit_price: float | None) -> tuple[float, float]:
    price = exit_price if exit_price is not None else latest_close
    pnl_per_share = price - plan["entry_price"]
    return pnl_per_share * plan["quantity"], (pnl_per_share / plan["entry_price"]) * 100


def update_plan_status(plan: dict, latest: dict) -> dict:
    latest_high = latest["high"]
    latest_low = latest["low"]
    latest_close = latest["close"]
    status = plan["status"]
    new_status = status
    exit_price = None
    exit_reason = None

    if status == "PLANNED" and latest_high >= plan["entry_price"]:
        new_status = "ACTIVE"
    elif status == "ACTIVE":
        if latest_low <= plan["stop_loss"]:
            new_status = "STOPPED"
            exit_price = plan["stop_loss"]
            exit_reason = "STOP_LOSS_HIT"
        elif latest_high >= plan["target_2"]:
            new_status = "TARGET_2_HIT"
            exit_price = plan["target_2"]
            exit_reason = "TARGET_2_HIT"
        elif latest_high >= plan["target_1"]:
            new_status = "TARGET_1_HIT"
            exit_reason = "TARGET_1_HIT"
    elif status == "TARGET_1_HIT":
        if latest_low <= plan["stop_loss"]:
            new_status = "STOPPED_AFTER_T1"
            exit_price = plan["stop_loss"]
            exit_reason = "STOPPED_AFTER_T1"
        elif latest_high >= plan["target_2"]:
            new_status = "TARGET_2_HIT"
            exit_price = plan["target_2"]
            exit_reason = "TARGET_2_HIT"

    paper_pnl, paper_pnl_percent = calculate_pnl(plan, latest_close, exit_price)
    now = datetime.utcnow().isoformat()
    update = {
        "latest_close": latest_close,
        "latest_high": latest_high,
        "latest_low": latest_low,
        "last_checked_at": now,
        "updated_at": now,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "paper_pnl": paper_pnl,
        "paper_pnl_percent": paper_pnl_percent,
    }
    if new_status != status:
        update["status"] = new_status
        update["status_updated_at"] = now
    return update


@router.post("/build-plans")
async def build_paper_plans(
    limit: int = Query(default=5, ge=1, le=25),
    timeframe: str = Query(default="1D"),
    save: bool = Query(default=True),
    paper_capital: float = Query(default=100000, gt=0),
    risk_percent: float = Query(default=1, gt=0),
    signal_type: str = Query(default="SWING_TV_CONFIRMED"),
) -> dict:
    db = get_database()
    allowed_types = ["SWING_TV_CONFIRMED", "MOMENTUM_TV_CONFIRMED"]
    signal_query = {
        "paper_only": True,
        "tv_confirmed": True,
        "timeframe": timeframe,
    }
    if signal_type == "ALL":
        signal_query["signal_type"] = {"$in": allowed_types}
    else:
        signal_query["signal_type"] = signal_type
    if signal_type == "MOMENTUM_TV_CONFIRMED":
        signal_query["momentum_confirmed"] = True
    cursor = db.paper_signals.find(
        signal_query,
        {"_id": 0},
    ).sort("updated_at", -1).limit(limit)

    plans = []
    processed = 0
    async for signal in cursor:
        processed += 1
        client = TradingViewClient()
        client.connect_to_debug_port()
        client.open_symbol(signal["symbol"])
        candles = client.fetch_candles(timeframe, min_candles=1)
        plan = build_plan_from_candles(signal, candles, paper_capital, risk_percent)
        if plan:
            plans.append(plan)

    upserted_count = 0
    modified_count = 0
    if save and plans:
        await db.paper_trades.create_index(
            [("symbol", 1), ("timeframe", 1), ("source_signal_type", 1), ("paper_only", 1), ("status", 1)],
            unique=True,
        )
        for plan in plans:
            identity = {
                "symbol": plan["symbol"],
                "timeframe": plan["timeframe"],
                "source_signal_type": plan["source_signal_type"],
                "paper_only": plan["paper_only"],
                "status": plan["status"],
            }
            created_at = plan["created_at"]
            update_doc = plan.copy()
            update_doc.pop("created_at", None)
            result = await db.paper_trades.update_one(
                identity,
                {"$set": update_doc, "$setOnInsert": {"created_at": created_at}},
                upsert=True,
            )
            upserted_count += 1 if result.upserted_id is not None else 0
            modified_count += result.modified_count

    return {
        "processed": processed,
        "plans_count": len(plans),
        "saved": save,
        "upserted_count": upserted_count,
        "modified_count": modified_count,
        "plans": plans,
    }


@router.get("/plans")
async def get_paper_plans(
    limit: int = Query(default=100, ge=1, le=500),
    source_signal_type: str = Query(default="ALL"),
) -> dict:
    query = {"paper_only": True}
    if source_signal_type != "ALL":
        query["source_signal_type"] = source_signal_type
    cursor = get_database().paper_trades.find(query, {"_id": 0}).sort("updated_at", -1).limit(limit)
    plans = [row async for row in cursor]
    return {"count": len(plans), "plans": plans}


@router.post("/update-plans")
async def update_paper_plans(
    limit: int = Query(default=10, ge=1, le=100),
    timeframe: str = Query(default="1D"),
) -> dict:
    db = get_database()
    cursor = db.paper_trades.find(
        {"paper_only": True, "status": {"$in": TRACKABLE_STATUSES}, "timeframe": timeframe},
    ).sort("updated_at", -1).limit(limit)

    results = []
    processed = 0
    updated_count = 0
    async for plan in cursor:
        processed += 1
        client = TradingViewClient()
        client.connect_to_debug_port()
        client.open_symbol(plan["symbol"])
        candles = client.fetch_candles(timeframe, min_candles=1)
        if not candles:
            results.append({"symbol": plan["symbol"], "status": plan["status"], "updated": False, "reason": "NO_CANDLES"})
            continue
        update = update_plan_status(plan, candles[-1])
        try:
            result = await db.paper_trades.update_one({"_id": plan["_id"]}, {"$set": update})
            modified = result.modified_count
        except DuplicateKeyError:
            merged_status = update.get("status", plan["status"])
            identity = {
                "symbol": plan["symbol"],
                "timeframe": plan["timeframe"],
                "source_signal_type": plan["source_signal_type"],
                "paper_only": plan["paper_only"],
                "status": merged_status,
            }
            await db.paper_trades.update_one(identity, {"$set": update})
            await db.paper_trades.delete_one({"_id": plan["_id"]})
            modified = 1
        updated_count += modified
        results.append(
            {
                "symbol": plan["symbol"],
                "previous_status": plan["status"],
                "status": update.get("status", plan["status"]),
                "updated": modified > 0,
                "latest_close": update["latest_close"],
                "latest_high": update["latest_high"],
                "latest_low": update["latest_low"],
                "exit_reason": update["exit_reason"],
                "paper_pnl": update["paper_pnl"],
                "paper_pnl_percent": update["paper_pnl_percent"],
            }
        )

    return {"processed": processed, "updated_count": updated_count, "results": results}


@router.get("/trades")
async def get_paper_trades(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    cursor = get_database().paper_trades.find({"paper_only": True}, {"_id": 0}).sort("updated_at", -1).limit(limit)
    trades = [row async for row in cursor]
    return {"count": len(trades), "trades": trades}


@router.get("/summary")
async def get_paper_summary() -> dict:
    cursor = get_database().paper_trades.find({"paper_only": True}, {"_id": 0})
    trades = [row async for row in cursor]
    total_pnl = sum(trade.get("paper_pnl") or 0 for trade in trades)
    closed = [trade for trade in trades if trade.get("status") in CLOSED_STATUSES]
    winning = [trade for trade in closed if (trade.get("paper_pnl") or 0) > 0]
    losing = [trade for trade in closed if (trade.get("paper_pnl") or 0) < 0]
    return {
        "total_trades": len(trades),
        "planned": sum(1 for trade in trades if trade.get("status") == "PLANNED"),
        "active": sum(1 for trade in trades if trade.get("status") == "ACTIVE"),
        "target_1_hit": sum(1 for trade in trades if trade.get("status") == "TARGET_1_HIT"),
        "target_2_hit": sum(1 for trade in trades if trade.get("status") == "TARGET_2_HIT"),
        "stopped": sum(1 for trade in trades if trade.get("status") == "STOPPED"),
        "stopped_after_t1": sum(1 for trade in trades if trade.get("status") == "STOPPED_AFTER_T1"),
        "closed_trades": len(closed),
        "open_trades": sum(1 for trade in trades if trade.get("status") in OPEN_STATUSES),
        "total_paper_pnl": total_pnl,
        "average_paper_pnl": total_pnl / len(trades) if trades else 0,
        "winning_trades": len(winning),
        "losing_trades": len(losing),
        "win_rate_percent": (len(winning) / len(closed) * 100) if closed else 0,
        "symbols": sorted({trade.get("symbol") for trade in trades if trade.get("symbol")}),
    }


@router.get("/active")
async def get_active_paper_trades(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    cursor = get_database().paper_trades.find(
        {"paper_only": True, "status": {"$in": OPEN_STATUSES}},
        {"_id": 0},
    ).sort("updated_at", -1).limit(limit)
    trades = [row async for row in cursor]
    return {"count": len(trades), "trades": trades}


@router.post("/run-pipeline")
async def run_paper_pipeline(
    limit: int = Query(default=1, ge=1),
    timeframe: str = Query(default="1D"),
    dry_run: bool = Query(default=False),
    strategy: str = Query(default="swing"),
) -> dict:
    started = datetime.utcnow()
    effective_limit = min(limit, 3)
    summary_before = await get_paper_summary()
    response = {
        "mode": "paper_only",
        "paper_only": True,
        "dry_run": dry_run,
        "mongo_writes_enabled": not dry_run,
        "live_trading": False,
        "broker_orders": False,
        "strategy": strategy,
        "timeframe": timeframe,
        "limit": effective_limit,
        "requested_limit": limit,
        "limit_warning": "limit clamped to 3" if limit > 3 else None,
        "started_at": started.isoformat(),
        "finished_at": None,
        "duration_seconds": None,
        "signals_step_ok": False,
        "plans_step_ok": False,
        "updates_step_ok": False,
        "summary_step_ok": False,
        "error": None,
        "signals": {},
        "swing_signals": {},
        "momentum_signals": {},
        "plans": {},
        "updates": {},
        "summary_before": summary_before,
        "summary_after": None,
        "summary": {},
        "tv_fetch_count": 0,
        "tv_cache_hits": 0,
        "tv_cache_misses": 0,
        "cached_symbols": [],
    }
    tv_cache = {}
    allowed_strategies = {"swing", "momentum", "all"}
    if strategy not in allowed_strategies:
        response["error"] = f"invalid_strategy: {strategy}"
        return finish_pipeline_response(response, started)

    def get_cached_candles(symbol: str) -> list[dict]:
        key = f"{symbol}|{timeframe}"
        if key in tv_cache:
            response["tv_cache_hits"] += 1
            return tv_cache[key]["candles"]
        response["tv_cache_misses"] += 1
        response["tv_fetch_count"] += 1
        client = TradingViewClient()
        client.connect_to_debug_port()
        client.open_symbol(symbol)
        candles = client.fetch_candles(timeframe, min_candles=1)
        tv_cache[key] = {"candles": candles, "diagnostics": client.diagnostics}
        response["cached_symbols"] = sorted(tv_cache.keys())
        return candles

    try:
        swing_signals = {"processed": 0, "signals_count": 0, "saved": not dry_run, "upserted_count": 0, "modified_count": 0, "signals": []}
        momentum_signals = {"processed": 0, "signals_count": 0, "saved": not dry_run, "upserted_count": 0, "modified_count": 0, "signals": []}
        if strategy in {"swing", "all"}:
            swing_signals = await build_pipeline_signals(effective_limit, timeframe, get_cached_candles, save=not dry_run)
        if strategy in {"momentum", "all"}:
            momentum_signals = await build_pipeline_momentum_signals(effective_limit, timeframe, get_cached_candles, save=not dry_run)
        signals = {
            "processed": swing_signals.get("processed", 0) + momentum_signals.get("processed", 0),
            "signals_count": swing_signals.get("signals_count", 0) + momentum_signals.get("signals_count", 0),
            "saved": not dry_run,
            "upserted_count": swing_signals.get("upserted_count", 0) + momentum_signals.get("upserted_count", 0),
            "modified_count": swing_signals.get("modified_count", 0) + momentum_signals.get("modified_count", 0),
            "signals": swing_signals.get("signals", []) + momentum_signals.get("signals", []),
        }
        response["signals_step_ok"] = True
        response["signals"] = signals
        response["swing_signals"] = swing_signals
        response["momentum_signals"] = momentum_signals
    except Exception as exc:
        response["error"] = f"signals_step_failed: {exc}"
        return finish_pipeline_response(response, started)

    try:
        plans = await build_pipeline_plans(
            effective_limit,
            timeframe,
            get_cached_candles,
            save=not dry_run,
            source_signals=signals.get("signals") if dry_run else None,
            signal_type={"swing": "SWING_TV_CONFIRMED", "momentum": "MOMENTUM_TV_CONFIRMED", "all": "ALL"}[strategy],
        )
        response["plans_step_ok"] = True
        response["plans"] = plans
    except Exception as exc:
        response["error"] = f"plans_step_failed: {exc}"
        return finish_pipeline_response(response, started)

    try:
        updates = await update_pipeline_plans(
            effective_limit,
            timeframe,
            get_cached_candles,
            save=not dry_run,
            preview_plans=plans.get("plans") if dry_run else None,
            source_signal_type={"swing": "SWING_TV_CONFIRMED", "momentum": "MOMENTUM_TV_CONFIRMED", "all": "ALL"}[strategy],
        )
        response["updates_step_ok"] = True
        response["updates"] = {
            "processed": updates.get("processed", 0),
            "updated_count": updates.get("updated_count", 0),
            "statuses": [row.get("status") for row in updates.get("results", [])],
        }
    except Exception as exc:
        response["error"] = f"updates_step_failed: {exc}"

    try:
        response["summary_after"] = None if dry_run else await get_paper_summary()
        response["summary"] = response["summary_after"] or response["summary_before"]
        response["summary_step_ok"] = True
    except Exception as exc:
        response["error"] = response["error"] or f"summary_step_failed: {exc}"
    return finish_pipeline_response(response, started)


def finish_pipeline_response(response: dict, started: datetime) -> dict:
    finished = datetime.utcnow()
    response["finished_at"] = finished.isoformat()
    response["duration_seconds"] = (finished - started).total_seconds()
    return {
        key: value
        for key, value in response.items()
        if value is not None or key != "limit_warning"
    }


async def build_pipeline_signals(limit: int, timeframe: str, get_candles, save: bool = True) -> dict:
    db = get_database()
    latest_run = await db.scan_runs.find_one({}, {"_id": 0, "scan_run_id": 1}, sort=[("created_at", -1)])
    scan_run_id = latest_run["scan_run_id"] if latest_run else None
    if scan_run_id is None:
        return {"processed": 0, "signals_count": 0, "saved": save, "upserted_count": 0, "modified_count": 0, "signals": []}
    cursor = db.scan_rows.find(
        {"scan_run_id": scan_run_id, "selected_for_tv": True, "status": {"$in": ["SCORED", "BELOW_THRESHOLD"]}},
        {"_id": 0},
    ).sort("score", -1).limit(limit)
    processed = 0
    signals = []
    async for row in cursor:
        processed += 1
        candles = get_candles(row["tradingview_symbol"])
        confirmation = confirm_from_candles(candles)
        paper_plan = build_price_action_paper_plan_from_candles(candles, "swing", "CONFIRMED_SIGNAL", timeframe) if confirmation["tv_confirmed"] else {}
        if confirmation["tv_confirmed"] and paper_plan.get("paper_plan_valid"):
            now = datetime.utcnow().isoformat()
            signals.append({
                "symbol": row["tradingview_symbol"], "timeframe": timeframe, "signal_type": "SWING_TV_CONFIRMED",
                "paper_only": True, "tv_confirmed": True, "score": row.get("score"), "nse_score": row.get("nse_score"),
                "momentum_score": row.get("momentum_score"), "last_close": confirmation.get("last_close"),
                "previous_close": confirmation.get("previous_close"), "last_volume": confirmation.get("last_volume"),
                "avg_volume_20": confirmation.get("avg_volume_20"), "reason": "TV_CONFIRMED",
                "status": "CONFIRMED_SIGNAL", "entry": paper_plan.get("paper_entry_price"),
                "sl": paper_plan.get("paper_stop_loss"), "t1": paper_plan.get("paper_target_1"),
                "rr": paper_plan.get("paper_rr_1"), "next_action": paper_plan.get("next_action_for_paper_trade"),
                **{field: paper_plan.get(field) for field in PAPER_PLAN_FIELDS},
                "created_at": now, "updated_at": now, "source": "tradingview",
            })
    upserted_count, modified_count = await upsert_paper_signals(signals) if save else (0, 0)
    return {
        "processed": processed,
        "signals_count": len(signals),
        "saved": save,
        "upserted_count": upserted_count,
        "modified_count": modified_count,
        "signals": signals,
    }


async def build_pipeline_momentum_signals(limit: int, timeframe: str, get_candles, save: bool = True) -> dict:
    db = get_database()
    latest_run = await db.scan_runs.find_one({}, {"_id": 0, "scan_run_id": 1}, sort=[("created_at", -1)])
    scan_run_id = latest_run["scan_run_id"] if latest_run else None
    if scan_run_id is None:
        return {"processed": 0, "signals_count": 0, "saved": save, "upserted_count": 0, "modified_count": 0, "signals": []}
    cursor = db.scan_rows.find(
        {"scan_run_id": scan_run_id, "momentum_candidate": True, "momentum_score": {"$gte": 70}},
        {"_id": 0},
    ).sort([("momentum_score", -1), ("relative_volume", -1), ("traded_value", -1)]).limit(limit)
    processed = 0
    signals = []
    async for row in cursor:
        processed += 1
        candles = get_candles(row["tradingview_symbol"])
        confirmation = confirm_momentum_from_candles(candles)
        paper_plan = build_price_action_paper_plan_from_candles(candles, "momentum", "MOMENTUM_CONFIRMED", timeframe) if confirmation.get("momentum_confirmed") else {}
        if not (
            confirmation.get("momentum_confirmed") is True
            and confirmation.get("reason") == "MOMENTUM_CONFIRMED"
            and (confirmation.get("candles_count") or 0) >= 50
            and paper_plan.get("paper_plan_valid")
        ):
            continue
        now = datetime.utcnow().isoformat()
        signals.append({
            "symbol": row["tradingview_symbol"], "timeframe": timeframe, "signal_type": "MOMENTUM_TV_CONFIRMED",
            "paper_only": True, "tv_confirmed": True, "momentum_confirmed": True,
            "score": row.get("score"), "nse_score": row.get("nse_score"), "momentum_score": row.get("momentum_score"),
            "last_close": confirmation.get("last_close"), "previous_close": confirmation.get("previous_close"),
            "last_volume": confirmation.get("last_volume"), "avg_volume_20": confirmation.get("avg_volume_20"),
            "close_change_5d_percent": confirmation.get("close_change_5d_percent"),
            "recent_high_20": confirmation.get("recent_high_20"), "reason": "MOMENTUM_CONFIRMED",
            "status": "MOMENTUM_CONFIRMED", "entry": paper_plan.get("paper_entry_price"),
            "sl": paper_plan.get("paper_stop_loss"), "t1": paper_plan.get("paper_target_1"),
            "rr": paper_plan.get("paper_rr_1"), "next_action": paper_plan.get("next_action_for_paper_trade"),
            **{field: paper_plan.get(field) for field in PAPER_PLAN_FIELDS},
            "created_at": now, "updated_at": now, "source": "tradingview",
        })
    upserted_count, modified_count = await upsert_paper_signals(signals) if save else (0, 0)
    return {
        "processed": processed,
        "signals_count": len(signals),
        "saved": save,
        "upserted_count": upserted_count,
        "modified_count": modified_count,
        "signals": signals,
    }


async def upsert_paper_signals(signals: list[dict]) -> tuple[int, int]:
    db = get_database()
    upserted_count = 0
    modified_count = 0
    if not signals:
        return 0, 0
    await db.paper_signals.create_index(
        [("symbol", 1), ("timeframe", 1), ("signal_type", 1), ("paper_only", 1), ("source", 1)],
        unique=True,
    )
    for signal in signals:
        identity = {key: signal[key] for key in ("symbol", "timeframe", "signal_type", "paper_only", "source")}
        created_at = signal["created_at"]
        update_doc = signal.copy()
        update_doc.pop("created_at", None)
        result = await db.paper_signals.update_one(identity, {"$set": update_doc, "$setOnInsert": {"created_at": created_at}}, upsert=True)
        upserted_count += 1 if result.upserted_id is not None else 0
        modified_count += result.modified_count
    return upserted_count, modified_count


async def build_pipeline_plans(
    limit: int,
    timeframe: str,
    get_candles,
    save: bool = True,
    source_signals: list[dict] | None = None,
    signal_type: str = "SWING_TV_CONFIRMED",
) -> dict:
    db = get_database()
    plans = []
    if source_signals is None:
        allowed_types = ["SWING_TV_CONFIRMED", "MOMENTUM_TV_CONFIRMED"]
        signal_query = {"paper_only": True, "tv_confirmed": True, "timeframe": timeframe}
        if signal_type == "ALL":
            signal_query["signal_type"] = {"$in": allowed_types}
        else:
            signal_query["signal_type"] = signal_type
        if signal_type == "MOMENTUM_TV_CONFIRMED":
            signal_query["momentum_confirmed"] = True
        cursor = db.paper_signals.find(signal_query, {"_id": 0}).sort("updated_at", -1).limit(limit)
        source_signals = [signal async for signal in cursor]
    for signal in source_signals[:limit]:
        plan = build_plan_from_candles(signal, get_candles(signal["symbol"]), 100000, 1)
        if plan:
            plans.append(plan)
    upserted_count, modified_count = await upsert_paper_plans(plans) if save else (0, 0)
    return {
        "plans_count": len(plans),
        "saved": save,
        "upserted_count": upserted_count,
        "modified_count": modified_count,
        "plans": plans,
    }


async def upsert_paper_plans(plans: list[dict]) -> tuple[int, int]:
    db = get_database()
    upserted_count = 0
    modified_count = 0
    if not plans:
        return 0, 0
    await db.paper_trades.create_index(
        [("symbol", 1), ("timeframe", 1), ("source_signal_type", 1), ("paper_only", 1), ("status", 1)],
        unique=True,
    )
    for plan in plans:
        existing_open = await db.paper_trades.find_one(
            {
                "symbol": plan["symbol"],
                "timeframe": plan["timeframe"],
                "source_signal_type": plan["source_signal_type"],
                "paper_only": plan["paper_only"],
                "status": {"$in": OPEN_STATUSES},
            },
            {"_id": 1},
        )
        if existing_open:
            result = await db.paper_trades.update_one(
                {"_id": existing_open["_id"]},
                {"$set": {"updated_at": plan["updated_at"]}},
            )
            modified_count += result.modified_count
            continue
        identity = {key: plan[key] for key in ("symbol", "timeframe", "source_signal_type", "paper_only", "status")}
        created_at = plan["created_at"]
        update_doc = plan.copy()
        update_doc.pop("created_at", None)
        result = await db.paper_trades.update_one(identity, {"$set": update_doc, "$setOnInsert": {"created_at": created_at}}, upsert=True)
        upserted_count += 1 if result.upserted_id is not None else 0
        modified_count += result.modified_count
    return upserted_count, modified_count


async def update_pipeline_plans(
    limit: int,
    timeframe: str,
    get_candles,
    save: bool = True,
    preview_plans: list[dict] | None = None,
    source_signal_type: str = "SWING_TV_CONFIRMED",
) -> dict:
    db = get_database()
    processed = 0
    updated_count = 0
    results = []
    if preview_plans is None:
        plan_query = {"paper_only": True, "status": {"$in": TRACKABLE_STATUSES}, "timeframe": timeframe}
        if source_signal_type != "ALL":
            plan_query["source_signal_type"] = source_signal_type
        cursor = db.paper_trades.find(plan_query).sort("updated_at", -1).limit(limit)
        plans = [plan async for plan in cursor]
    else:
        plans = preview_plans[:limit]
    for plan in plans:
        processed += 1
        candles = get_candles(plan["symbol"])
        if not candles:
            results.append({"symbol": plan["symbol"], "status": plan["status"], "updated": False, "reason": "NO_CANDLES"})
            continue
        update = update_plan_status(plan, candles[-1])
        modified = 0
        if save:
            try:
                result = await db.paper_trades.update_one({"_id": plan["_id"]}, {"$set": update})
                modified = result.modified_count
            except DuplicateKeyError:
                merged_status = update.get("status", plan["status"])
                identity = {
                    "symbol": plan["symbol"],
                    "timeframe": plan["timeframe"],
                    "source_signal_type": plan["source_signal_type"],
                    "paper_only": plan["paper_only"],
                    "status": merged_status,
                }
                await db.paper_trades.update_one(identity, {"$set": update})
                await db.paper_trades.delete_one({"_id": plan["_id"]})
                modified = 1
            updated_count += modified
        results.append({"symbol": plan["symbol"], "status": update.get("status", plan["status"]), "updated": bool(modified)})
    return {"processed": processed, "updated_count": updated_count, "results": results}
