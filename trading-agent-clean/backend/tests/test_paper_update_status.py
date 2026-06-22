import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes import paper


def parsed_time(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def make_plan(status: str = "WAITING_FOR_ENTRY") -> dict:
    return {
        "_id": "test-trade-id",
        "symbol": "TEST",
        "tradingview_symbol": "NSE:TEST",
        "timeframe": "1D",
        "source_signal_type": "SWING_TV_CONFIRMED",
        "paper_only": True,
        "status": status,
        "outcome_status": status,
        "entry_triggered": status not in {"NOT_TRIGGERED", "PLANNED", "WAITING", "WAITING_FOR_ENTRY"},
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "target_1": 120.0,
        "target_2": 130.0,
        "target_3": 140.0,
        "quantity": 10,
        "state_version": 1,
        "paper_pnl": 0.0,
    }


def make_candle(*, high: float, low: float, close: float, previous_day_low: float | None = None) -> dict:
    candle = {
        "time": 1_700_000_000,
        "open": 100.0,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1_000,
    }
    if previous_day_low is not None:
        candle["previous_day_low"] = previous_day_low
    return candle


def make_market_row(
    symbol: str = "TEST",
    *,
    high: float = 100.0,
    low: float = 95.0,
    close: float = 101.0,
    previous_day_low: float | None = None,
) -> dict:
    row = {
        "exchange": "NSE",
        "symbol": symbol,
        "canonical_symbol": symbol,
        "tradingview_symbol": f"NSE:{symbol}",
        "day_high": high,
        "day_low": low,
        "current_price": close,
        "updated_at": "2026-06-16T10:00:00",
    }
    if previous_day_low is not None:
        row["previous_day_low"] = previous_day_low
    return row


def make_plans(count: int, status: str = "WAITING_FOR_ENTRY") -> list[dict]:
    plans = []
    for index in range(count):
        plan = make_plan(status)
        plan["_id"] = f"test-trade-{index}"
        plan["symbol"] = f"TEST{index}"
        plan["tradingview_symbol"] = f"NSE:TEST{index}"
        plans.append(plan)
    return plans


def make_snapshot_for_plan(plan: dict, *, high: float = 100.0, low: float = 95.0, close: float = 101.0) -> dict:
    return {
        "paper_only": True,
        "paper_trade_id": str(plan["_id"]),
        "symbol": plan["symbol"],
        "canonical_symbol": plan["symbol"],
        "observed_at": parsed_time("2026-06-16T10:00:00"),
        "observed_at_iso": "2026-06-16T10:00:00",
        "high": high,
        "low": low,
        "close": close,
        "price": close,
        "source": "test_snapshot",
    }


def test_build_plan_stores_initial_and_current_stop_from_previous_day_low() -> None:
    candles = [
        make_candle(high=101.0 + index, low=91.0 + index, close=96.0 + index)
        for index in range(9)
    ]
    candles.append(make_candle(high=110.0, low=98.0, close=105.0))
    signal = {
        "symbol": "TEST",
        "tradingview_symbol": "NSE:TEST",
        "timeframe": "1D",
        "signal_type": "SWING_TV_CONFIRMED",
        "paper_plan_valid": True,
        "paper_entry_price": 110.0,
        "paper_stop_loss": 90.0,
        "paper_target_1": 120.0,
        "paper_target_2": 130.0,
        "paper_target_3": 140.0,
    }

    plan = paper.build_plan_from_candles(signal, candles, 100000, 1)

    assert plan is not None
    assert plan["stop_loss"] == 99.0
    assert plan["initial_stop_loss"] == 99.0
    assert plan["current_stop_loss"] == 99.0
    assert plan["quantity_remaining"] == plan["quantity"]
    assert plan["partial_exit_1"] is None
    assert plan["partial_exit_2"] is None
    assert plan["partial_exit_3"] is None
    assert plan["risk_per_share"] == 11.0


def test_market_data_latest_row_carries_previous_day_low() -> None:
    latest = paper.market_data_latest_row(make_market_row(previous_day_low=92.5))

    assert latest is not None
    assert latest["previous_day_low"] == 92.5


def test_waiting_trade_not_touched_stays_waiting_for_entry() -> None:
    plan = make_plan()

    update = paper.update_plan_status(plan, make_candle(high=99.0, low=95.0, close=98.0))

    assert update == {}
    assert paper.proposed_update_reason(plan, update) == "WAITING_FOR_ENTRY"
    assert update.get("status", plan["status"]) == "WAITING_FOR_ENTRY"
    assert update.get("outcome_status", plan["outcome_status"]) == "WAITING_FOR_ENTRY"
    assert update.get("paper_pnl", plan["paper_pnl"]) == 0
    assert bool(update) is False


def test_waiting_trade_entry_triggered_becomes_active() -> None:
    plan = make_plan()

    update = paper.update_plan_status(plan, make_candle(high=100.0, low=95.0, close=101.0))

    assert update["status"] == "ACTIVE"
    assert update["outcome_status"] == "ACTIVE"
    assert update["entry_triggered"] is True
    assert update["quantity_remaining"] == 10
    assert update["exit_allocations"] == {
        "total_quantity": 10,
        "t1_quantity": 3,
        "t2_quantity": 3,
        "t3_quantity": 4,
        "valid": True,
    }
    assert update["exit_reason"] is None
    assert update["paper_pnl"] == pytest.approx(10.0)


def test_waiting_trade_stores_dynamic_stop_from_previous_day_low_without_entry() -> None:
    plan = make_plan()

    update = paper.update_plan_status(
        plan,
        make_candle(high=99.0, low=95.0, close=98.0, previous_day_low=92.5),
    )

    assert update["status"] == "WAITING_FOR_ENTRY"
    assert update["outcome_status"] == "WAITING_FOR_ENTRY"
    assert update["entry_triggered"] is False
    assert update["initial_stop_loss"] == 92.5
    assert update["current_stop_loss"] == 92.5
    assert update["stop_loss"] == 92.5


def test_waiting_trade_legacy_planned_alias_normalizes_to_waiting_for_entry() -> None:
    plan = make_plan("PLANNED")
    plan["entry_triggered"] = False

    update = paper.update_plan_status(plan, make_candle(high=99.0, low=95.0, close=98.0))

    assert paper.proposed_update_reason(plan, update) == "WAITING_FOR_ENTRY"
    assert update["status"] == "WAITING_FOR_ENTRY"
    assert update["outcome_status"] == "WAITING_FOR_ENTRY"
    assert update["entry_triggered"] is False
    assert update.get("paper_pnl", plan["paper_pnl"]) == 0


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

    assert update["status"] == "SL_HIT"
    assert update["outcome_status"] == "SL_HIT"
    assert update["exit_reason"] == "STOP_LOSS_HIT"
    assert update["exit_price"] == 90.0
    assert update["paper_pnl"] == pytest.approx(-100.0)
    assert update["paper_pnl_percent"] == pytest.approx(-10.0)


def test_active_trade_uses_current_stop_loss() -> None:
    plan = make_plan("ACTIVE")
    plan["initial_stop_loss"] = 90.0
    plan["current_stop_loss"] = 94.0

    update = paper.update_plan_status(plan, make_candle(high=105.0, low=94.0, close=95.0))

    assert update["status"] == "SL_HIT"
    assert update["outcome_status"] == "SL_HIT"
    assert update["exit_reason"] == "STOP_LOSS_HIT"
    assert update["exit_price"] == 94.0
    assert update["paper_pnl"] == pytest.approx(-60.0)
    assert update["paper_pnl_percent"] == pytest.approx(-6.0)


def test_active_trade_target_hit() -> None:
    plan = make_plan("ACTIVE")

    update = paper.update_plan_status(plan, make_candle(high=120.0, low=95.0, close=118.0))

    assert update["status"] == "T1_PARTIAL"
    assert update["outcome_status"] == "T1_PARTIAL"
    assert update["state"] == "T1_PARTIAL"
    assert update["exit_reason"] == "T1_PARTIAL"
    assert update["exit_price"] is None
    assert update["partial_exit_1"]["exit_id"] == "test-trade-id:T1"
    assert update["partial_exit_1"]["quantity"] == pytest.approx(3)
    assert update["partial_exit_1"]["percent"] == 33
    assert update["quantity_remaining"] == pytest.approx(7)
    assert update["current_stop_loss"] == 100.0
    assert update["paper_pnl"] == pytest.approx(186.0)
    assert update["paper_pnl_percent"] == pytest.approx(18.6)
    assert update["realized_pnl"] == pytest.approx(60.0)
    assert update["remaining_unrealized_pnl"] == pytest.approx(126.0)
    assert update["total_trade_pnl"] == pytest.approx(186.0)


def test_active_trade_above_all_targets_books_only_t1_once() -> None:
    plan = make_plan("ACTIVE")

    update = paper.update_plan_status(plan, make_candle(high=140.0, low=105.0, close=140.0))

    assert update["status"] == "AMBIGUOUS"
    assert update["outcome_status"] == "AMBIGUOUS"
    assert update["ambiguity_reason"] == "MULTIPLE_TARGETS_TOUCHED_SAME_CANDLE"
    assert update["ambiguity_touched_levels"] == ["target_1", "target_2", "target_3"]


def test_entry_and_stop_same_unresolved_candle_becomes_ambiguous() -> None:
    plan = make_plan("WAITING_FOR_ENTRY")

    update = paper.update_plan_status(plan, make_candle(high=100.0, low=90.0, close=95.0))

    assert update["status"] == "AMBIGUOUS"
    assert update["outcome_status"] == "AMBIGUOUS"
    assert update["ambiguity_reason"] == "ENTRY_AND_STOP_TOUCHED_SAME_CANDLE"
    assert update["ambiguity_previous_status"] == "WAITING_FOR_ENTRY"
    assert update["lower_timeframe_resolution_attempt"]["resolved"] is False


def test_entry_and_target_same_unresolved_candle_becomes_ambiguous() -> None:
    plan = make_plan("WAITING_FOR_ENTRY")

    update = paper.update_plan_status(plan, make_candle(high=120.0, low=95.0, close=118.0))

    assert update["status"] == "AMBIGUOUS"
    assert update["ambiguity_reason"] == "ENTRY_AND_TARGET_TOUCHED_SAME_CANDLE"
    assert update["ambiguity_touched_levels"] == ["entry", "target_1"]


def test_target_and_stop_same_unresolved_candle_becomes_ambiguous() -> None:
    plan = make_plan("ACTIVE")

    update = paper.update_plan_status(plan, make_candle(high=120.0, low=90.0, close=100.0))

    assert update["status"] == "AMBIGUOUS"
    assert update["ambiguity_reason"] == "STOP_AND_TARGET_TOUCHED_SAME_CANDLE"
    assert set(update["ambiguity_touched_levels"]) == {"stop_loss", "target_1"}


def test_lower_timeframe_candles_resolve_target_before_stop() -> None:
    plan = make_plan("ACTIVE")
    candle = make_candle(high=120.0, low=90.0, close=100.0)
    candle["lower_timeframe_candles"] = [
        {"time": "09:20", "high": 121.0, "low": 110.0, "close": 120.0},
        {"time": "09:25", "high": 118.0, "low": 90.0, "close": 95.0},
    ]

    update = paper.update_plan_status(plan, candle)

    assert update["status"] == "T1_PARTIAL"
    assert update["partial_exit_1"]["quantity"] == 3
    assert update["lower_timeframe_resolution_attempt"]["resolved"] is True


def test_lower_timeframe_candles_resolve_stop_before_target() -> None:
    plan = make_plan("ACTIVE")
    candle = make_candle(high=120.0, low=90.0, close=100.0)
    candle["lower_timeframe_candles"] = [
        {"time": "09:20", "high": 100.0, "low": 90.0, "close": 91.0},
        {"time": "09:25", "high": 121.0, "low": 95.0, "close": 120.0},
    ]

    update = paper.update_plan_status(plan, candle)

    assert update["status"] == "SL_HIT"
    assert update["stop_exit"]["quantity"] == 10
    assert update["lower_timeframe_resolution_attempt"]["resolved"] is True


def test_t1_partial_continues_open_without_stop_or_next_target() -> None:
    plan = make_plan("T1_PARTIAL")
    plan["current_stop_loss"] = 100.0
    plan["quantity_remaining"] = 7
    plan["partial_exit_1"] = {"exit_price": 120.0, "quantity": 3, "percent": 33, "paper_pnl": 60.0}

    update = paper.update_plan_status(plan, make_candle(high=125.0, low=110.0, close=124.0))

    assert update.get("status", plan["status"]) == "T1_PARTIAL"
    assert update.get("outcome_status", plan["outcome_status"]) == "T1_PARTIAL"
    assert update["exit_reason"] is None
    assert update["exit_price"] is None
    assert update["paper_pnl"] == pytest.approx(228.0)
    assert update["paper_pnl_percent"] == pytest.approx(22.8)


def test_t1_partial_reaches_target_2() -> None:
    plan = make_plan("T1_PARTIAL")
    plan["current_stop_loss"] = 100.0
    plan["quantity_remaining"] = 7
    plan["partial_exit_1"] = {"exit_price": 120.0, "quantity": 3, "percent": 33, "paper_pnl": 60.0}

    update = paper.update_plan_status(plan, make_candle(high=130.0, low=105.0, close=128.0))

    assert update["status"] == "T2_PARTIAL"
    assert update["outcome_status"] == "T2_PARTIAL"
    assert update["state"] == "T2_PARTIAL"
    assert update["exit_reason"] == "T2_PARTIAL"
    assert update["exit_price"] is None
    assert update["partial_exit_2"]["quantity"] == pytest.approx(3)
    assert update["quantity_remaining"] == pytest.approx(4)
    assert update["current_stop_loss"] == 120.0
    assert update["paper_pnl"] == pytest.approx(262.0)
    assert update["paper_pnl_percent"] == pytest.approx(26.2)


def test_t1_partial_stop_closes_remaining_at_entry() -> None:
    plan = make_plan("T1_PARTIAL")

    update = paper.update_plan_status(plan, make_candle(high=125.0, low=100.0, close=101.0))

    assert update["status"] == "SL_HIT"
    assert update["outcome_status"] == "SL_HIT"
    assert update["state"] == "SL_HIT"
    assert update["exit_reason"] == "STOP_LOSS_HIT"
    assert update["exit_price"] == 100.0
    assert update["quantity_remaining"] == 0
    assert update["partial_exit_1"]["quantity"] == pytest.approx(3)
    assert update["stop_exit"]["quantity"] == pytest.approx(7)
    assert update["current_stop_loss"] == 100.0
    assert update["paper_pnl"] == pytest.approx(60.0)
    assert update["paper_pnl_percent"] == pytest.approx(6.0)


def test_t2_partial_stop_closes_remaining_at_target_1() -> None:
    plan = make_plan("T2_PARTIAL")

    update = paper.update_plan_status(plan, make_candle(high=135.0, low=120.0, close=121.0))

    assert update["status"] == "SL_HIT"
    assert update["outcome_status"] == "SL_HIT"
    assert update["state"] == "SL_HIT"
    assert update["exit_price"] == 120.0
    assert update["quantity_remaining"] == 0
    assert update["partial_exit_1"]["quantity"] == pytest.approx(3)
    assert update["partial_exit_2"]["quantity"] == pytest.approx(3)
    assert update["stop_exit"]["quantity"] == pytest.approx(4)
    assert update["current_stop_loss"] == 120.0
    assert update["paper_pnl"] == pytest.approx(230.0)
    assert update["paper_pnl_percent"] == pytest.approx(23.0)


def test_t3_hit_exits_remaining_and_completes_trade() -> None:
    plan = make_plan("T2_PARTIAL")
    plan["current_stop_loss"] = 120.0
    plan["quantity_remaining"] = 4
    plan["partial_exit_1"] = {"exit_price": 120.0, "quantity": 3, "percent": 33, "paper_pnl": 60.0}
    plan["partial_exit_2"] = {"exit_price": 130.0, "quantity": 3, "percent": 33, "paper_pnl": 90.0}

    update = paper.update_plan_status(plan, make_candle(high=140.0, low=125.0, close=140.0))

    assert update["status"] == "COMPLETED"
    assert update["outcome_status"] == "T3_HIT"
    assert update["state"] == "T3_HIT"
    assert update["exit_reason"] == "T3_HIT"
    assert update["exit_price"] == 140.0
    assert update["quantity_remaining"] == 0
    assert "partial_exit_1" not in update
    assert "partial_exit_2" not in update
    assert update["partial_exit_3"]["percent"] == 34
    assert update["partial_exit_3"]["quantity"] == pytest.approx(4)
    assert update["paper_pnl"] == pytest.approx(310.0)
    assert update["paper_pnl_percent"] == pytest.approx(31.0)
    assert update["initial_risk_amount"] == pytest.approx(100.0)
    assert update["realized_rr"] == pytest.approx(3.1)
    assert update["journal_pending"] is True


def test_completed_trade_dynamic_stop_is_protected() -> None:
    plan = make_plan("COMPLETED")
    plan["paper_pnl"] = 42.0

    update = paper.update_plan_status(
        plan,
        make_candle(high=140.0, low=80.0, close=90.0, previous_day_low=92.5),
    )

    assert update == {}


def apply_atomic_update_once(row: dict, query: dict, update: dict) -> int:
    if any(row.get(key) != value for key, value in query.items()):
        return 0
    row.update(update.get("$set", {}))
    for key, value in update.get("$inc", {}).items():
        row[key] = row.get(key, 0) + value
    return 1


def test_concurrent_entry_attempts_allow_one_success() -> None:
    row = make_plan("WAITING_FOR_ENTRY")
    update = paper.update_plan_status(row.copy(), make_candle(high=100.0, low=95.0, close=101.0))
    query = paper.atomic_trade_update_filter(row)
    mutation = paper.state_transition_update(update)

    first = apply_atomic_update_once(row, query, mutation)
    second = apply_atomic_update_once(row, query, mutation)

    assert query == {
        "_id": "test-trade-id",
        "paper_only": True,
        "status": "WAITING_FOR_ENTRY",
        "state_version": 1,
    }
    assert first == 1
    assert second == 0
    assert row["status"] == "ACTIVE"
    assert row["state_version"] == 2


def test_concurrent_t1_attempts_create_one_partial_exit() -> None:
    row = make_plan("ACTIVE")
    update = paper.update_plan_status(row.copy(), make_candle(high=120.0, low=95.0, close=118.0))
    query = paper.atomic_trade_update_filter(row)
    mutation = paper.state_transition_update(update)

    first = apply_atomic_update_once(row, query, mutation)
    second = apply_atomic_update_once(row, query, mutation)

    assert first == 1
    assert second == 0
    assert row["partial_exit_1"]["exit_id"] == "test-trade-id:T1"
    assert row["state_version"] == 2


def test_repeated_t2_processing_does_not_duplicate_exit() -> None:
    plan = make_plan("T2_PARTIAL")
    plan["quantity_remaining"] = 4
    plan["partial_exit_1"] = {"exit_id": "test-trade-id:T1", "exit_price": 120.0, "quantity": 3, "percent": 33, "paper_pnl": 60.0}
    plan["partial_exit_2"] = {"exit_id": "test-trade-id:T2", "exit_price": 130.0, "quantity": 3, "percent": 33, "paper_pnl": 90.0}

    update = paper.update_plan_status(plan, make_candle(high=135.0, low=125.0, close=132.0))

    assert "partial_exit_2" not in update
    assert update["paper_pnl"] == pytest.approx(278.0)


def test_repeated_t3_processing_does_not_duplicate_exit() -> None:
    plan = make_plan("COMPLETED")
    plan["outcome_status"] = "T3_HIT"
    plan["partial_exit_3"] = {"exit_id": "test-trade-id:T3", "exit_price": 140.0, "quantity": 4, "percent": 34, "paper_pnl": 160.0}

    update = paper.update_plan_status(plan, make_candle(high=145.0, low=130.0, close=140.0))

    assert update == {}


def test_automatic_outcome_one_failed_trade_does_not_stop_later_trade(monkeypatch: pytest.MonkeyPatch) -> None:
    fail = make_plan("ACTIVE")
    fail["_id"] = "fail-id"
    fail["symbol"] = "FAIL"
    fail["tradingview_symbol"] = "NSE:FAIL"
    passed = make_plan("ACTIVE")
    passed["_id"] = "pass-id"
    passed["symbol"] = "PASS"
    passed["tradingview_symbol"] = "NSE:PASS"
    collection = FakePaperTrades([fail, passed])
    fake_db = fake_db_for(collection, [])

    async def fake_fetch(_db, trade):
        if trade["symbol"] == "FAIL":
            raise RuntimeError("simulated market failure")
        return make_market_row("PASS", high=120.0, low=95.0, close=118.0), make_candle(high=120.0, low=95.0, close=118.0)

    monkeypatch.setattr(paper, "fetch_trade_market_latest", fake_fetch)

    response = asyncio.run(paper.run_automatic_outcome_update(db_override=fake_db))

    assert response["processed"] == 2
    assert response["updated_count"] == 1
    assert response["errors_count"] == 1
    assert response["errors"][0]["symbol"] == "FAIL"
    assert "traceback" not in response["errors"][0]
    assert len(collection.update_calls) == 1
    assert response["results"][0]["symbol"] == "PASS"
    assert response["results"][0]["updated"] is True
    assert fake_db.system_errors.rows[0]["component"] == "paper_outcome_engine"
    assert fake_db.system_errors.rows[0]["symbol"] == "FAIL"
    assert fake_db.system_errors.rows[0]["occurrence_count"] == 1


def test_automatic_outcome_succeeds_when_tradingview_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    collection = FakePaperTrades([make_plan("ACTIVE")])
    fake_db = fake_db_for(collection, [make_market_row("TEST", high=120.0, low=95.0, close=118.0)])

    class FailIfCalledTradingViewClient:
        def __init__(self) -> None:
            raise AssertionError("Automatic outcomes must use market data only")

    monkeypatch.setattr(paper, "TradingViewClient", FailIfCalledTradingViewClient)

    response = asyncio.run(paper.run_automatic_outcome_update(db_override=fake_db))

    assert response["processed"] == 1
    assert response["updated_count"] == 1
    assert response["errors_count"] == 0
    assert response["results"][0]["symbol"] == "TEST"
    assert response["results"][0]["status"] == "T1_PARTIAL"


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


class FakeMarketData:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def find_one(self, query: dict | None = None, *_args, **_kwargs) -> dict | None:
        row = next(
            (
                row
                for row in self.rows
                if all(row.get(key) == value for key, value in (query or {}).items())
            ),
            None,
        )
        return row.copy() if row else None


class FakePaperMarketSnapshots:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.update_calls = []

    def find(self, query: dict | None = None, *_args, **_kwargs) -> FakeCursor:
        def row_matches(row: dict) -> bool:
            for key, expected in (query or {}).items():
                actual = row.get(key)
                if isinstance(expected, dict) and "$gte" in expected:
                    if actual is None or parsed_time(actual) < parsed_time(expected["$gte"]):
                        return False
                elif actual != expected:
                    return False
            return True

        return FakeCursor([row.copy() for row in self.rows if row_matches(row)])

    async def update_one(self, query: dict, update: dict, upsert: bool = False) -> SimpleNamespace:
        self.update_calls.append((query, update, upsert))
        row = next((candidate for candidate in self.rows if all(candidate.get(key) == value for key, value in query.items())), None)
        if row is not None:
            return SimpleNamespace(modified_count=0, matched_count=1, upserted_id=None)
        if not upsert:
            return SimpleNamespace(modified_count=0, matched_count=0, upserted_id=None)
        new_row = {**query, **update.get("$setOnInsert", {})}
        self.rows.append(new_row)
        return SimpleNamespace(modified_count=0, matched_count=0, upserted_id=f"snapshot-{len(self.rows)}")


class FakeSystemErrors:
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
        row = next((candidate for candidate in self.rows if all(candidate.get(key) == value for key, value in query.items())), None)
        if row is None:
            row = {**query, **update.get("$setOnInsert", {})}
            self.rows.append(row)
        row.update(update.get("$set", {}))
        for key, value in update.get("$inc", {}).items():
            row[key] = row.get(key, 0) + value
        return SimpleNamespace(modified_count=1, matched_count=1, upserted_id=None)


class FakeTradingViewClient:
    candle = make_candle(high=100.0, low=95.0, close=101.0)

    def connect_to_debug_port(self) -> bool:
        return True

    def open_symbol(self, _symbol: str) -> dict:
        return {}

    def fetch_candles(self, _timeframe: str, min_candles: int = 1) -> list[dict]:
        assert min_candles == 1
        return [self.candle]


def fake_db_for(collection: FakePaperTrades, market_rows: list[dict] | None = None, snapshots: list[dict] | None = None) -> SimpleNamespace:
    rows = market_rows if market_rows is not None else [
        make_market_row(row["symbol"], high=100.0, low=95.0, close=101.0)
        for row in collection.rows
    ]
    snapshot_rows = snapshots if snapshots is not None else [make_snapshot_for_plan(row) for row in collection.rows]
    return SimpleNamespace(
        paper_trades=collection,
        market_data=FakeMarketData(rows),
        paper_market_snapshots=FakePaperMarketSnapshots(snapshot_rows),
        system_errors=FakeSystemErrors(),
    )


def test_dry_run_proposes_transition_without_mongo_write(monkeypatch: pytest.MonkeyPatch) -> None:
    collection = FakePaperTrades([make_plan()])
    fake_db = fake_db_for(collection)
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


def test_real_mode_requires_approval_before_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
    collection = FakePaperTrades(make_plans(2))
    fake_db = SimpleNamespace(paper_trades=collection)
    monkeypatch.setattr(paper, "get_database", lambda: fake_db)
    monkeypatch.setattr(paper, "TradingViewClient", FakeTradingViewClient)

    response = asyncio.run(paper.run_paper_trade_update(2, "1D", False, "test-write-limit", max_writes=1))

    assert response["processed"] == 0
    assert response["proposed_write_count"] == 0
    assert response["updated_count"] == 0
    assert response["successful_updates_count"] == 0
    assert response["blocked"] is True
    assert response["block_reason"] == "APPROVAL_REQUIRED"
    assert response["mongo_writes_enabled"] is False
    assert response["results"] == []
    assert collection.update_calls == []
    assert collection.delete_calls == []


def test_dry_run_reports_when_real_mode_would_be_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    collection = FakePaperTrades(make_plans(2))
    fake_db = fake_db_for(collection)
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


def test_real_mode_does_not_write_without_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    collection = FakePaperTrades(make_plans(1))
    fake_db = SimpleNamespace(paper_trades=collection)
    monkeypatch.setattr(paper, "get_database", lambda: fake_db)
    monkeypatch.setattr(paper, "TradingViewClient", FakeTradingViewClient)

    response = asyncio.run(paper.run_paper_trade_update(1, "1D", False, "test-allowed-write", max_writes=1))

    assert response["proposed_write_count"] == 0
    assert response["updated_count"] == 0
    assert response["successful_updates_count"] == 0
    assert response["errors_count"] == 0
    assert response["blocked"] is True
    assert response["block_reason"] == "APPROVAL_REQUIRED"
    assert response["mongo_writes_enabled"] is False
    assert response["results"] == []
    assert collection.update_calls == []
    assert collection.delete_calls == []


def test_unbound_real_mode_does_not_attempt_failing_writes(monkeypatch: pytest.MonkeyPatch) -> None:
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

    assert response["blocked"] is True
    assert response["block_reason"] == "APPROVAL_REQUIRED"
    assert response["updated_count"] == 0
    assert response["errors_count"] == 0
    assert response["results"] == []
    assert collection.update_calls == []


def test_unbound_real_mode_does_not_attempt_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
    collection = FakePaperTrades(make_plans(1))
    fake_db = SimpleNamespace(paper_trades=collection)

    class EvaluationFailsTradingViewClient:
        def connect_to_debug_port(self) -> bool:
            raise RuntimeError("simulated evaluation failure")

    monkeypatch.setattr(paper, "get_database", lambda: fake_db)
    monkeypatch.setattr(paper, "TradingViewClient", EvaluationFailsTradingViewClient)

    response = asyncio.run(paper.run_paper_trade_update(1, "1D", False, "test-evaluation-failure", max_writes=1))

    assert response["proposed_write_count"] == 0
    assert response["updated_count"] == 0
    assert response["successful_updates_count"] == 0
    assert response["errors_count"] == 0
    assert response["blocked"] is True
    assert response["block_reason"] == "APPROVAL_REQUIRED"
    assert response["results"] == []
    assert collection.update_calls == []
    assert collection.delete_calls == []


def test_max_trades_limits_processed_proposals(monkeypatch: pytest.MonkeyPatch) -> None:
    collection = FakePaperTrades(make_plans(3))
    fake_db = fake_db_for(collection)
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
    fake_db = fake_db_for(collection, [make_market_row("TEST", high=105.0, low=90.0, close=95.0)])

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
    assert result["proposed_new_status"] == "SL_HIT"
    assert result["proposed_new_outcome_status"] == "SL_HIT"
    assert result["proposed_pnl"] == pytest.approx(-100.0)
    assert result["proposed_reason"] == "STOP_LOSS_HIT"
    assert result["would_write"] is True
    assert collection.update_calls == []
    assert collection.delete_calls == []
