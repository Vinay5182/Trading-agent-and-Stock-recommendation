import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from pymongo.errors import DuplicateKeyError


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.trade_journal import (
    JournalWriteError,
    analytics_realized_pnl_record,
    build_trade_analytics,
    journal_completed_trade,
    sync_completed_trades_to_journal,
)


def matches(row: dict, query: dict | None) -> bool:
    if not query:
        return True
    for key, expected in query.items():
        if row.get(key) != expected:
            return False
    return True


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = [row.copy() for row in rows]
        self.index = 0

    def sort(self, *_args):
        return self

    def limit(self, limit: int):
        self.rows = self.rows[:limit]
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self.index]
        self.index += 1
        return row


class FakeCollection:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.update_calls = []
        self.insert_calls = []
        self.indexes = []

    async def create_index(self, fields, **kwargs):
        self.indexes.append((fields, kwargs))
        return kwargs.get("name", "index")

    def find(self, query: dict | None = None, *_args, **_kwargs):
        return FakeCursor([row for row in self.rows if matches(row, query)])

    async def insert_one(self, document: dict):
        self.insert_calls.append(document.copy())
        if any(row.get("paper_trade_id") == document.get("paper_trade_id") for row in self.rows):
            raise DuplicateKeyError("duplicate paper_trade_id")
        self.rows.append(document.copy())
        return SimpleNamespace(inserted_id=document.get("paper_trade_id"))

    async def update_one(self, *args, **kwargs):
        self.update_calls.append((args, kwargs))
        query = args[0] if args else {}
        update = args[1] if len(args) > 1 else {}
        row = next((row for row in self.rows if matches(row, query)), None)
        if row is None:
            return SimpleNamespace(modified_count=0)
        row.update(update.get("$set", {}))
        return SimpleNamespace(modified_count=1)


class FailingJournalCollection(FakeCollection):
    async def insert_one(self, document: dict):
        self.insert_calls.append(document.copy())
        raise RuntimeError("simulated journal outage")


def completed_trade(**overrides) -> dict:
    trade = {
        "_id": "trade-1",
        "paper_only": True,
        "symbol": "TEST",
        "source_signal_type": "SWING_TV_CONFIRMED",
        "status": "COMPLETED",
        "outcome_status": "T3_HIT",
        "entry_price": 100.0,
        "initial_stop_loss": 90.0,
        "target_1": 120.0,
        "target_2": 130.0,
        "target_3": 140.0,
        "entry_triggered_at": "2026-06-01T09:30:00",
        "status_updated_at": "2026-06-05T15:30:00",
        "exit_reason": "T3_HIT",
        "partial_exit_1": {"exit_price": 120.0, "quantity": 3, "percent": 33, "paper_pnl": 60.0},
        "partial_exit_2": {"exit_price": 130.0, "quantity": 3, "percent": 33, "paper_pnl": 90.0},
        "partial_exit_3": {"exit_price": 140.0, "quantity": 4, "percent": 34, "paper_pnl": 160.0},
        "paper_pnl": 310.0,
        "paper_pnl_percent": 31.0,
        "total_trade_pnl": 310.0,
        "initial_risk_amount": 100.0,
        "realized_rr": 3.1,
        "risk_reward_3": 4.0,
        "trade_quality_grade": "A_PLUS",
        "trap_status": "CLEAN",
        "fake_breakout_risk": "LOW",
        "retail_trap_risk": "LOW",
    }
    trade.update(overrides)
    return trade


def test_completed_trade_journal_is_insert_only_and_maps_required_fields() -> None:
    db = SimpleNamespace(paper_trades=FakeCollection([]), trade_journal=FakeCollection([]))
    trade = completed_trade()

    first = asyncio.run(journal_completed_trade(db, trade))
    second = asyncio.run(journal_completed_trade(db, trade))

    assert first["journaled"] is True
    assert second["duplicate"] is True
    assert len(db.trade_journal.rows) == 1
    record = db.trade_journal.rows[0]
    assert record["symbol"] == "TEST"
    assert record["strategy_type"] == "Swing"
    assert record["entry"] == 100.0
    assert record["stop_loss"] == 90.0
    assert record["T1"] == 120.0
    assert record["T2"] == 130.0
    assert record["T3"] == 140.0
    assert record["SL_HIT"] is False
    assert record["T1_HIT"] is True
    assert record["T2_HIT"] is True
    assert record["T3_HIT"] is True
    assert record["grade"] == "A+"
    assert record["RR"] == 3.1
    assert record["trap_status"] == "CLEAN"
    assert record["profit_percent"] == 31.0
    assert record["paper_pnl"] == 310.0
    assert record["RR"] == 3.1
    assert record["realized_rr"] == 3.1
    assert record["days_held"] == 4
    assert db.paper_trades.update_calls == []


