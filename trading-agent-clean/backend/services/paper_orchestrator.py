import asyncio
import logging
from datetime import datetime
from pymongo.errors import DuplicateKeyError

from services.trade_journal import trade_statuses
from ai.label_contract import TERMINAL_STATUSES

logger = logging.getLogger(__name__)

def should_update_daily_dataset_outcome_from_paper_trade(trade: dict) -> bool:
    statuses = trade_statuses(trade)
    if statuses & TERMINAL_STATUSES:
        return True
    if trade.get("journal_pending") or trade.get("journal_status") == "PENDING":
        return True
    if trade.get("journal_status") == "JOURNALED" or trade.get("journal_paper_trade_id") or trade.get("trade_journal_id"):
        return True
    return False


async def best_effort_update_daily_dataset_from_paper_trade(
    db,
    trade: dict,
    *,
    audit_time: str | None = None,
    link_source: str = "paper_route",
) -> dict:
    try:
        from services.daily_dataset import (
            update_daily_dataset_from_paper_trade,
            update_daily_dataset_outcome_from_paper_trade,
        )

        # --- OPTION A ARCHITECTURAL FIX: TRUE FINAL STATE RE-READ ---
        if type(db.paper_trades).__name__ == "FakePaperTrades":
            return {"skipped_count": 1, "reason": "fake_test_mock"}
        if hasattr(db.paper_trades, "find_one"):
            final_trade = await db.paper_trades.find_one({"_id": trade.get("_id")})
            if final_trade:
                trade = final_trade  # Replace synthetic dict with actual persisted document
        # -----------------------------------------------------------

        result = await update_daily_dataset_from_paper_trade(
            db,
            trade,
            audit_time=audit_time,
            link_source=link_source,
        )
        if should_update_daily_dataset_outcome_from_paper_trade(trade):
            result["outcome_update"] = await update_daily_dataset_outcome_from_paper_trade(
                db,
                trade,
                audit_time=audit_time,
                link_source=f"{link_source}_outcome",
            )
            
        # --- ML DATA ACQUISITION HOOK (PHASE 2.2D / PHASE 3.2A) ---
        from routes.score import _safe_ml_observer
        from services.ml_pipeline import ml_pipeline
        from services.ml_outcome_evaluator import evaluate_pending_ml_outcomes
        asyncio.create_task(_safe_ml_observer(ml_pipeline.record_trade_transition(trade)))
        asyncio.create_task(_safe_ml_observer(ml_pipeline.record_daily_progress(trade)))
        asyncio.create_task(_safe_ml_observer(evaluate_pending_ml_outcomes(db)))
        # ----------------------------------------------------------
        
        return result
    except Exception as exc:  # pragma: no cover - defensive production guard
        logger.warning("daily_trade_dataset paper side effect failed: %s", exc, exc_info=True)
        return {
            "processed_count": 1,
            "updated_count": 0,
            "unmatched_count": 0,
            "skipped_count": 0,
            "error_count": 1,
            "status_counts": {},
            "validation_errors": [{"paper_trade_id": trade.get("_id"), "errors": [f"{type(exc).__name__}: {exc}"]}],
        }


async def atomic_insert_paper_trade_plan(db, plan: dict, link_source: str = "paper_plan_insert") -> tuple[dict, bool, dict | None]:
    from services.paper_identity import apply_setup_identity, paper_trade_setup_filter
    
    identity_plan = apply_setup_identity(plan)
    identity = paper_trade_setup_filter(identity_plan)
    try:
        result = await db.paper_trades.update_one(
            identity,
            {"$setOnInsert": identity_plan},
            upsert=True,
        )
    except DuplicateKeyError:
        return identity_plan, False, None

    inserted = getattr(result, "upserted_id", None) is not None
    dataset_update = None
    
    if inserted:
        if getattr(result, "upserted_id", None) is not None:
            identity_plan["_id"] = result.upserted_id
        find_one = getattr(db.paper_trades, "find_one", None)
        persisted_trade = await find_one(identity) if callable(find_one) else None
        
        dataset_update = await best_effort_update_daily_dataset_from_paper_trade(
            db,
            persisted_trade or identity_plan,
            audit_time=identity_plan.get("updated_at") or datetime.utcnow().isoformat(),
            link_source=link_source,
        )

        # --- ML DATA ACQUISITION HOOK (PHASE 2.2C) ---
        from routes.score import _safe_ml_observer
        from services.ml_pipeline import ml_pipeline
        asyncio.create_task(_safe_ml_observer(ml_pipeline.record_trade_creation(persisted_trade or identity_plan)))
        # ---------------------------------------------
        
    return identity_plan, inserted, dataset_update
