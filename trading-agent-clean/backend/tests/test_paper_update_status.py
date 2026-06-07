import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes import paper


def make_plan(status: str = "NOT_TRIGGERED") -> dict:
    return {
        "_id": "test-trade-id",
        "symbol": "TEST",
        "tradingview_symbol": "NSE:TEST",
        "timeframe": "1D",
        "source_signal_type": "SWING_TV_CONFIRMED",
        "paper_only": True,
        "status": status,
        "outcome_status": status,
        "entry_triggered": status != "NOT_TRIGGERED",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "target_1": 120.0,
        "target_2": 130.0,
        "target_3": 140.0,
        "quantity": 10,
        "paper_pnl": 0.0,
    }


def make_candle(*, high: float, low: float, close: float) -> dict:
    return {
        "time": 1_700_000_000,
        "open": 100.0,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1_000,
    }


def make_plans(count: int, status: str = "NOT_TRIGGERED") -> list[dict]:
    plans = []
    for index in range(count):
        plan = make_plan(status)
        plan["_id"] = f"test-trade-{index}"
        plan["symbol"] = f"TEST{index}"
        plan["tradingview_symbol"] = f"NSE:TEST{index}"
        plans.append(plan)
    return plans


def test_waiting_trade_not_triggered_is_noop() -> None:
    plan = make_plan()

    update = paper.update_plan_status(plan, make_candle(high=99.0, low=95.0, close=98.0))

    assert update == {}
    assert paper.proposed_update_reason(plan, update) == "WAITING_FOR_ENTRY"
    assert update.get("status", plan["status"]) == "NOT_TRIGGERED"
    assert update.get("outcome_status", plan["outcome_status"]) == "NOT_TRIGGERED"
    assert update.get("paper_pnl", plan["paper_pnl"]) == 0
    assert bool(update) is False


def test_waiting_trade_entry_triggered_becomes_active() -> None:
    plan = make_plan()

    update = paper.update_plan_status(plan, make_candle(high=100.0, low=95.0, close=101.0))

    assert update["status"] == "ACTIVE"
    assert update["outcome_status"] == "ACTIVE"
    assert update["entry_triggered"] is True
    assert update["exit_reason"] is None
    assert update["paper_pnl"] == pytest.approx(10.0)


def test_waiting_trade_legacy_planned_alias_is_noop() -> None:
    plan = make_plan("PLANNED")
    plan["entry_triggered"] = False

    update = paper.update_plan_status(plan, make_candle(high=99.0, low=95.0, close=98.0))

    assert update == {}
    assert paper.proposed_update_reason(plan, update) == "WAITING_FOR_ENTRY"
    assert update.get("status", plan["status"]) == "PLANNED"
    assert update.get("paper_pnl", plan["paper_pnl"]) == 0
    assert bool(update) is False


def test_waiting_trade_legacy_planned_alias_entry_triggered_becomes_active() -> None:
    plan = make_plan("PLANNED")
    plan["entry_triggered"] = False

    update = paper.update_plan_status(plan, make_candle(high=100.0, low=95.0, close=101.0))

    assert update["status"] == "ACTIVE"
    assert update["outcome_status"] == "ACTIVE"
    assert update["entry_triggered"] is True
    assert update["exit_reason"] is None
    assert update["paper_pnl"] == pytest.approx(10.0)


def test_active_trade_stop_loss_hit() -> None:
    plan = make_plan("ACTIVE")

    update = paper.update_plan_status(plan, make_candle(high=105.0, low=90.0, close=95.0))

    assert update["status"] == "STOPPED"
    assert update["outcome_status"] == "STOPPED"
    assert update["exit_reason"] == "STOP_LOSS_HIT"
    assert update["exit_price"] == 90.0
    assert update["paper_pnl"] == pytest.approx(-100.0)
    assert update["paper_pnl_percent"] == pytest.approx(-10.0)


def test_active_trade_target_hit() -> None:
    plan = make_plan("ACTIVE")

    update = paper.update_plan_status(plan, make_candle(high=120.0, low=95.0, close=118.0))

    assert update["status"] == "TARGET_1_HIT"
    assert update["outcome_status"] == "TARGET_1_HIT"
    assert update["exit_reason"] == "TARGET_1_HIT"
    assert update["exit_price"] is None
    assert update["paper_pnl"] == pytest.approx(180.0)
    assert update["paper_pnl_percent"] == pytest.approx(18.0)


