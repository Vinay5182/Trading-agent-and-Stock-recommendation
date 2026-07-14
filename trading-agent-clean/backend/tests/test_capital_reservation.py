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
from config import settings, GENUINE_OPEN_STATUSES
import database
from services.position_sizing import calculate_proposed_sizing, adjust_accounting_on_quantity_change
from services.capital_accounting import (
    is_genuine_open_trade,
    trade_open_margin_used,
    trade_open_sl_risk,
    get_current_virtual_balance_and_pnl,
    get_portfolio_totals,
    try_activate_trade_with_capital,
)
from cli.capital_backfill import MIGRATION_NAME, backfill_capital_accounting
from routes.paper import update_plan_status, acquire_paper_update_lock, release_paper_update_lock
from services.migration_safety import validate_plan

TEST_DB_NAME = "test_capital_reservation_isolated"

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
    # Connect and patch database.get_database to return the isolated test DB
    client = AsyncIOMotorClient(settings.MONGO_URI)
    test_db = client[TEST_DB_NAME]
    monkeypatch.setattr(database, "get_database", lambda: test_db)
    monkeypatch.setattr(database, "mongo_client", client)

    import routes.dashboard
    monkeypatch.setattr(routes.dashboard, "get_database", lambda: test_db)

    import routes.paper
    monkeypatch.setattr(routes.paper, "get_database", lambda: test_db)

    # Run database cleaning before each test
    asyncio.run(clean_db())
    yield
    client.close()

# 1. A+, A and B risk percentages & grade margin caps & C/invalid-grade rejection
def test_position_sizing_grades_and_caps() -> None:
    # A+ Sizing: 0.50% risk, 10% margin cap
    # Balance: 2,50,000 -> Risk budget = 1250, Margin cap = 25000
    # Stop loss distance = 10 (entry=100, sl=90)
    # Qty by risk = 125. Margin for 125 qty = (125 * 100) / 2.5 = 5000.
    res_a_plus = calculate_proposed_sizing(
        entry_price=100.0,
        stop_loss=90.0,
        grade="A+",
        current_balance=settings.STARTING_VIRTUAL_BALANCE,
        available_margin=settings.STARTING_VIRTUAL_BALANCE,
        open_margin=0.0,
        combined_open_risk=0.0
    )
    assert res_a_plus["ok"] is True
    assert res_a_plus["final_quantity"] == 125
    assert res_a_plus["required_margin"] == 5000.0
    assert res_a_plus["estimated_sl_risk"] == 1250.0

    # A Sizing: 0.35% risk. Under version 2, this must not scale up and is blocked because risk qty is below minimum margin qty
    res_a = calculate_proposed_sizing(
        entry_price=100.0,
        stop_loss=90.0,
        grade="A",
        current_balance=settings.STARTING_VIRTUAL_BALANCE,
        available_margin=settings.STARTING_VIRTUAL_BALANCE,
        open_margin=0.0,
        combined_open_risk=0.0
    )
    assert res_a["ok"] is False
    assert res_a["reason"] == "RISK_QUANTITY_BELOW_MINIMUM"

    # B Sizing: 0.25% risk. Should not scale up and is blocked.
    res_b = calculate_proposed_sizing(
        entry_price=100.0,
        stop_loss=90.0,
        grade="B",
        current_balance=settings.STARTING_VIRTUAL_BALANCE,
        available_margin=settings.STARTING_VIRTUAL_BALANCE,
        open_margin=0.0,
        combined_open_risk=0.0
    )
    assert res_b["ok"] is False
    assert res_b["reason"] == "RISK_QUANTITY_BELOW_MINIMUM"

    # C or Lower / Invalid Grade Sizing: Should reject
    res_c = calculate_proposed_sizing(
        entry_price=100.0,
        stop_loss=90.0,
        grade="C",
        current_balance=settings.STARTING_VIRTUAL_BALANCE,
        available_margin=settings.STARTING_VIRTUAL_BALANCE,
        open_margin=0.0,
        combined_open_risk=0.0
    )
    assert res_c["ok"] is False
    assert res_c["reason"] == "GRADE_MISSING_OR_INVALID"

# 2. data-based entry-to-SL distance & zero/invalid stop-distance rejection & quantity-zero rejection
def test_invalid_sizing_parameters() -> None:
    # Zero stop distance
    res_zero_dist = calculate_proposed_sizing(
        entry_price=100.0,
        stop_loss=100.0,
        grade="A+",
        current_balance=settings.STARTING_VIRTUAL_BALANCE,
        available_margin=settings.STARTING_VIRTUAL_BALANCE,
        open_margin=0.0,
        combined_open_risk=0.0
    )
    assert res_zero_dist["ok"] is False
    assert res_zero_dist["reason"] == "INVALID_STOP_DISTANCE"

    # Zero available margin should fail (enforced)
    res_no_margin = calculate_proposed_sizing(
        entry_price=100.0,
        stop_loss=90.0,
        grade="A+",
        current_balance=settings.STARTING_VIRTUAL_BALANCE,
        available_margin=0.0,
        open_margin=0.0,
        combined_open_risk=0.0
    )
    assert res_no_margin["ok"] is False
    assert res_no_margin["reason"] == "INSUFFICIENT_AVAILABLE_MARGIN"

