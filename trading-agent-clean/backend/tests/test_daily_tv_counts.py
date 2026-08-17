import pytest
from services.tv_saved_results import persist_daily_tv_counts


class MockCollection:
    def __init__(self):
        self.docs = {}

    async def update_one(self, filter_dict, update_dict, upsert=False):
        trade_date = filter_dict.get("trade_date")
        doc = self.docs.get(trade_date)
        is_insert = doc is None

        if is_insert and upsert:
            doc = {}
            for k, v in update_dict.get("$setOnInsert", {}).items():
                doc[k] = v
            self.docs[trade_date] = doc

        if "$set" in update_dict:
            for k, v in update_dict["$set"].items():
                doc[k] = v


class MockDb:
    def __init__(self):
        self.daily_tradingview_counts = MockCollection()


@pytest.mark.anyio
async def test_persist_daily_tv_counts_phase2_summary():
    db = MockDb()
    collection = db.daily_tradingview_counts

    # 1. Swing update on date "2026-07-29" with Selected: 26, Confirmed: 3, Rejected: 23
    await persist_daily_tv_counts(
        db,
        strategy_type="swing",
        selected_count=26,
        confirmed_count=3,
        rejected_count=23,
        trade_date="2026-07-29",
    )
    doc = collection.docs["2026-07-29"]
    assert doc["trade_date"] == "2026-07-29"
    assert doc["swing_selected"] == 26
    assert doc["swing_confirmed"] == 3
    assert doc["swing_rejected"] == 23
    assert doc["momentum_selected"] == 0
    assert doc["momentum_confirmed"] == 0
    assert doc["momentum_rejected"] == 0

    # 2. Momentum update on date "2026-07-29" with Selected: 99, Confirmed: 45, Rejected: 54
    await persist_daily_tv_counts(
        db,
        strategy_type="momentum",
        selected_count=99,
        confirmed_count=45,
        rejected_count=54,
        trade_date="2026-07-29",
    )
    doc = collection.docs["2026-07-29"]
    assert doc["trade_date"] == "2026-07-29"
    # Swing fields preserved
    assert doc["swing_selected"] == 26
    assert doc["swing_confirmed"] == 3
    assert doc["swing_rejected"] == 23
    # Momentum fields updated
    assert doc["momentum_selected"] == 99
    assert doc["momentum_confirmed"] == 45
    assert doc["momentum_rejected"] == 54
