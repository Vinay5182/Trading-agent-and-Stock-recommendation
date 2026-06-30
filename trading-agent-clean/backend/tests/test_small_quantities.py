from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

import pytest
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from config import settings
import database
from services.capital_accounting import (
    try_activate_trade_with_capital,
    get_current_virtual_balance_and_pnl,
    get_portfolio_totals,
)

TEST_DB_NAME = "test_small_quantities_isolated"

async def clean_db():
    client = AsyncIOMotorClient(settings.MONGO_URI)
    db = client[TEST_DB_NAME]
    await db.paper_trades.drop()
    await db.trade_journal.drop()
    await db.paper_update_locks.drop()
    await db.paper_update_runs.drop()
    return client, db

@pytest.fixture(autouse=True)
def setup_monkeypatch(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGO_URI)
    test_db = client[TEST_DB_NAME]
    monkeypatch.setattr(database, "get_database", lambda: test_db)
    monkeypatch.setattr(database, "mongo_client", client)

    import routes.paper
    monkeypatch.setattr(routes.paper, "get_database", lambda: test_db)

    asyncio.run(clean_db())
    yield
    client.close()

def test_activation_rejection_quantities_one_to_three() -> None:
    async def run_async():
        _, db = await clean_db()

        # Test for each quantity from 1 to 3
        for qty in (1, 2, 3):
            trade = {
                "symbol": f"SYM_{qty}",
                "paper_only": True,
                "status": "WAITING_FOR_ENTRY",
                "outcome_status": "WAITING_FOR_ENTRY",
                "entry_price": 100.0,
                "stop_loss": 90.0,
                "trade_quality_grade": "A+",
                "setup_id": f"setup-qty-{qty}",
                "state_version": 1,
                "quantity": qty,
                "margin_remaining": 0.0,
                "open_sl_risk": 0.0,
                "initial_margin_reserved": 0.0,
                "initial_sl_risk": 0.0,
                "margin_released_total": 0.0,
            }
            await db.paper_trades.insert_one(trade)

            now = datetime.utcnow().isoformat()
            res = await try_activate_trade_with_capital(db, trade["_id"], current_state_version=1, now=now)

            # Sizing must fail and trade must be transitioned to EXPIRED
            assert res["ok"] is True
            assert res["activated"] is False
            assert res["reason"] == "QUANTITY_BELOW_MINIMUM"

            updated_trade = await db.paper_trades.find_one({"_id": trade["_id"]})
            assert updated_trade["status"] == "EXPIRED"
            assert updated_trade["capital_rejection_reason"] == "QUANTITY_BELOW_MINIMUM"

    asyncio.run(run_async())

def test_activation_acceptance_quantity_four() -> None:
    async def run_async():
        _, db = await clean_db()

        # Quantity 4 is the minimum valid quantity for exit allocation
        trade = {
            "symbol": "SYM_4",
            "paper_only": True,
            "status": "WAITING_FOR_ENTRY",
            "outcome_status": "WAITING_FOR_ENTRY",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "trade_quality_grade": "A+",
            "setup_id": "setup-qty-4",
            "state_version": 1,
            "quantity": 4,
            "margin_remaining": 0.0,
            "open_sl_risk": 0.0,
            "initial_margin_reserved": 0.0,
            "initial_sl_risk": 0.0,
            "margin_released_total": 0.0,
        }
        await db.paper_trades.insert_one(trade)

        now = datetime.utcnow().isoformat()
        res = await try_activate_trade_with_capital(db, trade["_id"], current_state_version=1, now=now)

        assert res["ok"] is True
        assert res["activated"] is True

        updated_trade = await db.paper_trades.find_one({"_id": trade["_id"]})
        assert updated_trade["status"] == "ACTIVE"
        assert updated_trade["exit_allocations"]["valid"] is True
        assert updated_trade["exit_allocations"]["t1_quantity"] == 1
        assert updated_trade["exit_allocations"]["t2_quantity"] == 1
        assert updated_trade["exit_allocations"]["t3_quantity"] == 2

    asyncio.run(run_async())