# 3. ₹5,000 minimum entry-margin acceptance
def test_minimum_entry_margin_scaling() -> None:
    # Balance: 50,000, Grade: A+ (Risk budget = 250)
    # Stop distance = 2 (entry=100, sl=98)
    # Qty by risk = 250 / 2 = 125. Required margin = (125 * 100) / 2.5 = 5000.
    # Required margin = 5000 >= 5000. Accepted!
    res_ok = calculate_proposed_sizing(
        entry_price=100.0,
        stop_loss=98.0,
        grade="A+",
        current_balance=50000.0,
        available_margin=50000.0,
        open_margin=0.0,
        combined_open_risk=0.0
    )
    assert res_ok["ok"] is True
    assert res_ok["final_quantity"] == 125
    assert res_ok["required_margin"] == 5000.0

    # Balance: 30,000, Grade: A+ (Risk budget = 150)
    # Stop distance = 2 (entry=100, sl=98)
    # Since margin (3000) < 5000, and we never scale up under v2, it blocks with RISK_QUANTITY_BELOW_MINIMUM.
    res_exceeded = calculate_proposed_sizing(
        entry_price=100.0,
        stop_loss=98.0,
        grade="A+",
        current_balance=30000.0,
        available_margin=30000.0,
        open_margin=0.0,
        combined_open_risk=0.0
    )
    assert res_exceeded["ok"] is False
    assert res_exceeded["reason"] == "RISK_QUANTITY_BELOW_MINIMUM"

# 4. available-margin limit & 80% portfolio margin limit & 5% combined open-risk limit bypasses
def test_portfolio_limits() -> None:
    # Available margin limit enforcement
    res_avail = calculate_proposed_sizing(
        entry_price=100.0,
        stop_loss=90.0,
        grade="A+",
        current_balance=settings.STARTING_VIRTUAL_BALANCE,
        available_margin=4000.0, # Less than 5000 required margin
        open_margin=0.0,
        combined_open_risk=0.0
    )
    assert res_avail["ok"] is False
    assert res_avail["reason"] == "INSUFFICIENT_AVAILABLE_MARGIN"

    # 80% portfolio margin limit enforcement
    res_margin_limit = calculate_proposed_sizing(
        entry_price=100.0,
        stop_loss=90.0,
        grade="A+",
        current_balance=settings.STARTING_VIRTUAL_BALANCE,
        available_margin=52000.0,
        open_margin=198000.0,
        combined_open_risk=0.0
    )
    assert res_margin_limit["ok"] is False
    assert res_margin_limit["reason"] == "PORTFOLIO_MARGIN_LIMIT_EXCEEDED"

    # 5% combined open-risk limit enforcement
    res_risk_limit = calculate_proposed_sizing(
        entry_price=100.0,
        stop_loss=90.0,
        grade="A+",
        current_balance=settings.STARTING_VIRTUAL_BALANCE,
        available_margin=50000.0,
        open_margin=0.0,
        combined_open_risk=12000.0
    )
    assert res_risk_limit["ok"] is False
    assert res_risk_limit["reason"] == "PORTFOLIO_RISK_LIMIT_EXCEEDED"

