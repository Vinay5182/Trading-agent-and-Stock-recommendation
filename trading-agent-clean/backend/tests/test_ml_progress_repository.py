from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
import sys

import pytest
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings
from services.ml_progress_repository import MLProgressRepository
from services.ml_pipeline import ml_pipeline
from database import get_database

TEST_DB_NAME = "test_ml_progress_isolated"


async def clean_db():
    client = AsyncIOMotorClient(settings.MONGO_URI)
    db = client[TEST_DB_NAME]
    await db.ml_candidate_daily_progress.drop()
    await db.ml_candidates.drop()
    await db.paper_trades.drop()

    import database
    database.get_database = lambda: db
    import services.ml_progress_repository
    services.ml_progress_repository.get_database = lambda: db
    return client, db


def test_ml_progress_repository_upsert_and_queries() -> None:
    async def run_async():
        _, db = await clean_db()
        await MLProgressRepository.create_indexes()

        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        yesterday_str = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

        # 1. Test upsert of yesterday's progress into single trade document
        hist_doc = {
            "paper_trade_id": "trade_abc",
            "candidate_id": "cand_123",
            "symbol": "INFY",
            "setup_date": "2026-07-01",
            "progress_date": yesterday_str,
            "status": "ACTIVE",
            "current_price": 105.0,
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "rr_progress": 1.0,
            "paper_only": True,
            "created_at": datetime.utcnow(),
        }

        success = await MLProgressRepository.upsert_daily_progress(hist_doc)
        assert success is True

        # 2. Test upsert of today's progress into the SAME trade document
        today_doc = {
            "paper_trade_id": "trade_abc",
            "candidate_id": "cand_123",
            "symbol": "INFY",
            "setup_date": "2026-07-01",
            "progress_date": today_str,
            "status": "ACTIVE",
            "current_price": 108.0,
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "rr_progress": 1.6,
            "paper_only": True,
            "created_at": datetime.utcnow(),
        }

        success_today = await MLProgressRepository.upsert_daily_progress(today_doc)
        assert success_today is True

        # Verify only ONE top-level document exists in collection for paper_trade_id
        trade_doc = await db.ml_candidate_daily_progress.find_one({"paper_trade_id": "trade_abc"})
        assert trade_doc is not None
        assert trade_doc["paper_trade_id"] == "trade_abc"
        assert trade_doc["current_price"] == 108.0
        assert len(trade_doc["daily_progress"]) == 2

        # 3. Test queries
        cand_rows = await MLProgressRepository.get_progress_by_candidate("cand_123")
        assert len(cand_rows) == 2
        assert cand_rows[0]["progress_date"] == yesterday_str
        assert cand_rows[1]["progress_date"] == today_str
        assert cand_rows[1]["close"] == 108.0

        date_rows = await MLProgressRepository.get_progress_for_date(today_str)
        assert len(date_rows) == 1
        assert date_rows[0]["progress_date"] == today_str
        assert date_rows[0]["close"] == 108.0

    asyncio.run(run_async())


def test_record_daily_progress_integration() -> None:
    async def run_async():
        _, db = await clean_db()
        await MLProgressRepository.create_indexes()

        trade = {
            "_id": "507f1f77bcf86cd799439011",
            "paper_trade_id": "507f1f77bcf86cd799439011",
            "candidate_id": "cand_demo_456",
            "symbol": "INFY",
            "setup_date": "2026-07-10",
            "entry_time": "2026-07-10T09:15:00",
            "updated_at": datetime.utcnow().isoformat(),
            "status": "ACTIVE",
            "entry_price": 1500.0,
            "stop_loss": 1470.0,
            "target_1": 1560.0,
            "target_2": 1600.0,
            "target_3": 1650.0,
            "paper_only": True,
        }

        market_row = {
            "close": 1530.0,
            "high": 1535.0,
            "low": 1495.0,
            "observed_at": datetime.utcnow().isoformat(),
        }

        await ml_pipeline.record_daily_progress(trade, market_row)

        # Verify top-level single document
        top_doc = await db.ml_candidate_daily_progress.find_one({"paper_trade_id": "507f1f77bcf86cd799439011"})
        assert top_doc is not None
        assert top_doc["candidate_id"] == "cand_demo_456"
        assert top_doc["paper_trade_id"] == "507f1f77bcf86cd799439011"
        assert top_doc["current_price"] == 1530.0
        assert len(top_doc["daily_progress"]) == 1

        rows = await MLProgressRepository.get_progress_by_candidate("cand_demo_456")
        assert len(rows) == 1
        row = rows[0]
        assert row["candidate_id"] == "cand_demo_456"
        assert row["paper_trade_id"] == "507f1f77bcf86cd799439011"
        assert row["close"] == 1530.0
        assert abs(row["rr_progress"] - 1.0) < 1e-4

    asyncio.run(run_async())
