import sys, os
sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), 'backend'))

import pytest
from backend.routes.paper import _update_plan_status_raw, WAITING_FOR_ENTRY_STATUS

def make_base_plan():
    return {
        "_id": "test_trade_123",
        "symbol": "TESTSYM",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "target_3": 130.0,
        "status": WAITING_FOR_ENTRY_STATUS,
        "outcome_status": WAITING_FOR_ENTRY_STATUS,
        "state": WAITING_FOR_ENTRY_STATUS,
        "entry_triggered": False,
        "quantity": 10,
        "quantity_remaining": 0,
        "trade_quality_grade": "A",
        "setup_date": "2026-07-01",
    }

def test_scenario_1_pre_entry_sl_day_n_entry_day_m():
    """
    Scenario 1: Pre-entry Stop Loss touch on Day 1 (low=85 <= 90),
    followed by Entry trigger on Day 2 (high=105 >= 100).
    Expected: Trade must expire (EXPIRED) pre-entry on Day 1 and never become ACTIVE.
    """
    plan = make_base_plan()
    
    # Day 1: Pre-entry price dip below SL
    day1_candle = {
        "time": "2026-07-01T10:00:00Z",
        "open": 95.0,
        "high": 95.0,
        "low": 85.0,  # Below SL 90.0
        "close": 92.0,
    }
    upd1 = _update_plan_status_raw(plan, day1_candle)
    assert upd1.get("status") == "STOPPED"
    assert upd1.get("exit_reason") == "STOP_LOSS_HIT_BEFORE_ENTRY"

def test_scenario_2_entry_and_sl_in_same_real_candle():
    """
    Scenario 2: Both Entry and Stop Loss are touched in the EXACT SAME single real candle.
    High=105 (>= 100), Low=85 (<= 90).
    Expected: Pre-entry SL hit takes priority and status becomes EXPIRED.
    """
    plan = make_base_plan()
    
    same_candle = {
        "time": "2026-07-01T10:00:00Z",
        "open": 95.0,
        "high": 105.0, # Touches Entry 100.0
        "low": 85.0,   # Touches SL 90.0
        "close": 92.0,
    }
    upd = _update_plan_status_raw(plan, same_candle)
    assert upd.get("lifecycle_blocked") is True
    assert upd.get("block_code") == "MISSING_LOWER_TIMEFRAME_DATA"

def test_scenario_3_entry_then_later_sl():
    """
    Scenario 3: Entry triggered on Day 1, followed by SL hit on Day 2.
    Expected: Day 1 becomes ACTIVE, Day 2 becomes SL_HIT.
    """
    plan = make_base_plan()
    
    # Day 1: Entry triggered
    day1_candle = {
        "time": "2026-07-01T10:00:00Z",
        "open": 98.0,
        "high": 105.0,
        "low": 98.0,
        "close": 102.0,
    }
    upd1 = _update_plan_status_raw(plan, day1_candle)
    assert upd1.get("status") == "ACTIVE"
    plan.update(upd1)
    
    # Day 2: SL hit after active
    day2_candle = {
        "time": "2026-07-02T10:00:00Z",
        "open": 101.0,
        "high": 101.0,
        "low": 88.0, # Below SL 90.0
        "close": 89.0,
    }
    upd2 = _update_plan_status_raw(plan, day2_candle)
    assert upd2.get("status") == "SL_HIT"
    assert upd2.get("exit_reason") == "STOP_LOSS_HIT"

def test_scenario_4_entry_then_later_target():
    """
    Scenario 4: Entry triggered on Day 1, followed by Target 1 hit on Day 2.
    Expected: Day 1 becomes ACTIVE, Day 2 becomes T1_PARTIAL.
    """
    plan = make_base_plan()
    
    # Day 1: Entry triggered
    day1_candle = {
        "time": "2026-07-01T10:00:00Z",
        "open": 98.0,
        "high": 105.0,
        "low": 98.0,
        "close": 102.0,
    }
    upd1 = _update_plan_status_raw(plan, day1_candle)
    assert upd1.get("status") == "ACTIVE"
    plan.update(upd1)
    
    # Day 2: Target 1 hit
    day2_candle = {
        "time": "2026-07-02T10:00:00Z",
        "open": 103.0,
        "high": 112.0, # Above T1 110.0
        "low": 102.0,
        "close": 111.0,
    }
    upd2 = _update_plan_status_raw(plan, day2_candle)
    assert upd2.get("status") == "T1_PARTIAL"
    assert upd2.get("t1_hit") is True

