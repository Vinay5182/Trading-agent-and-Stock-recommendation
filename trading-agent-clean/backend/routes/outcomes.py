"""
Outcomes API Route
Exposes endpoints for fetching and evaluating trade outcomes.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from database import get_database
from services.trade_outcome_evaluator import (
    evaluate_all_trade_outcomes,
    evaluate_and_save_trade_outcome,
)

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/trade/{trade_id}")
async def get_trade_outcome(trade_id: str, db=Depends(get_database)):
    """Retrieves computed trade outcome for a specific trade ID using clean API contracts."""
    if trade_id and trade_id.startswith("dtd_v1_"):
        raise HTTPException(
            status_code=400,
            detail="Dataset identifiers must be queried through the daily dataset endpoint."
        )

    # 1. Search trade_outcomes collection by trade_id
    doc = await db["trade_outcomes"].find_one({"trade_id": trade_id})
    if doc:
        doc["_id"] = str(doc["_id"])
        return {"ok": True, "outcome": doc}

    # 2. Search in swing_tv_confirmations, momentum_tv_confirmations, or paper_trades
    trade = await db["swing_tv_confirmations"].find_one({"$or": [{"trade_id": trade_id}, {"_id": trade_id}]})
    if not trade:
        trade = await db["momentum_tv_confirmations"].find_one({"$or": [{"trade_id": trade_id}, {"_id": trade_id}]})
    if not trade:
        trade = await db["paper_trades"].find_one({"$or": [{"trade_id": trade_id}, {"paper_trade_id": trade_id}, {"_id": trade_id}]})

    if not trade:
        from bson import ObjectId
        try:
            obj_id = ObjectId(trade_id)
            trade = (
                await db["swing_tv_confirmations"].find_one({"_id": obj_id})
                or await db["momentum_tv_confirmations"].find_one({"_id": obj_id})
                or await db["paper_trades"].find_one({"_id": obj_id})
            )
        except Exception:
            pass

    if not trade:
        raise HTTPException(status_code=404, detail=f"Trade document {trade_id} not found.")

    outcome = await evaluate_and_save_trade_outcome(db, trade)
    return {"ok": True, "outcome": outcome}


@router.post("/evaluate-all")
async def trigger_evaluate_all_outcomes(db=Depends(get_database)):
    """Backfills and evaluates outcomes for all trade candidates stored in MongoDB."""
    summary = await evaluate_all_trade_outcomes(db)
    return {"ok": True, "summary": summary}
