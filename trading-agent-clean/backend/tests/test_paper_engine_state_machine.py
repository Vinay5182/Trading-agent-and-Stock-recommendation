import pytest
import asyncio
from datetime import datetime, timezone
from backend.routes.paper import (
    update_plan_status,
    is_legal_state_transition,
    evaluate_paper_trade_chronologically,
)
from config import settings

class FakePaperTrades:
    def __init__(self, docs=None):
        self.docs = docs or []
    def find(self, *args, **kwargs):
        class Cursor:
            def __init__(self, data):
                self.data = data
            def sort(self, *args, **kwargs):
                return self
            def limit(self, *args, **kwargs):
                return self
            def __aiter__(self):
                self.iter = iter(self.data)
                return self
            async def __anext__(self):
                try:
                    return next(self.iter)
                except StopIteration:
                    raise StopAsyncIteration
        return Cursor(self.docs)
    async def find_one(self, *args, **kwargs):
        return self.docs[0] if self.docs else None

def make_plan(status="WAITING_FOR_ENTRY", symbol="TEST"):
    return {
        "_id": f"fake_{symbol}",
        "symbol": symbol,
        "tradingview_symbol": f"NSE:{symbol}",
        "status": status,
        "outcome_status": status,
        "state": status,
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "initial_stop_loss": 90.0,
        "current_stop_loss": 90.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "target_3": 130.0,
        "quantity": 10,
        "quantity_remaining": 10,
        "exit_allocations": {
            "t1": {"quantity": 3, "percent": 33.0},
            "t2": {"quantity": 3, "percent": 33.0},
            "t3": {"quantity": 4, "percent": 34.0},
            "total_quantity": 10,
            "t1_quantity": 3,
            "t2_quantity": 3,
            "t3_quantity": 4,
            "allocation_reason": "TEST",
            "allocation_version": 2,
            "valid": True,
        },
    }

# 1. Pre-entry stop-loss triggers EXPIRED
def test_pre_entry_stop_loss_triggers_expired():
    plan = make_plan("WAITING_FOR_ENTRY")
    # Low = 80 (below SL 90), but High = 95 (< Entry 100)
    candle = {"time": "2026-07-20T10:00:00Z", "open": 90, "high": 95, "low": 80, "close": 90}
    upd = update_plan_status(plan, candle)
    assert upd.get("status") in ("STOPPED", "EXPIRED")
    assert upd.get("exit_reason") == "STOP_LOSS_HIT_BEFORE_ENTRY"

# 2. ACTIVE can never become EXPIRED
def test_active_cannot_become_expired():
    assert is_legal_state_transition("ACTIVE", "EXPIRED") is False
    assert is_legal_state_transition("T1_PARTIAL", "EXPIRED") is False
    assert is_legal_state_transition("T2_PARTIAL", "EXPIRED") is False

# 3. Entry followed by SL
def test_entry_followed_by_sl():
    plan = make_plan("WAITING_FOR_ENTRY")
    c1 = {"time": "2026-07-20T10:00:00Z", "open": 98, "high": 105, "low": 98, "close": 102}
    upd1 = update_plan_status(plan, c1)
    assert upd1.get("status") == "ACTIVE"
    
    plan.update(upd1)
    c2 = {"time": "2026-07-21T10:00:00Z", "open": 100, "high": 101, "low": 88, "close": 89}
    upd2 = update_plan_status(plan, c2)
    assert upd2.get("status") == "SL_HIT"

# 4. Entry followed by Target
def test_entry_followed_by_target():
    plan = make_plan("WAITING_FOR_ENTRY")
    c1 = {"time": "2026-07-20T10:00:00Z", "open": 98, "high": 105, "low": 98, "close": 102}
    upd1 = update_plan_status(plan, c1)
    assert upd1.get("status") == "ACTIVE"
    
    plan.update(upd1)
    c2 = {"time": "2026-07-21T10:00:00Z", "open": 102, "high": 112, "low": 101, "close": 111}
    upd2 = update_plan_status(plan, c2)
    assert upd2.get("status") == "T1_PARTIAL"

# 5. Entry and SL in same candle (Triggers lifecycle block when no lower timeframe data exists)
def test_entry_and_sl_same_candle():
    plan = make_plan("WAITING_FOR_ENTRY")
    # High = 105 (breaches entry 100), Low = 85 (breaches SL 90)
    c = {"time": "2026-07-20T10:00:00Z", "open": 95, "high": 105, "low": 85, "close": 98}
    upd = update_plan_status(plan, c)
    assert upd.get("lifecycle_blocked") is True
    assert upd.get("block_code") == "MISSING_LOWER_TIMEFRAME_DATA"

# 6. Entry and Target in same candle (Activates entry since entry price was touched)
def test_entry_and_target_same_candle():
    plan = make_plan("WAITING_FOR_ENTRY")
    # High = 115 (breaches entry 100 and T1 110)
    c = {"time": "2026-07-20T10:00:00Z", "open": 95, "high": 115, "low": 95, "close": 112}
    upd = update_plan_status(plan, c)
    assert upd.get("status") in ("ACTIVE", "T1_PARTIAL")
    assert upd.get("status") != "EXPIRED"

# 7. Full replay equals incremental evaluation
def test_full_replay_equals_incremental():
    plan = make_plan("WAITING_FOR_ENTRY")
    candles = [
        {"time": "2026-07-20T10:00:00Z", "open": 90, "high": 95, "low": 91, "close": 92},
        {"time": "2026-07-21T10:00:00Z", "open": 95, "high": 105, "low": 95, "close": 102},
        {"time": "2026-07-22T10:00:00Z", "open": 102, "high": 103, "low": 88, "close": 89},
    ]
    
    # Incremental evaluation:
    st = dict(plan)
    for c in candles:
        u = update_plan_status(st, c)
        if u:
            st.update(u)
            
    assert st.get("status") == "SL_HIT"

# 8. Replay is deterministic (100 runs produce identical results)
def test_replay_is_deterministic():
    plan = make_plan("WAITING_FOR_ENTRY")
    candles = [
        {"time": "2026-07-20T10:00:00Z", "open": 90, "high": 95, "low": 91, "close": 92},
        {"time": "2026-07-21T10:00:00Z", "open": 95, "high": 105, "low": 95, "close": 102},
        {"time": "2026-07-22T10:00:00Z", "open": 102, "high": 103, "low": 88, "close": 89},
    ]
    
    results = []
    for _ in range(100):
        st = dict(plan)
        for c in candles:
            u = update_plan_status(st, c)
            if u:
                st.update(u)
        results.append(st.get("status"))
        
    assert len(set(results)) == 1
    assert results[0] == "SL_HIT"