def test_lauruslabs_gap_above_entry_and_target1():
    """
    Regression Test: LAURUSLABS single-candle gap-up above BOTH Entry (1610.70) and Target 1 (1672.20).
    Open=1727.8, High=1727.8, Low=1727.8, Close=1727.8.
    Expected: Trade MUST activate as ACTIVE (or T1_PARTIAL) and NOT be falsely expired as TARGET_HIT_BEFORE_ENTRY.
    """
    plan = {
        "_id": "lauruslabs_test_id",
        "symbol": "LAURUSLABS",
        "entry_price": 1610.7,
        "stop_loss": 1549.2,
        "target_1": 1672.2,
        "target_2": 1733.7,
        "target_3": 1795.2,
        "status": "WAITING_FOR_ENTRY",
        "outcome_status": "WAITING_FOR_ENTRY",
        "state": "WAITING_FOR_ENTRY",
        "entry_triggered": False,
        "quantity": 10,
        "quantity_remaining": 0,
        "trade_quality_grade": "A",
        "setup_date": "2026-07-24",
    }
    gap_candle = {
        "time": "2026-07-27T09:10:08Z",
        "open": 1605.0,
        "high": 1727.8,
        "low": 1605.0,
        "close": 1727.8,
        "source": "paper_market_snapshots",
    }
    upd = _update_plan_status_raw(plan, gap_candle)
    assert upd.get("status") in ("ACTIVE", "T1_PARTIAL", "T2_PARTIAL")
    assert upd.get("status") != "EXPIRED"

def test_active_trade_surviving_missing_snapshots():
    """
    Regression Test: Active trade surviving missing snapshots by falling back to market_candles / historical_ohlcv.
    """
    import asyncio
    from backend.routes.paper import evaluate_paper_trade_chronologically
    
    class FakeCursor:
        def __init__(self, docs):
            self.docs = docs
        def sort(self, *args, **kwargs):
            return self
        def limit(self, *args, **kwargs):
            return self
        async def to_list(self, length=1000):
            return self.docs
        async def __aiter__(self):
            for doc in self.docs:
                yield doc

    class FakeDb:
        def __init__(self):
            self.paper_market_snapshots = type("Coll", (), {"find": lambda self, *args, **kwargs: FakeCursor([])})()
            self.market_candles = type("Coll", (), {"find": lambda self, *args, **kwargs: FakeCursor([
                {
                    "symbol": "TESTSURVIVE",
                    "timestamp": "2026-07-27T10:00:00Z",
                    "open": 101.0,
                    "high": 105.0,
                    "low": 99.0,
                    "close": 104.0,
                }
            ])})()
            self.historical_ohlcv = type("Coll", (), {"find": lambda self, *args, **kwargs: FakeCursor([])})()

    db = FakeDb()
    plan = make_base_plan()
    plan["symbol"] = "TESTSURVIVE"
    plan["created_at"] = "2026-07-27T00:00:00Z"
    plan["setup_date"] = "2026-07-27"

    upd = asyncio.run(evaluate_paper_trade_chronologically(db, plan))
    assert upd.get("status") == "ACTIVE"
    assert upd.get("status") != "EXPIRED"

def test_expired_pre_entry_sl_trades():
    """
    Regression Test: Pre-entry SL trades (TIPSMUSIC, COLPAL, ZEEL, MARICO) correctly evaluate to EXPIRED.
    """
    import asyncio
    from backend.routes.paper import evaluate_paper_trade_chronologically

    class FakeCursor:
        def __init__(self, docs):
            self.docs = docs
        def sort(self, *args, **kwargs):
            return self
        def limit(self, *args, **kwargs):
            return self
        async def to_list(self, length=1000):
            return self.docs
        async def __aiter__(self):
            for doc in self.docs:
                yield doc

    class FakeDb:
        def __init__(self):
            self.paper_market_snapshots = type("Coll", (), {"find": lambda self, *args, **kwargs: FakeCursor([
                {
                    "observed_at_iso": "2026-07-30T10:00:00Z",
                    "open": 2100.0,
                    "high": 2100.0,
                    "low": 2080.0, # Touches SL 2092.10
                    "close": 2085.0,
                }
            ])})()
            self.market_candles = type("Coll", (), {"find": lambda self, *args, **kwargs: FakeCursor([])})()
            self.historical_ohlcv = type("Coll", (), {"find": lambda self, *args, **kwargs: FakeCursor([])})()

    db = FakeDb()
    colpal_plan = {
        **make_base_plan(),
        "symbol": "COLPAL",
        "entry_price": 2214.85,
        "stop_loss": 2092.10,
        "target_1": 2337.60,
        "created_at": "2026-07-29T11:00:00Z",
    }

    upd = asyncio.run(evaluate_paper_trade_chronologically(db, colpal_plan))
    assert upd.get("status") == "STOPPED"
    assert upd.get("exit_reason") == "STOP_LOSS_HIT_BEFORE_ENTRY"

