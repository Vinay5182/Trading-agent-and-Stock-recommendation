from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from routes.staleness import parse_timestamp, is_score_stale, latest_market_data_updated_at

def test_parse_timestamp_edge_cases():
    # None input
    assert parse_timestamp(None) is None
    assert parse_timestamp("") is None

    # Datetime inputs (naive vs aware)
    now_naive = datetime.utcnow()
    parsed_naive = parse_timestamp(now_naive)
    assert parsed_naive.tzinfo == timezone.utc

    now_aware = datetime.now(timezone.utc)
    parsed_aware = parse_timestamp(now_aware)
    assert parsed_aware == now_aware

    # ISO strings
    iso_z = "2026-06-29T12:00:00Z"
    parsed_z = parse_timestamp(iso_z)
    assert parsed_z.year == 2026
    assert parsed_z.month == 6
    assert parsed_z.day == 29
    assert parsed_z.tzinfo == timezone.utc

    # Invalid values
    assert parse_timestamp("not-a-timestamp") is None

def test_is_score_stale_edge_cases():
    # Empty inputs
    assert is_score_stale(None, None) is False
    assert is_score_stale("2026-06-29T12:00:00Z", None) is True
    assert is_score_stale(None, "2026-06-29T12:00:00Z") is False

    # String timestamps comparisons
    assert is_score_stale("2026-06-29T12:00:00Z", "2026-06-29T11:00:00Z") is True
    assert is_score_stale("2026-06-29T12:00:00Z", "2026-06-29T13:00:00Z") is False

    # Datetime objects or mixed
    dt_market = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)
    dt_score_stale = datetime(2026, 6, 29, 11, 0, 0, tzinfo=timezone.utc)
    dt_score_fresh = datetime(2026, 6, 29, 13, 0, 0, tzinfo=timezone.utc)

    assert is_score_stale(dt_market, dt_score_stale) is True
    assert is_score_stale(dt_market, dt_score_fresh) is False

class FakeMarketCollection:
    def __init__(self, document=None):
        self.document = document

    async def find_one(self, query, projection=None, sort=None):
        return self.document if self.document else {}

def test_latest_market_data_updated_at_empty(monkeypatch):
    async def run_async():
        import routes.staleness
        monkeypatch.setattr(routes.staleness, "get_universe_result", lambda idx: {
            "symbols": [{"canonical_symbol": "TCS"}]
        })

        db = FakeMarketCollection(None)
        # wrap db in namespace so we can do db.market_data
        from types import SimpleNamespace
        db_namespace = SimpleNamespace(market_data=db)
        res = await latest_market_data_updated_at(db_namespace, "NIFTY50")
        assert res is None
    asyncio.run(run_async())

def test_latest_market_data_updated_at_present(monkeypatch):
    async def run_async():
        import routes.staleness
        monkeypatch.setattr(routes.staleness, "get_universe_result", lambda idx: {
            "symbols": [{"canonical_symbol": "TCS"}]
        })

        db = FakeMarketCollection({"updated_at": "2026-06-29T12:00:00Z"})
        from types import SimpleNamespace
        db_namespace = SimpleNamespace(market_data=db)
        res = await latest_market_data_updated_at(db_namespace, "NIFTY50")
        assert res == "2026-06-29T12:00:00Z"
    asyncio.run(run_async())