def test_target_1_hit_continues_open_without_stop_or_next_target() -> None:
    plan = make_plan("TARGET_1_HIT")

    update = paper.update_plan_status(plan, make_candle(high=125.0, low=95.0, close=124.0))

    assert update.get("status", plan["status"]) == "TARGET_1_HIT"
    assert update.get("outcome_status", plan["outcome_status"]) == "TARGET_1_HIT"
    assert update["exit_reason"] is None
    assert update["exit_price"] is None
    assert update["paper_pnl"] == pytest.approx(240.0)
    assert update["paper_pnl_percent"] == pytest.approx(24.0)


def test_target_1_hit_reaches_target_2() -> None:
    plan = make_plan("TARGET_1_HIT")

    update = paper.update_plan_status(plan, make_candle(high=130.0, low=95.0, close=128.0))

    assert update["status"] == "TARGET_2_HIT"
    assert update["outcome_status"] == "TARGET_2_HIT"
    assert update["exit_reason"] == "TARGET_2_HIT"
    assert update["exit_price"] == 130.0
    assert update["paper_pnl"] == pytest.approx(300.0)
    assert update["paper_pnl_percent"] == pytest.approx(30.0)


def test_target_1_hit_stops_after_target_1() -> None:
    plan = make_plan("TARGET_1_HIT")

    update = paper.update_plan_status(plan, make_candle(high=125.0, low=90.0, close=95.0))

    assert update["status"] == "STOPPED_AFTER_T1"
    assert update["outcome_status"] == "STOPPED_AFTER_T1"
    assert update["exit_reason"] == "STOPPED_AFTER_T1"
    assert update["exit_price"] == 90.0
    assert update["paper_pnl"] == pytest.approx(-100.0)
    assert update["paper_pnl_percent"] == pytest.approx(-10.0)


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.index = 0

    def sort(self, *_args) -> "FakeCursor":
        return self

    def limit(self, limit: int) -> "FakeCursor":
        self.rows = self.rows[:limit]
        return self

    def __aiter__(self) -> "FakeCursor":
        return self

    async def __anext__(self) -> dict:
        if self.index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self.index]
        self.index += 1
        return row


class FakePaperTrades:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.update_calls = []
        self.delete_calls = []

    def find(self, *_args, **_kwargs) -> FakeCursor:
        return FakeCursor(self.rows.copy())

    async def update_one(self, *args, **kwargs) -> SimpleNamespace:
        self.update_calls.append((args, kwargs))
        return SimpleNamespace(modified_count=1)

    async def delete_one(self, *args, **kwargs) -> SimpleNamespace:
        self.delete_calls.append((args, kwargs))
        return SimpleNamespace(deleted_count=1)


class FakeTradingViewClient:
    candle = make_candle(high=100.0, low=95.0, close=101.0)

    def connect_to_debug_port(self) -> bool:
        return True

    def open_symbol(self, _symbol: str) -> dict:
        return {}

    def fetch_candles(self, _timeframe: str, min_candles: int = 1) -> list[dict]:
        assert min_candles == 1
        return [self.candle]


def test_dry_run_proposes_transition_without_mongo_write(monkeypatch: pytest.MonkeyPatch) -> None:
    collection = FakePaperTrades([make_plan()])
    fake_db = SimpleNamespace(paper_trades=collection)
    monkeypatch.setattr(paper, "get_database", lambda: fake_db)
    monkeypatch.setattr(paper, "TradingViewClient", FakeTradingViewClient)

    response = asyncio.run(paper.run_paper_trade_update(1, "1D", True, "test-dry-run"))

    assert response["dry_run"] is True
    assert response["mongo_writes_enabled"] is False
    assert response["processed"] == 1
    assert response["proposed_write_count"] == 1
    assert response["would_update_count"] == 1
    assert response["updated_count"] == 0
    assert response["errors_count"] == 0
    assert response["blocked"] is False
    assert response["max_trades"] == 1
    assert response["max_writes"] == 1
    assert response["results"][0]["proposed_new_status"] == "ACTIVE"
    assert response["results"][0]["would_write"] is True
    assert collection.update_calls == []
    assert collection.delete_calls == []


def test_real_mode_blocks_all_writes_when_max_writes_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    collection = FakePaperTrades(make_plans(2))
    fake_db = SimpleNamespace(paper_trades=collection)
    monkeypatch.setattr(paper, "get_database", lambda: fake_db)
    monkeypatch.setattr(paper, "TradingViewClient", FakeTradingViewClient)

    response = asyncio.run(paper.run_paper_trade_update(2, "1D", False, "test-write-limit", max_writes=1))

    assert response["processed"] == 2
    assert response["proposed_write_count"] == 2
    assert response["updated_count"] == 0
    assert response["successful_updates_count"] == 0
    assert response["blocked"] is True
    assert response["block_reason"] == "MAX_WRITES_EXCEEDED"
    assert response["mongo_writes_enabled"] is False
    assert all(result["write_blocked"] is True for result in response["results"])
    assert collection.update_calls == []
    assert collection.delete_calls == []