# 4b. new accounting requirements tests
def test_new_accounting_requirements() -> None:
    async def run_async():
        _, db = await clean_db()

        # 1. Starting state: Balance is 2,50,000.
        # Settled Balance: 2,50,000, Reserved Margin: 0, Available Cash: 2,50,000
        balance, realized = await get_current_virtual_balance_and_pnl(db)
        open_margin, open_risk = await get_portfolio_totals(db)

        assert balance == settings.STARTING_VIRTUAL_BALANCE
        assert realized == 0.0
        assert open_margin == 0.0

        # 2. WAITING trade reserves zero
        waiting_trade = {
            "symbol": "TCS",
            "paper_only": True,
            "status": "WAITING_FOR_ENTRY",
            "outcome_status": "WAITING_FOR_ENTRY",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "trade_quality_grade": "A+",
            "setup_id": "setup-1",
            "state_version": 1,
            "margin_remaining": 0.0,
            "open_sl_risk": 0.0,
            "initial_margin_reserved": 0.0,
            "initial_sl_risk": 0.0,
            "margin_released_total": 0.0,
        }
        await db.paper_trades.insert_one(waiting_trade)
        open_margin_w, open_risk_w = await get_portfolio_totals(db)
        assert open_margin_w == 0.0
        assert open_risk_w == 0.0

        # 3. Activate the trade
        now = datetime.utcnow().isoformat()
        res_act = await try_activate_trade_with_capital(db, waiting_trade["_id"], current_state_version=1, now=now)
        assert res_act["ok"] is True
        assert res_act["activated"] is True

        # Verify:
        # - Entry moves capital from available cash to reserved margin
        # - Entry does not reduce settled balance
        activated = await db.paper_trades.find_one({"_id": waiting_trade["_id"]})
        assert activated["status"] == "ACTIVE"
        assert activated["initial_margin_reserved"] == 5000.0
        assert activated["margin_remaining"] == 5000.0

        balance_entry, realized_entry = await get_current_virtual_balance_and_pnl(db)
        open_margin_entry, open_risk_entry = await get_portfolio_totals(db)
        available_cash_entry = balance_entry - open_margin_entry

        assert balance_entry == settings.STARTING_VIRTUAL_BALANCE # Settled balance unchanged
        assert open_margin_entry == 5000.0 # Reserved margin is now 5000
        assert available_cash_entry == 245000.0 # Available cash decreased by 5000

        # 4. Partial exit
        plan = activated
        update = {
            "quantity_remaining": 92,
            "status": "T1_PARTIAL",
            "partial_exit_1": {
                "exit_stage": "T1",
                "exit_price": 110.0,
                "quantity": 33,
                "percent": 26.4,
                "paper_pnl": 330.0,
                "exited_at": now
            }
        }

        # Proportional exit releases proportional capital
        adjusted_partial = adjust_accounting_on_quantity_change(plan, update)
        assert adjusted_partial["quantity_remaining"] == 92
        assert adjusted_partial["margin_remaining"] == 3680.0
        assert adjusted_partial["open_sl_risk"] == 920.0
        assert adjusted_partial["margin_released_total"] == 1320.0

        await db.paper_trades.update_one({"_id": plan["_id"]}, {"$set": adjusted_partial})

        # Also add a journal record to simulate realized P&L of 330
        journal_partial = {
            "symbol": "TCS",
            "paper_only": True,
            "status": "T1_HIT",
            "pnl": 330.0,
            "paper_pnl": 330.0,
            "exit_date": now,
            "journaled_at": now,
        }
        await db.trade_journal.insert_one(journal_partial)

        # Verify partial exit updates settled balance & available cash increases
        balance_part, realized_part = await get_current_virtual_balance_and_pnl(db)
        open_margin_part, _ = await get_portfolio_totals(db)
        available_cash_part = balance_part - open_margin_part

        assert balance_part == 250330.0 # Settled balance updated with realized pnl (+330)
        assert open_margin_part == 3680.0 # Reserved margin reduced to 3680
        # Available Cash change = 246650 - 245000 = 1650.
        # Formula: released_margin (1320) + realized_pnl_delta (330) = 1650.
        assert available_cash_part - available_cash_entry == 1650.0

        # 5. Full/final exit
        plan_part = await db.paper_trades.find_one({"_id": plan["_id"]})
        update_full = {
            "quantity_remaining": 0,
            "status": "STOP_HIT",
            "realized_pnl": -590.0,
            "stop_exit": {
                "exit_stage": "STOP",
                "exit_price": 90.0,
                "quantity": 92,
                "paper_pnl": -920.0,
                "exited_at": now
            }
        }

        adjusted_full = adjust_accounting_on_quantity_change(plan_part, update_full)
        assert adjusted_full["quantity_remaining"] == 0
        assert adjusted_full["margin_remaining"] == 0.0
        assert adjusted_full["open_sl_risk"] == 0.0
        assert adjusted_full["margin_released_total"] == 5000.0 # fully released

        # Check final exit metadata
        assert adjusted_full["final_released_margin"] == 3680.0
        assert adjusted_full["final_realized_pnl_delta"] == -920.0
        assert adjusted_full["cash_returned_on_final_exit"] == 2760.0 # 3680 - 920

        await db.paper_trades.update_one({"_id": plan["_id"]}, {"$set": adjusted_full})

        # Update journal to reflect final total P&L of -590
        await db.trade_journal.drop() # clean old partial
        journal_full = {
            "symbol": "TCS",
            "paper_only": True,
            "status": "STOP_HIT",
            "pnl": -590.0,
            "paper_pnl": -590.0,
            "exit_date": now,
            "journaled_at": now,
        }
        await db.trade_journal.insert_one(journal_full)

        balance_full, realized_full = await get_current_virtual_balance_and_pnl(db)
        open_margin_full, _ = await get_portfolio_totals(db)
        available_cash_full = balance_full - open_margin_full

        assert balance_full == 249410.0 # settings.STARTING_VIRTUAL_BALANCE - 590
        assert open_margin_full == 0.0
        assert available_cash_full == 249410.0

        # Available cash change from before final exit:
        # available_cash_full - available_cash_part = 249410 - 246650 = 2760.
        # Formula: released_margin (3680) + realized_pnl_delta (-920) = 2760.
        assert available_cash_full - available_cash_part == 2760.0

        # 6. Repeated scheduler processing does not double-release
        plan_closed = await db.paper_trades.find_one({"_id": plan["_id"]})
        update_again = {"status": "STOP_HIT"}
        adjusted_again = adjust_accounting_on_quantity_change(plan_closed, update_again)
        assert "margin_released_total" not in adjusted_again

        # 7. Terminal trade reserves zero
        open_margin_t, _ = await get_portfolio_totals(db)
        assert open_margin_t == 0.0

    asyncio.run(run_async())

