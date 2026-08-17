from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path
import pytest
from motor.motor_asyncio import AsyncIOMotorClient

# Insert backend directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from config import settings
import database
from cli.paper_reconciliation import (
    build_reconciliation_preview,
    run_reconciliation,
    RECONCILIATION_PRECONDITION_FIELDS,
)
from services.migration_safety import MigrationSafetyError, compute_plan_hash

TEST_DB_NAME = "test_paper_reconciliation_isolated"

async def clean_db():
    client = AsyncIOMotorClient(settings.MONGO_URI)
    db = client[TEST_DB_NAME]
    await db.paper_trades.drop()
    await db.trade_journal.drop()
    return client, db


def test_reconciliation_categories() -> None:
    async def run_async():
        client, db = await clean_db()
        now = datetime.utcnow().isoformat()

        # Helper to run preview on a single trade and verify category
        async def verify_category(trade_doc, expected_category):
            await db.paper_trades.drop()
            await db.paper_trades.insert_one(trade_doc)
            res = await build_reconciliation_preview(db)
            assert res["ok"] is True
            cats = res["categories"]
            assert trade_doc["_id"] in cats[expected_category], f"Expected {trade_doc['_id']} to be in {expected_category}, but got: {cats}"

        # 1. SAFE
        await verify_category({
            "_id": "trade-safe",
            "symbol": "TCS",
            "paper_only": True,
            "status": "ACTIVE",
            "outcome_status": "ACTIVE",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "quantity": 100,
            "quantity_remaining": 100,
            "margin_remaining": 4000.0,
            "open_sl_risk": 1000.0,
            "trade_quality_grade": "A+",
            "updated_at": now,
            "state_version": 1,
        }, "SAFE")

        # 2. CAPITAL_OVERALLOCATED
        await verify_category({
            "_id": "trade-capital-overallocated",
            "symbol": "INFY",
            "paper_only": True,
            "status": "ACTIVE",
            "outcome_status": "ACTIVE",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "quantity": 750,
            "quantity_remaining": 750,
            "margin_remaining": 30000.0,  # exceeds 8% grade limit on 2,50,000 balance
            "open_sl_risk": 7500.0,
            "trade_quality_grade": "A",
            "updated_at": now,
            "state_version": 1,
        }, "CAPITAL_OVERALLOCATED")

        # 3. RISK_OVERALLOCATED
        # We need a trade whose risk exceeds 5% of 250,000 balance (which is 12,500)
        # Margin is 3000 * 10 / 2.5 = 12,000 (well below A+ grade cap of 25,000)
        # Risk is 3000 * (10 - 5) = 15,000 (> 12,500 portfolio limit)
        await verify_category({
            "_id": "trade-risk-overallocated",
            "symbol": "RELIANCE",
            "paper_only": True,
            "status": "ACTIVE",
            "outcome_status": "ACTIVE",
            "entry_price": 10.0,
            "stop_loss": 5.0,
            "quantity": 3000,
            "quantity_remaining": 3000,
            "margin_remaining": 12000.0,
            "open_sl_risk": 15000.0,
            "trade_quality_grade": "A+",
            "updated_at": now,
            "state_version": 1,
        }, "RISK_OVERALLOCATED")

        # 4. NEGATIVE_AVAILABLE_CAPITAL
        await verify_category({
            "_id": "trade-negative-capital",
            "symbol": "SBIN",
            "paper_only": True,
            "status": "ACTIVE",
            "outcome_status": "ACTIVE",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "quantity": 100,
            "quantity_remaining": 100,
            "margin_remaining": -500.0,
            "open_sl_risk": 1000.0,
            "trade_quality_grade": "A+",
            "updated_at": now,
            "state_version": 1,
        }, "NEGATIVE_AVAILABLE_CAPITAL")

        # 5. SMALL_QUANTITY_INVALID_ALLOCATION
        await verify_category({
            "_id": "trade-small-qty",
            "symbol": "HDFC",
            "paper_only": True,
            "status": "ACTIVE",
            "outcome_status": "ACTIVE",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "quantity": 2,
            "quantity_remaining": 2,
            "margin_remaining": 80.0,
            "open_sl_risk": 20.0,
            "trade_quality_grade": "A+",
            "updated_at": now,
            "state_version": 1,
        }, "SMALL_QUANTITY_INVALID_ALLOCATION")

        # 6. RESERVATION_MISSING
        await verify_category({
            "_id": "trade-res-missing",
            "symbol": "ICICI",
            "paper_only": True,
            "status": "ACTIVE",
            "outcome_status": "ACTIVE",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "quantity": 100,
            "quantity_remaining": 100,
            "trade_quality_grade": "A+",
            "updated_at": now,
            "state_version": 1,
        }, "RESERVATION_MISSING")

        # 7. RESERVATION_DUPLICATE
        # For duplicates, we need to insert two trades under the same setup_id
        await db.paper_trades.drop()
        t1 = {
            "_id": "trade-res-dup-1",
            "symbol": "WIPRO",
            "paper_only": True,
            "status": "ACTIVE",
            "outcome_status": "ACTIVE",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "quantity": 100,
            "quantity_remaining": 100,
            "margin_remaining": 4000.0,
            "open_sl_risk": 1000.0,
            "setup_id": "setup-dup-id",
            "trade_quality_grade": "A+",
            "updated_at": now,
            "state_version": 1,
        }
        t2 = {
            "_id": "trade-res-dup-2",
            "symbol": "WIPRO",
            "paper_only": True,
            "status": "ACTIVE",
            "outcome_status": "ACTIVE",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "quantity": 100,
            "quantity_remaining": 100,
            "margin_remaining": 4000.0,
            "open_sl_risk": 1000.0,
            "setup_id": "setup-dup-id",
            "trade_quality_grade": "A+",
            "updated_at": now,
            "state_version": 1,
        }
        await db.paper_trades.insert_many([t1, t2])
        res = await build_reconciliation_preview(db)
        assert "trade-res-dup-2" in res["categories"]["RESERVATION_DUPLICATE"]

        # 8. ACCOUNTING_MISMATCH
        await verify_category({
            "_id": "trade-mismatch",
            "symbol": "AXIS",
            "paper_only": True,
            "status": "EXPIRED",
            "outcome_status": "EXPIRED",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "quantity": 100,
            "quantity_remaining": 100,
            "margin_remaining": 4000.0,
            "open_sl_risk": 1000.0,
            "trade_quality_grade": "A+",
            "updated_at": now,
            "state_version": 1,
        }, "ACCOUNTING_MISMATCH")

        # 9. MANUAL_REVIEW_REQUIRED
        await verify_category({
            "_id": "trade-manual-review",
            "symbol": "LT",
            "paper_only": True,
            "status": "ACTIVE",
            "outcome_status": "ACTIVE",
            "stop_loss": 90.0,
            "quantity": 100,
            "quantity_remaining": 100,
            "margin_remaining": 4000.0,
            "open_sl_risk": 1000.0,
            "trade_quality_grade": "A+",
        }, "MANUAL_REVIEW_REQUIRED")

    orig_balance = settings.STARTING_VIRTUAL_BALANCE
    orig_risk_limit = settings.PORTFOLIO_RISK_LIMIT_PERCENT
    object.__setattr__(settings, "STARTING_VIRTUAL_BALANCE", 250000.0)
    object.__setattr__(settings, "PORTFOLIO_RISK_LIMIT_PERCENT", 5.0)
    try:
        asyncio.run(run_async())
    finally:
        object.__setattr__(settings, "STARTING_VIRTUAL_BALANCE", orig_balance)
        object.__setattr__(settings, "PORTFOLIO_RISK_LIMIT_PERCENT", orig_risk_limit)


