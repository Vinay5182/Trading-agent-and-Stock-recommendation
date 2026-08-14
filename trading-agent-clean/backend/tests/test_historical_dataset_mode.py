import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from services.capital_accounting import try_activate_trade_with_capital
from routes.paper import setup_valid_until_value

def test_historical_dataset_mode_activates_with_zero_margin():
    import asyncio
    class DummyDB:
        class paper_trades:
            @staticmethod
            async def find_one(query):
                return {
                    "_id": "dummy_id",
                    "paper_only": True,
                    "status": "WAITING_FOR_ENTRY",
                    "state_version": 1,
                    "historical_dataset_mode": True
                }
            @staticmethod
            async def update_one(filter, update, upsert=False):
                class DummyResult:
                    modified_count = 1
                return DummyResult()
    db = DummyDB()
    res = asyncio.run(try_activate_trade_with_capital(db, "dummy_id", 1, "2026-08-11T00:00:00Z"))
    assert res["ok"] is True
    assert res["activated"] is True

def test_historical_dataset_mode_bypasses_wallclock_validity():
    trade = {
        "created_at": "2026-07-27T09:00:00Z",
        "strategy": "SWING",
        "historical_dataset_mode": True
    }
    val = setup_valid_until_value(trade)
    assert val is None