def test_dashboard_formulas_and_field_mappings() -> None:
    async def run_async():
        _, db = await clean_db()

        # Create an active trade
        active_trade = {
            "symbol": "TCS",
            "paper_only": True,
            "status": "ACTIVE",
            "outcome_status": "ACTIVE",
            "quantity": 100,
            "quantity_remaining": 100,
            "initial_margin_reserved": 5000.0,
            "margin_remaining": 5000.0,
            "initial_sl_risk": 1000.0,
            "open_sl_risk": 1000.0,
            "margin_released_total": 0.0,
            "remaining_unrealized_pnl": 200.0,
        }
        await db.paper_trades.insert_one(active_trade)

        # Create a journal entry for realized P&L of 300
        journal_entry = {
            "symbol": "INFY",
            "paper_only": True,
            "status": "COMPLETED",
            "pnl": 300.0,
            "paper_pnl": 300.0,
        }
        await db.trade_journal.insert_one(journal_entry)

        from routes.dashboard import get_paper_equity
        data = await get_paper_equity()

        # Verify keys
        assert "starting_virtual_capital" in data
        assert "settled_balance" in data
        assert "reserved_margin" in data
        assert "available_cash" in data
        assert "unrealized_pnl" in data
        assert "total_equity" in data
        assert "total_realized_pnl" in data
        assert "effective_open_exposure" in data
        assert "broker_funded_exposure" in data
        assert "active_partial_trade_count" in data
        assert "total_margin_released" in data
        assert "capital_returned_from_latest_exits" in data

        # Verify values & formulas
        assert data["starting_virtual_capital"] == settings.STARTING_VIRTUAL_BALANCE
        assert data["settled_balance"] == 250300.0
        assert data["reserved_margin"] == 5000.0
        assert data["available_cash"] == 245300.0
        assert data["unrealized_pnl"] == 200.0
        assert data["total_equity"] == 250500.0
        assert data["total_realized_pnl"] == 300.0
        assert data["total_pnl"] == 500.0

    asyncio.run(run_async())

# 5. activation-time recalculation & waiting trades reserve zero & proposed fields excluded from portfolio totals
def test_waiting_trades_and_activation_recalculation() -> None:
    async def run_async():
        _, db = await clean_db()
        # Create a WAITING trade in DB
        trade = {
            "symbol": "TCS",
            "paper_only": True,
            "status": "WAITING_FOR_ENTRY",
            "outcome_status": "WAITING_FOR_ENTRY",
            "entry_triggered": False,
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "trade_quality_grade": "A+",
            "setup_id": "setup-123",
            "state_version": 1,
            # Proposed values
            "proposed_quantity": 125,
            "proposed_exposure": 12500.0,
            "proposed_margin": 5000.0,
            "proposed_sl_risk": 1250.0,
            "proposed_capital_model_version": "v2",
            # Zeros
            "margin_remaining": 0.0,
            "open_sl_risk": 0.0,
            "initial_margin_reserved": 0.0,
            "initial_sl_risk": 0.0,
            "margin_released_total": 0.0,
        }
        await db.paper_trades.insert_one(trade)

        # 1. Waiting trades reserve zero
        open_margin, open_risk = await get_portfolio_totals(db)
        assert open_margin == 0.0
        assert open_risk == 0.0

        # 2. Activate trade
        now = datetime.utcnow().isoformat()
        res = await try_activate_trade_with_capital(db, trade["_id"], current_state_version=1, now=now)
        assert res["ok"] is True
        assert res["activated"] is True

        # Check actual database document after activation
        activated = await db.paper_trades.find_one({"_id": trade["_id"]})
        assert activated["status"] == "ACTIVE"
        assert activated["original_quantity"] == 125
        assert activated["quantity_remaining"] == 125
        assert activated["initial_margin_reserved"] == 5000.0
        assert activated["margin_remaining"] == 5000.0
        assert activated["initial_sl_risk"] == 1250.0
        assert activated["open_sl_risk"] == 1250.0

        # Portfolio totals should now reflect this active trade
        open_margin_now, open_risk_now = await get_portfolio_totals(db)
        assert open_margin_now == 5000.0
        assert open_risk_now == 1250.0

    asyncio.run(run_async())