def test_reconciliation_dry_run_and_cas() -> None:
    async def run_async():
        client, db = await clean_db()
        now = datetime.utcnow().isoformat()

        # Let's insert a trade that needs a mismatch fix
        trade = {
            "_id": "trade-cas-test",
            "symbol": "TCS",
            "paper_only": True,
            "status": "EXPIRED",
            "outcome_status": "EXPIRED",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "quantity": 100,
            "quantity_remaining": 100,
            "margin_remaining": 4000.0,
            "open_sl_risk": 1000.0,
            "updated_at": now,
            "state_version": 1,
        }
        await db.paper_trades.insert_one(trade)

        # 1. Preview (dry run) mode
        res_preview = await run_reconciliation(db, apply=False)
        assert res_preview["ok"] is True
        assert res_preview["apply"] is False
        assert res_preview["proposed_updates_count"] == 1

        plan = res_preview["plan"]
        assert len(plan["operations"]) == 1
        op = plan["operations"][0]
        assert op["collection"] == "paper_trades"
        assert op["document_id"] == "trade-cas-test"

        # 2. Approved plan with correct SHA-256
        plan_hash = compute_plan_hash(plan)
        approved_plan = {
            "migration_name": plan["migration_name"],
            "target_database": plan["target_database"],
            "plan_hash": plan_hash,
            "operations": plan["operations"],
        }

        # Let's execute apply with a changed database row to test CAS state version mismatch!
        await db.paper_trades.update_one({"_id": "trade-cas-test"}, {"$set": {"state_version": 2, "updated_at": datetime.utcnow().isoformat()}})

        # The CAS check will skip updating the document because query filter fails (state_version changed).
        # This will return result with ok=False and stale_skipped=1.
        res_apply = await run_reconciliation(db, apply=True, plan=approved_plan)
        assert res_apply["ok"] is False
        assert res_apply["stale_skipped"] == 1
        assert res_apply["requires_fresh_preview"] is True

    asyncio.run(run_async())


def test_reconciliation_batch_limits_and_sorting() -> None:
    async def run_async():
        client, db = await clean_db()
        now = datetime.utcnow().isoformat()

        # Insert 5 trades needing mismatch fixes, created at different times
        trades = []
        for i in range(5):
            trades.append({
                "_id": f"trade-{i}",
                "symbol": f"SYM{i}",
                "paper_only": True,
                "status": "EXPIRED",
                "outcome_status": "EXPIRED",
                "entry_price": 100.0,
                "stop_loss": 90.0,
                "quantity": 100,
                "margin_remaining": 10.0,
                "open_sl_risk": 10.0,
                "updated_at": f"2026-06-29T10:00:0{i}Z",
                "state_version": 1,
            })
        await db.paper_trades.insert_many(trades)

        # Test limit batching
        res_limit = await run_reconciliation(db, apply=False, limit=3)
        assert res_limit["scanned"] == 3
        # Since we sort by updated_at desc (-1), the scanned trades should be 4, 3, 2
        categories = res_limit["categories"]
        mismatch_ids = categories["ACCOUNTING_MISMATCH"]
        assert len(mismatch_ids) == 3
        assert "trade-4" in mismatch_ids
        assert "trade-3" in mismatch_ids
        assert "trade-2" in mismatch_ids
        assert "trade-0" not in mismatch_ids

    asyncio.run(run_async())
