import os
import sys
import asyncio
import pytest
from motor.motor_asyncio import AsyncIOMotorClient

backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from services.candidate_trade_outcomes_service import (
    COLLECTION_NAME,
    build_candidate_outcome_id,
    create_candidate_trade_outcome_doc,
    persist_candidate_trade_outcome_records,
)

MONGO_URI = "mongodb://localhost:27017"
DB_NAME = "trading_agent_clean"


def get_test_db():
    client = AsyncIOMotorClient(MONGO_URI)
    return client[DB_NAME]


def run_async(coro):
    return asyncio.run(coro)


def test_candidate_trade_outcomes_pipeline():
    async def _test():
        db = get_test_db()
        coll = db[COLLECTION_NAME]

        symbol = "NSE:TEST_CTO_STOCK"
        scan_date = "2026-06-29"

        # Cleanup
        await coll.delete_many({"identity.symbol": symbol})

        try:
            # 1. Test Deterministic Candidate Outcome ID
            cid_1 = build_candidate_outcome_id("TEST_CTO_STOCK", "2026-06-29", "MULTI")
            cid_2 = build_candidate_outcome_id("NSE:TEST_CTO_STOCK", "2026-06-29", "MULTI")
            assert cid_1 == cid_2, "Deterministic ID generator failed!"

            # 2. Test Document Creation with Full 13 Sub-document Schema
            candidate_features = {
                "score": 88,
                "nse_score": 88,
                "momentum_score": 85,
                "strategy_type": "MULTI",
                "momentum_candidate": True,
                "swing_candidate": False,
                "score_version": "score_v2_strict_numeric",
                "score_breakdown": {"price_strength": 18, "liquidity": 19},
                "normalized_score_inputs": {"current_price": 500.0, "previous_close": 490.0},
            }
            trade_plan = {
                "entry_price": 500.0,
                "stop_loss": 490.0,
                "target_1": 525.0,
                "target_2": 550.0,
                "target_3": 575.0,
                "risk_reward_ratio": 2.5,
                "position_size": 100,
            }
            selection_status = {
                "selected_for_trade": True,
                "selection_reason": "PASSED_SCORE_AND_RISK_CAPS",
                "rejection_reason": None,
            }

            doc = create_candidate_trade_outcome_doc(
                symbol=symbol,
                scan_date=scan_date,
                candidate_features=candidate_features,
                trade_plan=trade_plan,
                selection_status=selection_status,
            )

            # Verify Schema Sections
            assert "identity" in doc
            assert "market_context" in doc
            assert "candidate_features" in doc
            assert "trade_plan" in doc
            assert "selection_status" in doc
            assert "entry_info" in doc
            assert "price_evolution" in doc
            assert "target_tracking" in doc
            assert "stoploss" in doc
            assert "holding_info" in doc
            assert "exit" in doc
            assert "final_label" in doc
            assert "ml_labels" in doc
            assert "audit" in doc

            # 3. Test Persistence & Idempotency
            res = await persist_candidate_trade_outcome_records(db, [doc])
            assert res["inserted_count"] == 1

            # Duplicate pass check (must not duplicate)
            res_dup = await persist_candidate_trade_outcome_records(db, [doc])
            assert res_dup["inserted_count"] == 0

            # 4. Verify DB Document State
            db_doc = await coll.find_one({"identity.candidate_id": doc["identity"]["candidate_id"]})
            assert db_doc is not None
            assert db_doc["candidate_features"]["score"] == 88
            assert db_doc["trade_plan"]["entry_price"] == 500.0
            assert db_doc["selection_status"]["selected_for_trade"] is True

        finally:
            await coll.delete_many({"identity.symbol": symbol})

    run_async(_test())