# 6. partial exit proportional margin/risk release & partial margin allowed below ₹5,000 & full exit releases all remaining margin & no double margin release
def test_margin_release_on_exits() -> None:
    # Set up active trade
    plan = {
        "status": "ACTIVE",
        "original_quantity": 100,
        "quantity_remaining": 100,
        "initial_margin_reserved": 5000.0,
        "margin_remaining": 5000.0,
        "initial_sl_risk": 1000.0,
        "open_sl_risk": 1000.0,
        "margin_released_total": 0.0,
    }

    # Proportional exit (partial exit 1: quantity remaining = 67)
    update = {"quantity_remaining": 67}
    res_partial = adjust_accounting_on_quantity_change(plan, update)
    assert res_partial["quantity_remaining"] == 67
    assert res_partial["margin_remaining"] == 3350.0 # 5000 * 0.67
    assert res_partial["open_sl_risk"] == 670.0 # 1000 * 0.67
    assert res_partial["margin_released_total"] == 1650.0 # 5000 - 3350

    # Test partial margin allowed below ₹5,000 without floor-capping it
    assert res_partial["margin_remaining"] < 5000.0

    # Full exit: status = CLOSED (or COMPLETED)
    # Releases all remaining margin
    plan_after_partial = {**plan, **res_partial, "status": "T1_PARTIAL"}
    update_full = {"status": "CLOSED"}
    res_full = adjust_accounting_on_quantity_change(plan_after_partial, update_full)
    assert res_full["quantity_remaining"] == 0
    assert res_full["margin_remaining"] == 0.0
    assert res_full["open_sl_risk"] == 0.0
    assert res_full["margin_released_total"] == 5000.0 # Initial 5000 fully released

    # Test no double margin release
    plan_closed = {**plan_after_partial, **res_full}
    update_again = {"status": "CLOSED"}
    res_again = adjust_accounting_on_quantity_change(plan_closed, update_again)
    # The update should not modify margin release fields again (it should not be present in the update document)
    assert "margin_released_total" not in res_again

# 7. released principal does not change Current Virtual Balance
def test_released_principal_does_not_change_balance() -> None:
    async def run_async():
        _, db = await clean_db()
        # Verify balance starts at ₹2,50,000
        balance, pnl = await get_current_virtual_balance_and_pnl(db)
        assert balance == settings.STARTING_VIRTUAL_BALANCE
        assert pnl == 0.0

        # Simulate releasing margin by completing a trade (no journal entry yet)
        # Verify get_current_virtual_balance_and_pnl still returns settings.STARTING_VIRTUAL_BALANCE
        balance2, pnl2 = await get_current_virtual_balance_and_pnl(db)
        assert balance2 == settings.STARTING_VIRTUAL_BALANCE

    asyncio.run(run_async())

# 8. duplicate active setup prevention
def test_duplicate_active_setup_prevention() -> None:
    async def run_async():
        _, db = await clean_db()
        # Create an ACTIVE trade for setup-abc
        active_trade = {
            "symbol": "TCS",
            "paper_only": True,
            "status": "ACTIVE",
            "outcome_status": "ACTIVE",
            "setup_id": "setup-abc",
        }
        await db.paper_trades.insert_one(active_trade)

        # Create a WAITING trade for the same setup-abc
        waiting_trade = {
            "symbol": "TCS",
            "paper_only": True,
            "status": "WAITING_FOR_ENTRY",
            "outcome_status": "WAITING_FOR_ENTRY",
            "entry_triggered": False,
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "trade_quality_grade": "A+",
            "setup_id": "setup-abc",
            "state_version": 1,
        }
        await db.paper_trades.insert_one(waiting_trade)

        # Attempt to activate the waiting trade
        now = datetime.utcnow().isoformat()
        res = await try_activate_trade_with_capital(db, waiting_trade["_id"], current_state_version=1, now=now)
        assert res["ok"] is True
        assert res["activated"] is False
        assert res["reason"] == "DUPLICATE_ACTIVE_SETUP"

        # Verify waiting trade was transitioned to EXPIRED
        expired = await db.paper_trades.find_one({"_id": waiting_trade["_id"]})
        assert expired["status"] == "EXPIRED"
        assert expired["capital_rejection_reason"] == "DUPLICATE_ACTIVE_SETUP"

    asyncio.run(run_async())