def test_dry_run_reports_when_real_mode_would_be_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    collection = FakePaperTrades(make_plans(2))
    fake_db = SimpleNamespace(paper_trades=collection)
    monkeypatch.setattr(paper, "get_database", lambda: fake_db)
    monkeypatch.setattr(paper, "TradingViewClient", FakeTradingViewClient)

    response = asyncio.run(paper.run_paper_trade_update(2, "1D", True, "test-dry-run-block", max_writes=1))

    assert response["dry_run"] is True
    assert response["proposed_write_count"] == 2
    assert response["blocked"] is True
    assert response["block_reason"] == "MAX_WRITES_EXCEEDED"
    assert response["mongo_writes_enabled"] is False
    assert collection.update_calls == []
    assert collection.delete_calls == []


def test_real_mode_allows_expected_writes_within_max_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    collection = FakePaperTrades(make_plans(1))
    fake_db = SimpleNamespace(paper_trades=collection)
    monkeypatch.setattr(paper, "get_database", lambda: fake_db)
    monkeypatch.setattr(paper, "TradingViewClient", FakeTradingViewClient)

    response = asyncio.run(paper.run_paper_trade_update(1, "1D", False, "test-allowed-write", max_writes=1))

    assert response["proposed_write_count"] == 1
    assert response["updated_count"] == 1
    assert response["successful_updates_count"] == 1
    assert response["errors_count"] == 0
    assert response["blocked"] is False
    assert response["mongo_writes_enabled"] is True
    assert response["results"][0]["write_attempted"] is True
    assert response["results"][0]["updated"] is True
    assert len(collection.update_calls) == 1
    assert collection.delete_calls == []


def test_write_failure_is_reported_and_other_updates_continue(monkeypatch: pytest.MonkeyPatch) -> None:
    class OneWriteFailsPaperTrades(FakePaperTrades):
        async def update_one(self, *args, **kwargs) -> SimpleNamespace:
            self.update_calls.append((args, kwargs))
            if args[0]["_id"] == "test-trade-0":
                raise RuntimeError("simulated write failure")
            return SimpleNamespace(modified_count=1)

    collection = OneWriteFailsPaperTrades(make_plans(2))
    fake_db = SimpleNamespace(paper_trades=collection)
    monkeypatch.setattr(paper, "get_database", lambda: fake_db)
    monkeypatch.setattr(paper, "TradingViewClient", FakeTradingViewClient)

    response = asyncio.run(paper.run_paper_trade_update(2, "1D", False, "test-write-failure", max_writes=2))

    failed_result = next(result for result in response["results"] if result["trade_id"] == "test-trade-0")
    successful_result = next(result for result in response["results"] if result["trade_id"] == "test-trade-1")
    assert response["blocked"] is False
    assert response["proposed_write_count"] == 2
    assert response["updated_count"] == 1
    assert response["successful_updates_count"] == 1
    assert response["errors_count"] == 1
    assert response["errors"][0]["trade_id"] == "test-trade-0"
    assert response["errors"][0]["symbol"] == "TEST0"
    assert response["errors"][0]["error_message"] == "simulated write failure"
    assert response["errors"][0]["error_stage"] == "AFTER_WRITE_ATTEMPT"
    assert response["errors"][0]["write_attempted"] is True
    assert failed_result["updated"] is False
    assert failed_result["error_stage"] == "AFTER_WRITE_ATTEMPT"
    assert successful_result["updated"] is True
    assert len(collection.update_calls) == 2


def test_evaluation_failure_is_reported_before_write_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    collection = FakePaperTrades(make_plans(1))
    fake_db = SimpleNamespace(paper_trades=collection)

    class EvaluationFailsTradingViewClient:
        def connect_to_debug_port(self) -> bool:
            raise RuntimeError("simulated evaluation failure")

    monkeypatch.setattr(paper, "get_database", lambda: fake_db)
    monkeypatch.setattr(paper, "TradingViewClient", EvaluationFailsTradingViewClient)

    response = asyncio.run(paper.run_paper_trade_update(1, "1D", False, "test-evaluation-failure", max_writes=1))

    result = response["results"][0]
    assert response["proposed_write_count"] == 0
    assert response["updated_count"] == 0
    assert response["successful_updates_count"] == 0
    assert response["errors_count"] == 1
    assert response["errors"][0]["trade_id"] == "test-trade-0"
    assert response["errors"][0]["error_message"] == "simulated evaluation failure"
    assert response["errors"][0]["error_stage"] == "BEFORE_WRITE_ATTEMPT"
    assert response["errors"][0]["write_attempted"] is False
    assert result["error_stage"] == "BEFORE_WRITE_ATTEMPT"
    assert result["write_attempted"] is False
    assert collection.update_calls == []
    assert collection.delete_calls == []


