import pytest
import asyncio
from datetime import datetime
import hashlib
from pathlib import Path
import sys

# Insert backend directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.ml_outcome_repository import MLOutcomeRepository
from services.ml_candidate_repository import MLCandidateRepository
from services.ml_pipeline import MLPipeline
from database import get_database

class FakeCursor:
    def __init__(self, docs):
        self.docs = docs
        self.idx = 0
    def sort(self, field, direction):
        return self
    def limit(self, limit_num):
        return self
    def __aiter__(self):
        return self
    async def __anext__(self):
        if self.idx < len(self.docs):
            doc = self.docs[self.idx]
            self.idx += 1
            return doc
        raise StopAsyncIteration

class FakeCollection:
    def __init__(self):
        self.docs = {}
        self.indexes = []

    async def create_index(self, key, **kwargs):
        self.indexes.append((key, kwargs))

    async def update_one(self, filter_dict, update_dict, upsert=False):
        cand_id = filter_dict.get("candidate_id")
        if not cand_id:
            return
        if cand_id in self.docs:
            if "$set" in update_dict:
                self.docs[cand_id].update(update_dict["$set"])
        elif upsert:
            set_on_insert = update_dict.get("$setOnInsert", {})
            set_dict = update_dict.get("$set", {})
            new_doc = dict(set_on_insert)
            new_doc.update(set_dict)
            self.docs[cand_id] = new_doc

    async def find_one(self, filter_dict, projection=None):
        for cand_id, doc in self.docs.items():
            match = True
            for k, v in filter_dict.items():
                if doc.get(k) != v:
                    match = False
                    break
            if match:
                res = dict(doc)
                if projection and projection.get("_id") == 0 and "_id" in res:
                    del res["_id"]
                return res
        return None

    def find(self, filter_dict, projection=None):
        matching = []
        for cand_id, doc in self.docs.items():
            match = True
            for k, v in filter_dict.items():
                if doc.get(k) != v:
                    match = False
                    break
            if match:
                res = dict(doc)
                if projection and projection.get("_id") == 0 and "_id" in res:
                    del res["_id"]
                matching.append(res)
        return FakeCursor(matching)

def test_ml_outcome_repository_upsert_immutability(monkeypatch) -> None:
    async def run_async():
        fake_coll = FakeCollection()
        monkeypatch.setattr(MLOutcomeRepository, "get_collection", lambda: fake_coll)

        await MLOutcomeRepository.create_indexes()
        assert any(idx[0] == "outcome_id" and idx[1].get("unique") is True for idx in fake_coll.indexes)
        assert any(idx[0] == "candidate_id" and idx[1].get("unique") is True for idx in fake_coll.indexes)

        outcome1 = {
            "outcome_id": "out_1111111111111111",
            "candidate_id": "cand_abc",
            "paper_trade_id": "trade_123",
            "symbol": "INFY",
            "entry_time": "2026-07-10T10:00:00Z",
            "exit_time": "2026-07-14T14:00:00Z",
            "result": "WIN",
            "realized_pnl": 500.0,
            "created_at": datetime.utcnow()
        }
        inserted = await MLOutcomeRepository.insert_outcome(outcome1)
        assert inserted is True

        retrieved = await MLOutcomeRepository.get_outcome_by_candidate_id("cand_abc")
        assert retrieved is not None
        assert retrieved["outcome_id"] == "out_1111111111111111"
        assert retrieved["realized_pnl"] == 500.0

        # Attempt overwrite with different PnL / exit_time
        outcome_overwrite = {
            "outcome_id": "out_2222222222222222",
            "candidate_id": "cand_abc",
            "paper_trade_id": "trade_123",
            "symbol": "INFY",
            "entry_time": "2026-07-10T10:00:00Z",
            "exit_time": "2026-07-15T15:00:00Z",
            "result": "LOSS",
            "realized_pnl": -999.0,
            "created_at": datetime.utcnow()
        }
        await MLOutcomeRepository.insert_outcome(outcome_overwrite)

        # Verify original document remains immutable due to $setOnInsert
        retrieved_after = await MLOutcomeRepository.get_outcome_by_candidate_id("cand_abc")
        assert retrieved_after["outcome_id"] == "out_1111111111111111"
        assert retrieved_after["result"] == "WIN"
        assert retrieved_after["realized_pnl"] == 500.0

        by_trade = await MLOutcomeRepository.get_outcome_by_trade_id("trade_123")
        assert by_trade["candidate_id"] == "cand_abc"

        by_id = await MLOutcomeRepository.get_outcome_by_id("out_1111111111111111")
        assert by_id["candidate_id"] == "cand_abc"

        list_res = await MLOutcomeRepository.list_outcomes_by_result("WIN")
        assert len(list_res) == 1
        assert list_res[0]["candidate_id"] == "cand_abc"

    asyncio.run(run_async())