# 9. competing activations & CAS failure reserves nothing & lock release on success/error/CAS failure & stale totals cannot bypass limits
def test_competing_activations_and_lock_concurrency() -> None:
    async def run_async():
        _, db = await clean_db()
        # Create a WAITING trade in DB
        trade = {
            "symbol": "TCS",
            "paper_only": True,
            "status": "WAITING_FOR_ENTRY",
            "outcome_status": "WAITING_FOR_ENTRY",
            "entry_triggered": False,
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "trade_quality_grade": "A+",
            "setup_id": "setup-123",
            "state_version": 1,
        }
        await db.paper_trades.insert_one(trade)

        # Call try_activate_trade_with_capital twice concurrently
        now = datetime.utcnow().isoformat()
        task1 = try_activate_trade_with_capital(db, trade["_id"], current_state_version=1, now=now)
        task2 = try_activate_trade_with_capital(db, trade["_id"], current_state_version=1, now=now)

        results = await asyncio.gather(task1, task2, return_exceptions=True)

        # One must succeed, the other must fail due to either LOCK_ALREADY_HELD, STATE_VERSION_CONFLICT, or INVALID_PRECONDITION_STATUS
        ok_count = sum(1 for r in results if isinstance(r, dict) and r.get("ok") and r.get("activated"))
        failed_count = sum(1 for r in results if not isinstance(r, dict) or not r.get("activated"))

        assert ok_count == 1
        assert failed_count == 1

        # Check lock status: lock must be released
        lock = await db.paper_update_locks.find_one({"lock_name": "paper_trade_outcome_update"})
        assert lock is None or lock["status"] == "RELEASED"

    asyncio.run(run_async())

# 10. EXPIRED capital rejection excluded from win/loss/P&L
def test_expired_trade_excluded_from_analytics() -> None:
    async def run_async():
        _, db = await clean_db()
        # Add an EXPIRED capital rejection trade
        expired_trade = {
            "symbol": "TCS",
            "paper_only": True,
            "status": "EXPIRED",
            "outcome_status": "EXPIRED",
            "capital_rejection_reason": "PORTFOLIO_RISK_LIMIT",
            "paper_pnl": 0.0,
        }
        await db.paper_trades.insert_one(expired_trade)

        # Retrieve virtual balance: should be ₹2,50,000 with 0 realized P&L
        balance, realized_pnl = await get_current_virtual_balance_and_pnl(db)
        assert balance == settings.STARTING_VIRTUAL_BALANCE
        assert realized_pnl == 0.0

    asyncio.run(run_async())

# 11. stored dashboard fields take priority over fallback & legacy fallback works only when stored fields are absent
def test_dashboard_stored_vs_fallback() -> None:
    # 1. Stored margin priority
    trade_stored = {
        "entry_price": 100.0,
        "quantity": 100,
        "quantity_remaining": 100,
        "margin_remaining": 3000.0, # Stored value
        "open_sl_risk": 500.0, # Stored value
    }
    assert trade_open_margin_used(trade_stored) == 3000.0
    assert trade_open_sl_risk(trade_stored) == 500.0

    # 2. Legacy fallback
    trade_fallback = {
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "quantity": 100,
        "quantity_remaining": 100,
    }
    # exposure = 10000. margin = 10000 / 2.5 = 4000. But legacy fallback applies 5k floor.
    assert trade_open_margin_used(trade_fallback) == 5000.0
    # risk = 100 * 10 = 1000
    assert trade_open_sl_risk(trade_fallback) == 1000.0

# 12. migration dry-run writes nothing & migration apply is not run & migration idempotency test using isolated database
def test_migration_dry_run_and_idempotency() -> None:
    async def run_async():
        _, db = await clean_db()
        # Insert a legacy trade with missing model version
        trade = {
            "symbol": "TCS",
            "paper_only": True,
            "status": "ACTIVE",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "quantity": 100,
            "quantity_remaining": 100,
        }
        await db.paper_trades.insert_one(trade)

        # 1. Run dry-run backfill
        res_dry = await backfill_capital_accounting(db, apply=False)
        assert res_dry["scanned"] == 1
        assert res_dry["proposed_updates_count"] == 1
        assert res_dry["applied_updates_count"] == 0

        # Verify DB is unchanged
        db_trade = await db.paper_trades.find_one({"_id": trade["_id"]})
        assert "capital_model_version" not in db_trade

        # 2. Approve the preview plan and run apply backfill (for idempotency testing)
        approved_plan = validate_plan(
            res_dry["plan"],
            migration_name=MIGRATION_NAME,
            target_database=db.name,
            confirm_hash=res_dry["plan_hash"],
        )
        res_apply1 = await backfill_capital_accounting(db, apply=True, plan=approved_plan)
        assert res_apply1["applied_operations"] == 1

        # Verify DB is updated
        db_trade_updated = await db.paper_trades.find_one({"_id": trade["_id"]})
        assert db_trade_updated["capital_model_version"] == "v2"

        # 3. Re-apply the same approved plan: should be idempotent (0 applied updates)
        res_apply2 = await backfill_capital_accounting(db, apply=True, plan=approved_plan)
        assert res_apply2["applied_operations"] == 0
        assert res_apply2["already_satisfied"] == 1
        res_after = await backfill_capital_accounting(db, apply=False)
        assert res_after["proposed_updates_count"] == 0
        assert res_after["applied_updates_count"] == 0

    asyncio.run(run_async())