def test_max_trades_limits_processed_proposals(monkeypatch: pytest.MonkeyPatch) -> None:
    collection = FakePaperTrades(make_plans(3))
    fake_db = SimpleNamespace(paper_trades=collection)
    monkeypatch.setattr(paper, "get_database", lambda: fake_db)
    monkeypatch.setattr(paper, "TradingViewClient", FakeTradingViewClient)

    response = asyncio.run(paper.run_paper_trade_update(2, "1D", True, "test-max-trades", max_writes=2))

    assert response["max_trades"] == 2
    assert response["processed"] == 2
    assert response["proposed_write_count"] == 2
    assert len(response["results"]) == 2
    assert collection.update_calls == []
    assert collection.delete_calls == []


@pytest.mark.parametrize("status", ["STOPPED", "STOP_HIT", "TARGET_HIT", "CLOSED", "EXPIRED"])
def test_terminal_status_is_skipped_without_reprocessing(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    plan = make_plan(status)
    plan["paper_pnl"] = 42.0
    collection = FakePaperTrades([plan])
    fake_db = SimpleNamespace(paper_trades=collection)

    class FailIfCalledTradingViewClient:
        def __init__(self) -> None:
            raise AssertionError("Terminal trades must be skipped before TradingView access")

    monkeypatch.setattr(paper, "get_database", lambda: fake_db)
    monkeypatch.setattr(paper, "TradingViewClient", FailIfCalledTradingViewClient)

    response = asyncio.run(paper.run_paper_trade_update(1, "1D", True, "test-terminal-skip"))

    result = response["results"][0]
    assert response["updated_count"] == 0
    assert response["would_update_count"] == 0
    assert result["proposed_new_status"] == status
    assert result["proposed_new_outcome_status"] == status
    assert result["proposed_pnl"] == 42.0
    assert result["proposed_reason"] == "TERMINAL_STATUS"
    assert result["would_write"] is False
    assert collection.update_calls == []
    assert collection.delete_calls == []


def test_completed_target_2_status_is_skipped_without_reprocessing(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = make_plan("TARGET_2_HIT")
    plan["paper_pnl"] = 300.0
    collection = FakePaperTrades([plan])
    fake_db = SimpleNamespace(paper_trades=collection)

    class FailIfCalledTradingViewClient:
        def __init__(self) -> None:
            raise AssertionError("Completed target trades must be skipped before TradingView access")

    monkeypatch.setattr(paper, "get_database", lambda: fake_db)
    monkeypatch.setattr(paper, "TradingViewClient", FailIfCalledTradingViewClient)

    response = asyncio.run(paper.run_paper_trade_update(1, "1D", True, "test-target-2-skip"))

    result = response["results"][0]
    assert response["updated_count"] == 0
    assert response["would_update_count"] == 0
    assert result["proposed_new_status"] == "TARGET_2_HIT"
    assert result["proposed_new_outcome_status"] == "TARGET_2_HIT"
    assert result["proposed_pnl"] == 300.0
    assert result["proposed_reason"] == "TERMINAL_STATUS"
    assert result["would_write"] is False
    assert collection.update_calls == []
    assert collection.delete_calls == []


def test_active_stop_dry_run_proposes_transition_without_mongo_write(monkeypatch: pytest.MonkeyPatch) -> None:
    collection = FakePaperTrades([make_plan("ACTIVE")])
    fake_db = SimpleNamespace(paper_trades=collection)

    class StopHitTradingViewClient(FakeTradingViewClient):
        candle = make_candle(high=105.0, low=90.0, close=95.0)

    monkeypatch.setattr(paper, "get_database", lambda: fake_db)
    monkeypatch.setattr(paper, "TradingViewClient", StopHitTradingViewClient)

    response = asyncio.run(paper.run_paper_trade_update(1, "1D", True, "test-active-stop-dry-run"))

    result = response["results"][0]
    assert response["dry_run"] is True
    assert response["mongo_writes_enabled"] is False
    assert response["would_update_count"] == 1
    assert response["updated_count"] == 0
    assert result["proposed_new_status"] == "STOPPED"
    assert result["proposed_new_outcome_status"] == "STOPPED"
    assert result["proposed_pnl"] == pytest.approx(-100.0)
    assert result["proposed_reason"] == "STOP_LOSS_HIT"
    assert result["would_write"] is True
    assert collection.update_calls == []
    assert collection.delete_calls == []
