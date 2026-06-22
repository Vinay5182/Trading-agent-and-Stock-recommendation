import asyncio
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import paper_automation


def matches(row: dict, query: dict | None) -> bool:
    if not query:
        return True
    return all(row.get(key) == value for key, value in query.items())


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = [row.copy() for row in rows]
        self.index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self.index]
        self.index += 1
        return row


class FakeSchedulerStatus:
    def __init__(self) -> None:
        self.rows = []
        self.indexes = []
        self.update_calls = []

    async def create_index(self, fields, **kwargs):
        self.indexes.append((fields, kwargs))
        return kwargs.get("name", "index")

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        self.update_calls.append((query, update, upsert))
        for left, right in (("$setOnInsert", "$inc"), ("$setOnInsert", "$set"), ("$set", "$inc")):
            overlap = set(update.get(left, {})) & set(update.get(right, {}))
            if overlap:
                raise AssertionError(f"Mongo update path conflict between {left} and {right}: {sorted(overlap)}")
        row = next((candidate for candidate in self.rows if matches(candidate, query)), None)
        if row is None:
            row = {**query, **update.get("$setOnInsert", {})}
            self.rows.append(row)
        row.update(update.get("$set", {}))
        for key, value in update.get("$inc", {}).items():
            row[key] = row.get(key, 0) + value
        return SimpleNamespace(modified_count=1, matched_count=1, upserted_id=None)

    def find(self, query: dict | None = None, projection: dict | None = None):
        rows = [row for row in self.rows if matches(row, query)]
        if projection and projection.get("_id") == 0:
            rows = [{key: value for key, value in row.items() if key != "_id"} for row in rows]
        return FakeCursor(rows)


def fake_db() -> SimpleNamespace:
    return SimpleNamespace(scheduler_status=FakeSchedulerStatus())


def test_scheduler_status_persists_through_simulated_restart() -> None:
    async def run() -> None:
        db = fake_db()
        original = deepcopy(paper_automation.AUTOMATION_STATUS)
        try:
            await paper_automation.persist_scheduler_status(
                db,
                "sync_trade_ready",
                {
                    "running": False,
                    "expected_interval_seconds": paper_automation.SYNC_INTERVAL_SECONDS,
                    "last_started_at": "2026-06-17T09:00:00",
                    "last_completed_at": "2026-06-17T09:00:01",
                    "last_success_at": "2026-06-17T09:00:01",
                    "last_error": None,
                    "processed_count": 7,
                },
            )
            paper_automation.AUTOMATION_STATUS["jobs"]["sync_trade_ready"]["last_started_at"] = None
            paper_automation.AUTOMATION_STATUS["jobs"]["sync_trade_ready"]["last_completed_at"] = None
            paper_automation.AUTOMATION_STATUS["jobs"]["sync_trade_ready"]["last_success_at"] = None
            paper_automation.AUTOMATION_STATUS["jobs"]["sync_trade_ready"]["processed_count"] = 0

            status = await paper_automation.get_paper_automation_status_from_db(db, now=datetime(2026, 6, 17, 9, 0, 5))

            assert status["jobs"]["sync_trade_ready"]["last_success_at"] == "2026-06-17T09:00:01"
            assert status["jobs"]["sync_trade_ready"]["processed_count"] == 7
            assert status["processed_count"] == 7
            assert status["persisted_jobs"]["trade_ready_sync"]["expected_interval_seconds"] == paper_automation.SYNC_INTERVAL_SECONDS
        finally:
            paper_automation.AUTOMATION_STATUS.clear()
            paper_automation.AUTOMATION_STATUS.update(original)

    asyncio.run(run())


def test_scheduler_status_counter_increment_has_no_mongo_path_conflict() -> None:
    async def run() -> None:
        db = fake_db()

        await paper_automation.persist_scheduler_status(
            db,
            "sync_trade_ready",
            {
                "running": False,
                "expected_interval_seconds": paper_automation.SYNC_INTERVAL_SECONDS,
                "last_error": "simulated",
            },
            inc={"failed_count": 1},
        )

        update = db.scheduler_status.update_calls[-1][1]
        assert "failed_count" not in update["$setOnInsert"]
        assert db.scheduler_status.rows[0]["failed_count"] == 1

    asyncio.run(run())


def test_scheduler_status_initializes_one_document_per_job() -> None:
    async def run() -> None:
        db = fake_db()

        await paper_automation.initialize_scheduler_status(db)

        job_names = sorted(row["job_name"] for row in db.scheduler_status.rows)
        assert job_names == ["outcome_engine", "trade_ready_sync"]
        assert all(row["running"] is False for row in db.scheduler_status.rows)
        assert next(row for row in db.scheduler_status.rows if row["job_name"] == "trade_ready_sync")["expected_interval_seconds"] == paper_automation.SYNC_INTERVAL_SECONDS
        assert next(row for row in db.scheduler_status.rows if row["job_name"] == "outcome_engine")["expected_interval_seconds"] == paper_automation.OUTCOME_INTERVAL_SECONDS

    asyncio.run(run())


def test_repeated_scheduler_startup_does_not_create_duplicate_tasks(monkeypatch) -> None:
    async def run() -> None:
        async def idle_scheduler() -> None:
            await asyncio.Event().wait()

        monkeypatch.setattr(paper_automation, "run_paper_automation", idle_scheduler)
        paper_automation._AUTOMATION_TASK = None

        first = paper_automation.start_paper_automation_once()
        second = paper_automation.start_paper_automation_once()

        assert first is second
        assert first.done() is False
        await paper_automation.shutdown_paper_automation()
        assert paper_automation._AUTOMATION_TASK is None

    asyncio.run(run())


def test_scheduler_shutdown_cancels_running_task(monkeypatch) -> None:
    async def run() -> None:
        cancelled = {"value": False}

        async def idle_scheduler() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled["value"] = True
                raise

        monkeypatch.setattr(paper_automation, "run_paper_automation", idle_scheduler)
        paper_automation._AUTOMATION_TASK = None

        paper_automation.start_paper_automation_once()
        await asyncio.sleep(0)
        await paper_automation.shutdown_paper_automation()

        assert cancelled["value"] is True
        assert paper_automation._AUTOMATION_TASK is None

    asyncio.run(run())