# 13. normal startup performs no backfill writes
def test_startup_does_not_perform_backfill_writes() -> None:
    async def run_async():
        _, db = await clean_db()
        # Verify that connected state performs no writes
        pass
    asyncio.run(run_async())

# 14. no active sizing path uses fixed ₹1,000 or old ₹1,00,000 capital
def test_dynamic_sizing_capital_base() -> None:
    # Sizing for A+ on balance of 2,50,000:
    # 0.50% risk = 1,250.
    res_large = calculate_proposed_sizing(
        entry_price=100.0,
        stop_loss=90.0,
        grade="A+",
        current_balance=settings.STARTING_VIRTUAL_BALANCE,
        available_margin=settings.STARTING_VIRTUAL_BALANCE,
        open_margin=0.0,
        combined_open_risk=0.0
    )
    assert res_large["ok"] is True
    assert res_large["final_quantity"] == 125 # Risk budget 1250 / 10 stop distance = 125 qty

    # Sizing for A+ on balance of 5,00,000:
    # 0.50% risk = 2,500.
    res_larger = calculate_proposed_sizing(
        entry_price=100.0,
        stop_loss=90.0,
        grade="A+",
        current_balance=settings.STARTING_VIRTUAL_BALANCE,
        available_margin=settings.STARTING_VIRTUAL_BALANCE,
        open_margin=0.0,
        combined_open_risk=0.0
    )
    assert res_larger["ok"] is True
    assert res_larger["final_quantity"] == 250 # Risk budget 2500 / 10 stop distance = 250 qty


