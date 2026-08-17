import pytest
import database
from ai.trade_status import CLOSED_TRADE_STATUSES
from routes.paper import is_open_trade, is_waiting_trade
from routes.ai import is_closed_paper_trade, _is_open_paper_trade, _is_waiting_paper_trade
from motor.motor_asyncio import AsyncIOMotorClient

@pytest.fixture
def anyio_backend():
    return 'asyncio'

def test_closed_trade_statuses_taxonomy():
    assert "ENTRY_MISSED_GAP_UP" in CLOSED_TRADE_STATUSES
    assert "COMPLETED" in CLOSED_TRADE_STATUSES or "T3_HIT" in CLOSED_TRADE_STATUSES
    assert "SL_HIT" in CLOSED_TRADE_STATUSES or "STOPPED" in CLOSED_TRADE_STATUSES

def test_is_open_trade_classification():
    active_trade = {"status": "ACTIVE", "outcome_status": "ACTIVE"}
    t1_partial = {"status": "T1_PARTIAL", "outcome_status": "T1_PARTIAL"}
    t2_partial = {"status": "T2_PARTIAL", "outcome_status": "T2_PARTIAL"}
    completed = {"status": "COMPLETED", "outcome_status": "COMPLETED"}
    sl_hit = {"status": "SL_HIT", "outcome_status": "SL_HIT"}
    gap_missed = {"status": "ENTRY_MISSED_GAP_UP", "outcome_status": "ENTRY_MISSED_GAP_UP"}
    invalidated = {"status": "INVALIDATED_STALE", "outcome_status": "INVALIDATED_STALE", "invalidated_reason": "STALE_ENTRY"}

    assert is_open_trade(active_trade) is True
    assert is_open_trade(t1_partial) is True
    assert is_open_trade(t2_partial) is True
    assert is_open_trade(completed) is False
    assert is_open_trade(sl_hit) is False
    assert is_open_trade(gap_missed) is False
    assert is_open_trade(invalidated) is False

    assert _is_open_paper_trade(active_trade) is True
    assert _is_open_paper_trade(t1_partial) is True
    assert _is_open_paper_trade(t2_partial) is True
    assert _is_open_paper_trade(completed) is False

@pytest.mark.anyio
async def test_mongodb_read_only_integrity_and_avalon():
    client = AsyncIOMotorClient('mongodb://localhost:27017')
    try:
        db = client.get_database("trading_agent_clean")

        count = await db.paper_trades.count_documents({})
        assert count >= 147

        avalon = await db.paper_trades.find_one({'symbol': 'AVALON'})
        assert avalon is not None
        assert avalon.get('status') == 'INVALIDATED_STALE'
        assert avalon.get('invalidated_reason') == 'STALE_ENTRY_PRE_DEPLOYMENT'
        assert float(avalon.get('realized_pnl') or 0.0) == 0.0
        assert float(avalon.get('paper_pnl') or 0.0) == 0.0

        symbols = ['MASTEK', 'IIFL', 'TVSMOTOR', 'MEDANTA', 'PPLPHARMA', 'LALPATHLAB', 'PRICOLLTD', 'BAJFINANCE', 'PRUDENT', 'TI', 'SAPPHIRE', 'PAYTM', 'KTKBANK']
        cursor = db.paper_trades.find({'symbol': {'$in': symbols}, 'status': {'$in': ['T1_PARTIAL', 'T2_PARTIAL']}})
        partials = [t async for t in cursor]
        assert len(partials) >= 13
    finally:
        client.close()