def test_sync_completed_trades_to_journal_does_not_mutate_completed_trades() -> None:
    trades = [
        completed_trade(_id="win", symbol="WIN"),
        completed_trade(_id="open", symbol="OPEN", status="ACTIVE", outcome_status="ACTIVE"),
    ]
    db = SimpleNamespace(paper_trades=FakeCollection(trades), trade_journal=FakeCollection([]))

    result = asyncio.run(sync_completed_trades_to_journal(db))

    assert result["processed"] == 1
    assert result["journaled"] == 1
    assert len(db.trade_journal.rows) == 1
    assert db.paper_trades.update_calls == []
    assert next(row for row in db.paper_trades.rows if row["_id"] == "win")["status"] == "COMPLETED"


def test_journal_backfill_is_exactly_once() -> None:
    trades = [completed_trade(_id="win", symbol="WIN")]
    db = SimpleNamespace(paper_trades=FakeCollection(trades), trade_journal=FakeCollection([]))

    first = asyncio.run(sync_completed_trades_to_journal(db))
    second = asyncio.run(sync_completed_trades_to_journal(db))

    assert first["journaled"] == 1
    assert first["duplicates"] == 0
    assert second["journaled"] == 0
    assert second["duplicates"] == 1
    assert len(db.trade_journal.rows) == 1


def test_journal_pending_completion_pending_recovery_is_idempotent() -> None:
    trade = completed_trade(
        _id="pending",
        symbol="PENDING",
        journal_pending=True,
        completion_pending=True,
        journal_status="PENDING",
    )
    db = SimpleNamespace(paper_trades=FakeCollection([trade]), trade_journal=FakeCollection([]))

    first = asyncio.run(sync_completed_trades_to_journal(db))
    second = asyncio.run(sync_completed_trades_to_journal(db))

    row = db.paper_trades.rows[0]
    assert first["journaled"] == 1
    assert second["journaled"] == 0
    assert second["duplicates"] == 1
    assert len(db.trade_journal.rows) == 1
    assert row["journal_status"] == "JOURNALED"
    assert row["journal_pending"] is False
    assert row["completion_pending"] is False
    assert len(db.paper_trades.update_calls) == 1


def test_journal_insert_failure_is_reported_not_silently_successful() -> None:
    trade = completed_trade(_id="win", symbol="WIN")
    db = SimpleNamespace(paper_trades=FakeCollection([trade]), trade_journal=FailingJournalCollection([]))

    try:
        asyncio.run(journal_completed_trade(db, trade))
    except JournalWriteError as exc:
        assert "JOURNAL_INSERT_FAILED" in str(exc)
    else:
        raise AssertionError("journal_completed_trade must raise when immutable insert fails")

    result = asyncio.run(sync_completed_trades_to_journal(db))

    assert result["ok"] is False
    assert result["processed"] == 1
    assert result["journaled"] == 0
    assert result["errors_count"] == 1


def test_trade_analytics_builds_dashboard_cards_and_comparisons() -> None:
    records = [
        {
            "symbol": "WIN",
            "strategy_type": "Swing",
            "grade": "A+",
            "trap_status": "CLEAN",
            "profit_percent": 30.0,
            "RR": 4.0,
            "exit_date": "2026-06-05T15:30:00",
            "exit_reason": "T3_HIT",
        },
        {
            "symbol": "LOSS",
            "strategy_type": "Momentum",
            "grade": "B",
            "trap_status": "DANGER",
            "profit_percent": -10.0,
            "RR": 2.0,
            "exit_date": "2026-06-20T15:30:00",
            "exit_reason": "SL_HIT",
        },
    ]

    analytics = build_trade_analytics(records)

    assert analytics["total_trades"] == 2
    assert analytics["win_rate"] == 50.0
    assert analytics["average_profit"] == 30.0
    assert analytics["average_loss"] == -10.0
    assert analytics["profit_factor"] == 3.0
    assert analytics["average_rr"] == 3.0
    assert analytics["best_trade"]["symbol"] == "WIN"
    assert analytics["worst_trade"]["symbol"] == "LOSS"
    assert analytics["monthly_pnl"] == {"2026-06": 20.0}
    assert analytics["yearly_pnl"] == {"2026": 20.0}
    assert analytics["strategy_comparison"]["Swing"]["total_trades"] == 1
    assert analytics["strategy_comparison"]["Momentum"]["total_trades"] == 1
    assert analytics["grade_comparison"]["A+"]["total_trades"] == 1
    assert analytics["grade_comparison"]["B"]["total_trades"] == 1
    assert analytics["trap_comparison"]["CLEAN"]["total_trades"] == 1
    assert analytics["trap_comparison"]["DANGER"]["total_trades"] == 1
    assert analytics["dashboard_cards"]["Total Trades"] == 2
    assert analytics["dashboard_cards"]["Win Rate"] == 50.0
    assert analytics["dashboard_cards"]["Profit Factor"] == 3.0
    assert analytics["dashboard_cards"]["Average RR"] == 3.0
    assert analytics["dashboard_cards"]["Monthly Return"] == 20.0