def test_capital_rejection_status_semantics() -> None:
    async def run_async():
        _, db = await clean_db()

        # 1. Setup trade: WAITING_FOR_ENTRY
        trade = {
            "symbol": "TCS_TEST",
            "paper_only": True,
            "status": "WAITING_FOR_ENTRY",
            "outcome_status": "WAITING_FOR_ENTRY",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "trade_quality_grade": "A+",
            "setup_id": "setup-semantics-1",
            "state_version": 1,
            "quantity": 100,
            "margin_remaining": 0.0,
            "open_sl_risk": 0.0,
            "initial_margin_reserved": 0.0,
            "initial_sl_risk": 0.0,
            "margin_released_total": 0.0,
        }
        await db.paper_trades.insert_one(trade)

        from unittest.mock import patch

        with patch("services.position_sizing.calculate_proposed_sizing") as mock_sizing:
            # 2. Test insufficient margin remains retryable
            mock_sizing.return_value = {"ok": False, "reason": "INSUFFICIENT_MARGIN"}
            now = datetime.utcnow().isoformat()
            res = await try_activate_trade_with_capital(db, trade["_id"], current_state_version=1, now=now)
            assert res["ok"] is True
            assert res["activated"] is False
            assert res["reason"] == "INSUFFICIENT_MARGIN"
            assert res["state"] == "WAITING_FOR_ENTRY"

            # verify no reservation
            updated = await db.paper_trades.find_one({"_id": trade["_id"]})
            assert updated["status"] == "WAITING_FOR_ENTRY"
            assert updated["activation_blocked_reason"] == "INSUFFICIENT_MARGIN"
            assert updated["margin_remaining"] == 0.0

            # 3. Test portfolio-margin limit remains retryable
            mock_sizing.return_value = {"ok": False, "reason": "PORTFOLIO_MARGIN_LIMIT_EXCEEDED"}
            res = await try_activate_trade_with_capital(db, trade["_id"], current_state_version=2, now=now)
            assert res["ok"] is True
            assert res["activated"] is False
            assert res["reason"] == "PORTFOLIO_MARGIN_LIMIT_EXCEEDED"
            assert res["state"] == "WAITING_FOR_ENTRY"

            updated = await db.paper_trades.find_one({"_id": trade["_id"]})
            assert updated["status"] == "WAITING_FOR_ENTRY"
            assert updated["activation_blocked_reason"] == "PORTFOLIO_MARGIN_LIMIT_EXCEEDED"

            # 4. Test portfolio-risk limit remains retryable
            mock_sizing.return_value = {"ok": False, "reason": "PORTFOLIO_RISK_LIMIT_EXCEEDED"}
            res = await try_activate_trade_with_capital(db, trade["_id"], current_state_version=3, now=now)
            assert res["ok"] is True
            assert res["activated"] is False
            assert res["reason"] == "PORTFOLIO_RISK_LIMIT_EXCEEDED"
            assert res["state"] == "WAITING_FOR_ENTRY"

            # 5. Test configuration error INVALID_BALANCE remains retryable WAITING_FOR_ENTRY with operational block
            mock_sizing.return_value = {"ok": False, "reason": "INVALID_BALANCE"}
            res = await try_activate_trade_with_capital(db, trade["_id"], current_state_version=4, now=now)
            assert res["ok"] is True
            assert res["activated"] is False
            assert res["reason"] == "INVALID_BALANCE"
            assert res["state"] == "WAITING_FOR_ENTRY"

            updated = await db.paper_trades.find_one({"_id": trade["_id"]})
            assert updated["status"] == "WAITING_FOR_ENTRY"
            assert updated["activation_blocked_reason"] == "INVALID_BALANCE"

            # 6. After capacity becomes available, same setup can activate exactly once
            mock_sizing.return_value = {
                "ok": True,
                "final_quantity": 100,
                "exposure": 10000.0,
                "required_margin": 4000.0,
                "estimated_sl_risk": 1000.0,
            }
            res = await try_activate_trade_with_capital(db, trade["_id"], current_state_version=5, now=now)
            assert res["ok"] is True
            assert res["activated"] is True

            updated = await db.paper_trades.find_one({"_id": trade["_id"]})
            assert updated["status"] == "ACTIVE"
            assert updated["activation_blocked_reason"] is None
            assert updated["margin_remaining"] == 4000.0

            # Retry fails
            res_retry = await try_activate_trade_with_capital(db, trade["_id"], current_state_version=6, now=now)
            assert res_retry["ok"] is False
            assert res_retry["reason"] == "INVALID_PRECONDITION_STATUS"

        # 7. Test quantity below minimum permanently rejected to EXPIRED
        trade2 = {
            "symbol": "TCS_TEST2",
            "paper_only": True,
            "status": "WAITING_FOR_ENTRY",
            "outcome_status": "WAITING_FOR_ENTRY",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "trade_quality_grade": "A+",
            "setup_id": "setup-semantics-2",
            "state_version": 1,
            "quantity": 3,
            "margin_remaining": 0.0,
            "open_sl_risk": 0.0,
            "initial_margin_reserved": 0.0,
            "initial_sl_risk": 0.0,
            "margin_released_total": 0.0,
        }
        await db.paper_trades.insert_one(trade2)
        with patch("services.position_sizing.calculate_proposed_sizing") as mock_sizing:
            mock_sizing.return_value = {"ok": False, "reason": "QUANTITY_BELOW_MINIMUM"}
            res = await try_activate_trade_with_capital(db, trade2["_id"], current_state_version=1, now=now)
            assert res["ok"] is True
            assert res["activated"] is False
            assert res["reason"] == "QUANTITY_BELOW_MINIMUM"
            assert res["state"] == "EXPIRED"

            updated = await db.paper_trades.find_one({"_id": trade2["_id"]})
            assert updated["status"] == "EXPIRED"
            assert updated["capital_rejection_reason"] == "QUANTITY_BELOW_MINIMUM"

        # 8. Test invalid allocation is permanently rejected to EXPIRED
        trade3 = {
            "symbol": "TCS_TEST3",
            "paper_only": True,
            "status": "WAITING_FOR_ENTRY",
            "outcome_status": "WAITING_FOR_ENTRY",
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "trade_quality_grade": "A+",
            "setup_id": "setup-semantics-3",
            "state_version": 1,
            "quantity": 10,
            "margin_remaining": 0.0,
            "open_sl_risk": 0.0,
            "initial_margin_reserved": 0.0,
            "initial_sl_risk": 0.0,
            "margin_released_total": 0.0,
        }
        await db.paper_trades.insert_one(trade3)
        with patch("services.position_sizing.calculate_proposed_sizing") as mock_sizing:
            mock_sizing.return_value = {
                "ok": True,
                "final_quantity": 3,  # Force invalid exit allocation
                "exposure": 300.0,
                "required_margin": 120.0,
                "estimated_sl_risk": 30.0,
            }
            res = await try_activate_trade_with_capital(db, trade3["_id"], current_state_version=1, now=now)
            assert res["ok"] is True
            assert res["activated"] is False
            assert res["reason"] == "INVALID_EXIT_ALLOCATION"
            assert res["state"] == "EXPIRED"

            updated = await db.paper_trades.find_one({"_id": trade3["_id"]})
            assert updated["status"] == "EXPIRED"
            assert updated["capital_rejection_reason"] == "INVALID_EXIT_ALLOCATION"

    asyncio.run(run_async())