def test_ambiguous_records_are_in_realized_pnl_but_excluded_from_denominators() -> None:
    records = [
        {
            "symbol": "WIN",
            "strategy_type": "Swing",
            "grade": "A+",
            "trap_status": "CLEAN",
            "profit_percent": 30.0,
            "total_trade_pnl": 300.0,
            "RR": 4.0,
            "exit_date": "2026-06-05T15:30:00",
            "exit_reason": "T3_HIT",
            "status": "COMPLETED",
            "outcome_status": "T3_HIT",
        },
        {
            "symbol": "LOSS",
            "strategy_type": "Momentum",
            "grade": "B",
            "trap_status": "DANGER",
            "profit_percent": -10.0,
            "total_trade_pnl": -100.0,
            "RR": -1.0,
            "exit_date": "2026-06-20T15:30:00",
            "exit_reason": "SL_HIT",
            "status": "COMPLETED",
            "outcome_status": "SL_HIT",
        },
        {
            "symbol": "AMB",
            "strategy_type": "Swing",
            "grade": "A+",
            "trap_status": "CLEAN",
            "profit_percent": 999.0,
            "total_trade_pnl": 9999.0,
            "RR": 99.0,
            "exit_date": "2026-06-21T15:30:00",
            "exit_reason": "AMBIGUOUS",
            "status": "AMBIGUOUS",
            "outcome_status": "AMBIGUOUS",
            "ambiguous": True,
        },
    ]

    analytics = build_trade_analytics(records)

    assert analytics["total_trades"] == 2
    assert analytics["ambiguous_count"] == 1
    assert analytics["win_rate"] == 50.0
    assert analytics["profit_factor"] == 3.0
    assert analytics["monthly_pnl"] == {"2026-06": 10199.0}
    assert analytics["yearly_pnl"] == {"2026": 10199.0}
    assert analytics["best_trade"]["symbol"] == "WIN"
    assert analytics["worst_trade"]["symbol"] == "LOSS"
    assert analytics["strategy_comparison"]["Swing"]["total_trades"] == 1
    assert analytics["grade_comparison"]["A+"]["total_trades"] == 1


def test_ambiguous_realized_pnl_has_no_date_gate_and_unresolved_rows_are_excluded() -> None:
    records = [
        {
            "symbol": "PAST_AMB",
            "status": "AMBIGUOUS",
            "outcome_status": "AMBIGUOUS",
            "ambiguous": True,
            "total_trade_pnl": 500.0,
            "profit_percent": 500.0,
            "exit_date": "2024-01-05T10:00:00",
        },
        {
            "symbol": "CURRENT_AMB",
            "status": "AMBIGUOUS",
            "outcome_status": "AMBIGUOUS",
            "ambiguous": True,
            "total_trade_pnl": -50.0,
            "profit_percent": -50.0,
            "exit_date": "2026-07-10T10:00:00",
        },
        {
            "symbol": "FUTURE_AMB",
            "status": "AMBIGUOUS",
            "outcome_status": "AMBIGUOUS",
            "ambiguous": True,
            "total_trade_pnl": -300.0,
            "profit_percent": -300.0,
            "exit_date": "2099-12-31T10:00:00",
        },
        {
            "symbol": "WAIT",
            "status": "WAITING",
            "total_trade_pnl": 999.0,
            "profit_percent": 999.0,
            "exit_date": "2026-07-10T10:00:00",
        },
        {
            "symbol": "ACTIVE",
            "status": "ACTIVE",
            "total_trade_pnl": 777.0,
            "profit_percent": 777.0,
            "exit_date": "2026-07-10T10:00:00",
        },
        {
            "symbol": "PLANNED",
            "status": "PLANNED",
            "total_trade_pnl": 123.0,
            "profit_percent": 123.0,
            "exit_date": "2026-07-10T10:00:00",
        },
        {
            "symbol": "NOT_TRIGGERED",
            "status": "NOT_TRIGGERED",
            "total_trade_pnl": 456.0,
            "profit_percent": 456.0,
            "exit_date": "2026-07-10T10:00:00",
        },
    ]

    assert [row["symbol"] for row in records if analytics_realized_pnl_record(row)] == [
        "PAST_AMB",
        "CURRENT_AMB",
        "FUTURE_AMB",
    ]

    analytics = build_trade_analytics(records)

    assert analytics["total_trades"] == 0
    assert analytics["ambiguous_count"] == 3
    assert analytics["win_rate"] == 0.0
    assert analytics["profit_factor"] == 0.0
    assert analytics["monthly_pnl"] == {
        "2024-01": 500.0,
        "2026-07": -50.0,
        "2099-12": -300.0,
    }
    assert analytics["yearly_pnl"] == {"2024": 500.0, "2026": -50.0, "2099": -300.0}
