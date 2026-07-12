import asyncio
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.daily_dataset_contract import (
    ACTION_PENDING,
    ACTION_STRATEGY_REJECTED,
    ACTION_TAKE_TRADE,
    ACTION_TECHNICAL_FAILED,
    ACTION_WAIT_FOR_PULLBACK,
    GRADE_A,
    GRADE_B,
    GRADE_NO_GRADE,
    STAGE_TV_CONFIRMATION,
    STRATEGY_MOMENTUM,
    STRATEGY_SWING,
    TRAP_CLEAN,
    TRAP_UNKNOWN,
    TV_STATUS_MOMENTUM_CONFIRMED,
    TV_STATUS_REJECTED,
    TV_STATUS_TECHNICAL_FAILED,
    TV_STATUS_WAIT_FOR_PULLBACK,
)
from services.daily_dataset import (
    build_daily_dataset_candidate_row,
    normalize_decision_snapshot,
    normalize_trade_plan_snapshot,
    normalize_tv_confirmation_snapshot,
    update_daily_dataset_from_confirmation,
)


FIXED_NOW = "2026-07-08T10:45:00.000000Z"
SOURCE_CANDLE_AT = "2026-07-08T10:00:00+00:00"


def run(coro):
    return asyncio.run(coro)


def nested_get(row: dict, dotted_key: str):
    current = row
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def nested_set(row: dict, dotted_key: str, value):
    current = row
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = deepcopy(value)


def matches_query(row: dict, query: dict | None) -> bool:
    if not query:
        return True
    for key, expected in query.items():
        if key == "$or":
            if not any(matches_query(row, option) for option in expected):
                return False
            continue
        if nested_get(row, key) != expected:
            return False
    return True


class FakeDatasetCollection:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = [deepcopy(row) for row in rows or []]
        self.update_calls = []

    async def find_one(self, query: dict, *_args, **_kwargs):
        row = next((candidate for candidate in self.rows if matches_query(candidate, query)), None)
        return deepcopy(row) if row else None

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        self.update_calls.append((deepcopy(query), deepcopy(update), upsert))
        row = next((candidate for candidate in self.rows if matches_query(candidate, query)), None)
        if row is None:
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)
        for key, value in update.get("$set", {}).items():
            nested_set(row, key, value)
        for key, value in update.get("$push", {}).items():
            current = nested_get(row, key)
            if current is None:
                nested_set(row, key, [])
                current = nested_get(row, key)
            current.append(deepcopy(value))
        return SimpleNamespace(matched_count=1, modified_count=1, upserted_id=None)


class FakeSavedConfirmationCollection:
    def __init__(self) -> None:
        self.rows = []
        self.update_calls = []

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        self.update_calls.append((deepcopy(query), deepcopy(update), upsert))
        document = deepcopy(update.get("$set", {}))
        for key, value in update.get("$setOnInsert", {}).items():
            document.setdefault(key, deepcopy(value))
        for key in update.get("$unset", {}):
            document.pop(key, None)
        self.rows.append(document)
        return SimpleNamespace(matched_count=0, modified_count=1, upserted_id=f"saved-{len(self.rows)}")


class FakeDb:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.daily_trade_dataset = FakeDatasetCollection(rows)

    def __getitem__(self, name: str):
        return getattr(self, name)


class FakeRouteDb(FakeDb):
    def __init__(self, rows: list[dict] | None = None) -> None:
        super().__init__(rows)
        self.swing_tv_confirmations = FakeSavedConfirmationCollection()
        self.momentum_tv_confirmations = FakeSavedConfirmationCollection()


def scored_candidate(**overrides):
    row = {
        "_id": "scored-1",
        "exchange": "NSE",
        "symbol": "TEST",
        "canonical_symbol": "TEST",
        "tradingview_symbol": "NSE:TEST",
        "index_name": "BROAD_MARKET_750",
        "current_price": 101.5,
        "previous_close": 99.0,
        "open_price": 100.0,
        "day_high": 103.0,
        "day_low": 98.5,
        "traded_volume": 123456,
        "traded_value": 12345678,
        "change_percent": 2.52,
        "relative_volume": 1.8,
        "thirty_day_change_percent": 12.4,
        "source_used": "test",
        "updated_at": SOURCE_CANDLE_AT,
        "score_version": "score_v1",
        "score": 83.0,
        "selected_for_tv": True,
        "swing_candidate": True,
        "swing_status": "SWING_SELECTED_FOR_TV",
        "momentum_score": 74.0,
        "momentum_candidate": True,
        "momentum_status": "MOMENTUM_PRECHECK_PASSED",
    }
    row.update(overrides)
    return row


def dataset_row(strategy_type=STRATEGY_MOMENTUM, **candidate_overrides):
    return build_daily_dataset_candidate_row(
        scored_candidate(**candidate_overrides),
        strategy_type=strategy_type,
        trade_date="2026-07-08",
        audit_time="2026-07-08T10:30:00.000000Z",
    )


def confirmation(**overrides):
    row = {
        "_id": "confirm-1",
        "symbol": "TEST",
        "canonical_symbol": "TEST",
        "tradingview_symbol": "NSE:TEST",
        "strategy_type": "momentum",
        "tv_status": "MOMENTUM_CONFIRMED",
        "trade_quality_grade": "A",
        "quality_score": 84,
        "trap_status": "CLEAN",
        "confidence_score": 82,
        "reason": "clean breakout",
        "confirmed_at": "2026-07-08T10:40:00+00:00",
        "source_candle_at": SOURCE_CANDLE_AT,
        "paper_entry_price": 102.0,
        "paper_stop_loss": 98.0,
        "paper_target_1": 110.0,
        "paper_target_2": 116.0,
        "paper_target_3": 124.0,
        "paper_rr_1": 2.0,
        "paper_rr_2": 3.5,
        "paper_rr_3": 5.5,
    }
    row.update(overrides)
    return row


def test_momentum_confirmed_grade_a_maps_to_take_trade():
    db = FakeDb([dataset_row()])

    result = run(update_daily_dataset_from_confirmation(db, confirmation(), audit_time=FIXED_NOW))

    assert result["updated_count"] == 1
    stored = db.daily_trade_dataset.rows[0]
    assert nested_get(stored, "identity.current_stage") == STAGE_TV_CONFIRMATION
    assert nested_get(stored, "tv_confirmation_snapshot.status") == TV_STATUS_MOMENTUM_CONFIRMED
    assert nested_get(stored, "decision_snapshot.action") == ACTION_TAKE_TRADE
    assert nested_get(stored, "decision_snapshot.trade_quality_grade") == GRADE_A
    assert nested_get(stored, "decision_snapshot.trap_status") == TRAP_CLEAN


def test_momentum_wait_for_pullback_grade_b_maps_to_wait():
    db = FakeDb([dataset_row()])

    result = run(
        update_daily_dataset_from_confirmation(
            db,
            confirmation(tv_status="WAIT_FOR_PULLBACK", trade_quality_grade="B", trap_status="CAUTION"),
            audit_time=FIXED_NOW,
        )
    )

    assert result["updated_count"] == 1
    stored = db.daily_trade_dataset.rows[0]
    assert nested_get(stored, "tv_confirmation_snapshot.status") == TV_STATUS_WAIT_FOR_PULLBACK
    assert nested_get(stored, "decision_snapshot.action") == ACTION_WAIT_FOR_PULLBACK
    assert nested_get(stored, "decision_snapshot.trade_quality_grade") == GRADE_B


def test_momentum_rejected_maps_to_strategy_rejected_and_preserves_no_trade_reason():
    db = FakeDb([dataset_row()])

    result = run(
        update_daily_dataset_from_confirmation(
            db,
            confirmation(tv_status="REJECTED", next_action_for_paper_trade="NO_PAPER_TRADE", reason="weak follow through"),
            audit_time=FIXED_NOW,
        )
    )

    assert result["updated_count"] == 1
    stored = db.daily_trade_dataset.rows[0]
    assert nested_get(stored, "tv_confirmation_snapshot.status") == TV_STATUS_REJECTED
    assert nested_get(stored, "decision_snapshot.action") == ACTION_STRATEGY_REJECTED
    assert nested_get(stored, "decision_snapshot.source_action") == "NO_PAPER_TRADE"
    assert nested_get(stored, "decision_snapshot.no_trade_reason") == "weak follow through"
    assert nested_get(stored, "decision_snapshot.rejected_reason") == "weak follow through"


def test_technical_failed_maps_to_technical_failed_and_label_placeholder_stays_pending():
    db = FakeDb([dataset_row(strategy_type=STRATEGY_SWING)])

    result = run(
        update_daily_dataset_from_confirmation(
            db,
            confirmation(
                strategy_type="swing",
                tv_status="TECHNICAL_FAILED",
                reason="TV_CONFIRMATION_EXCEPTION",
                error="chart unavailable",
            ),
            strategy_type=STRATEGY_SWING,
            audit_time=FIXED_NOW,
        )
    )

    assert result["updated_count"] == 1
    stored = db.daily_trade_dataset.rows[0]
    assert nested_get(stored, "tv_confirmation_snapshot.status") == TV_STATUS_TECHNICAL_FAILED
    assert nested_get(stored, "decision_snapshot.action") == ACTION_TECHNICAL_FAILED
    assert nested_get(stored, "ml_label.label_state") == "PENDING"


def test_swing_confirmed_maps_correctly():
    db = FakeDb([dataset_row(strategy_type=STRATEGY_SWING)])

    result = run(
        update_daily_dataset_from_confirmation(
            db,
            confirmation(strategy_type="swing", tv_status="CONFIRMED_SIGNAL"),
            strategy_type=STRATEGY_SWING,
            audit_time=FIXED_NOW,
        )
    )

    assert result["updated_count"] == 1
    stored = db.daily_trade_dataset.rows[0]
    assert nested_get(stored, "tv_confirmation_snapshot.status") == "CONFIRMED_SIGNAL"
    assert nested_get(stored, "decision_snapshot.action") == ACTION_TAKE_TRADE


def test_confirmation_update_does_not_overwrite_immutable_snapshots():
    db = FakeDb([dataset_row()])

    run(
        update_daily_dataset_from_confirmation(
            db,
            confirmation(current_price=999.0, score=1.0, source_used="changed"),
            audit_time=FIXED_NOW,
        )
    )

    stored = db.daily_trade_dataset.rows[0]
    assert nested_get(stored, "raw_market_snapshot.current_price") == 101.5
    assert nested_get(stored, "score_snapshot.strategy_score") == 74.0


def test_unmatched_confirmation_returns_unmatched_and_does_not_create_duplicate_row():
    db = FakeDb([])

    result = run(update_daily_dataset_from_confirmation(db, confirmation(), audit_time=FIXED_NOW))

    assert result["updated_count"] == 0
    assert result["unmatched_count"] == 1
    assert len(db.daily_trade_dataset.rows) == 0


def test_matching_by_setup_id_works():
    db = FakeDb([dataset_row(setup_id="setup-123")])

    result = run(update_daily_dataset_from_confirmation(db, confirmation(setup_id="setup-123"), audit_time=FIXED_NOW))

    assert result["updated_count"] == 1
    assert nested_get(db.daily_trade_dataset.rows[0], "decision_snapshot.action") == ACTION_TAKE_TRADE


def test_matching_by_symbol_date_strategy_source_candle_fallback_works():
    db = FakeDb([dataset_row()])

    result = run(update_daily_dataset_from_confirmation(db, confirmation(), audit_time=FIXED_NOW))

    assert result["updated_count"] == 1
    assert len(db.daily_trade_dataset.update_calls) == 1


def test_trade_plan_fields_are_captured_when_present():
    plan = normalize_trade_plan_snapshot(confirmation(position_side="LONG"))

    assert plan == {
        "entry_price": 102.0,
        "stop_loss": 98.0,
        "target_1": 110.0,
        "target_2": 116.0,
        "target_3": 124.0,
        "risk_reward_1": 2.0,
        "risk_reward_2": 3.5,
        "risk_reward_3": 5.5,
        "position_side": "LONG",
        "plan_source": "tv_confirmation",
    }


def test_unknown_status_action_grade_trap_normalization_is_safe():
    row = confirmation(tv_status="STRANGE_STATUS", trade_quality_grade="???", trap_status="mystery")

    tv_snapshot = normalize_tv_confirmation_snapshot(row)
    decision = normalize_decision_snapshot(row)

    assert tv_snapshot["status"] == "PENDING"
    assert decision["action"] == ACTION_PENDING
    assert decision["trade_quality_grade"] == GRADE_NO_GRADE
    assert decision["trap_status"] == TRAP_UNKNOWN


def test_momentum_save_confirmation_updates_matching_daily_dataset_row(monkeypatch):
    from routes import momentum

    db = FakeRouteDb([dataset_row()])
    monkeypatch.setattr(momentum, "get_database", lambda: db)

    result = run(momentum.save_momentum_confirmation_row(confirmation(index_name="BROAD_MARKET_750")))

    assert result["updated_count"] == 1
    assert len(db.momentum_tv_confirmations.rows) == 1
    stored = db.daily_trade_dataset.rows[0]
    assert nested_get(stored, "identity.current_stage") == STAGE_TV_CONFIRMATION
    assert nested_get(stored, "tv_confirmation_snapshot.status") == TV_STATUS_MOMENTUM_CONFIRMED
    assert nested_get(stored, "decision_snapshot.action") == ACTION_TAKE_TRADE
    assert nested_get(stored, "trade_plan_snapshot.entry_price") == 102.0


def test_swing_save_confirmation_updates_matching_daily_dataset_row(monkeypatch):
    from routes import swing

    db = FakeRouteDb([dataset_row(strategy_type=STRATEGY_SWING)])
    monkeypatch.setattr(swing, "get_database", lambda: db)

    result = run(
        swing.save_confirmation_row(
            confirmation(strategy_type="swing", tv_status="CONFIRMED_SIGNAL", index_name="BROAD_MARKET_750")
        )
    )

    assert result["updated_count"] == 1
    assert len(db.swing_tv_confirmations.rows) == 1
    stored = db.daily_trade_dataset.rows[0]
    assert nested_get(stored, "identity.current_stage") == STAGE_TV_CONFIRMATION
    assert nested_get(stored, "tv_confirmation_snapshot.status") == "CONFIRMED_SIGNAL"
    assert nested_get(stored, "decision_snapshot.action") == ACTION_TAKE_TRADE


def test_swing_run_save_false_does_not_update_daily_dataset_and_keeps_response_fields(monkeypatch):
    from routes import swing

    class FakeTradingViewManager:
        attached_target_id = "tab-1"

        def get_preflight_status(self):
            return {"operation_allowed": True}

        async def run_operation(self, *_args, **_kwargs):
            return confirmation(strategy_type="swing", tv_status="CONFIRMED_SIGNAL")

        def clear_attachment_if_target(self, *_args, **_kwargs):
            return None

    async def fake_load_single_candidate(*_args, **_kwargs):
        return [scored_candidate(strategy_type="swing")]

    db = FakeRouteDb([dataset_row(strategy_type=STRATEGY_SWING)])
    monkeypatch.setattr(swing, "get_database", lambda: db)
    monkeypatch.setattr(swing, "tradingview_manager", FakeTradingViewManager())
    monkeypatch.setattr(swing, "load_single_swing_candidate", fake_load_single_candidate)

    response = run(
        swing.run_swing_tv_confirmation(
            "BROAD_MARKET_750",
            1,
            ["1D"],
            False,
            single_symbol=True,
            symbol="TEST",
        )
    )

    assert response["saved"] is False
    assert response["rows_count"] == 1
    assert "confirmed_count" in response
    assert "rejected_count" in response
    assert "technical_failed_count" in response
    assert "daily_dataset_update" not in response
    assert db.daily_trade_dataset.update_calls == []
    assert db.swing_tv_confirmations.rows == []


def test_swing_run_save_true_includes_daily_dataset_update_summary(monkeypatch):
    from routes import swing

    class FakeTradingViewManager:
        attached_target_id = "tab-1"

        def get_preflight_status(self):
            return {"operation_allowed": True}

        async def run_operation(self, *_args, **_kwargs):
            return confirmation(strategy_type="swing", tv_status="CONFIRMED_SIGNAL")

        def clear_attachment_if_target(self, *_args, **_kwargs):
            return None

    async def fake_load_single_candidate(*_args, **_kwargs):
        return [scored_candidate(strategy_type="swing")]

    db = FakeRouteDb([dataset_row(strategy_type=STRATEGY_SWING)])
    monkeypatch.setattr(swing, "get_database", lambda: db)
    monkeypatch.setattr(swing, "tradingview_manager", FakeTradingViewManager())
    monkeypatch.setattr(swing, "load_single_swing_candidate", fake_load_single_candidate)

    response = run(
        swing.run_swing_tv_confirmation(
            "BROAD_MARKET_750",
            1,
            ["1D"],
            True,
            single_symbol=True,
            symbol="TEST",
        )
    )

    assert response["saved"] is True
    assert response["rows_count"] == 1
    assert response["daily_dataset_update"]["processed_count"] == 1
    assert response["daily_dataset_update"]["updated_count"] == 1
    assert len(db.swing_tv_confirmations.rows) == 1
    assert len(db.daily_trade_dataset.update_calls) == 1


def test_unmatched_dataset_row_does_not_fail_confirmation_save(monkeypatch):
    from routes import momentum

    db = FakeRouteDb([])
    monkeypatch.setattr(momentum, "get_database", lambda: db)

    result = run(momentum.save_momentum_confirmation_row(confirmation(index_name="BROAD_MARKET_750")))

    assert result["updated_count"] == 0
    assert result["unmatched_count"] == 1
    assert len(db.daily_trade_dataset.rows) == 0
    assert len(db.momentum_tv_confirmations.rows) == 1


def test_dataset_update_exception_does_not_break_confirmation_save(monkeypatch):
    from routes import swing

    async def fail_dataset_update(*_args, **_kwargs):
        raise RuntimeError("dataset write unavailable")

    db = FakeRouteDb([dataset_row(strategy_type=STRATEGY_SWING)])
    monkeypatch.setattr(swing, "get_database", lambda: db)
    monkeypatch.setattr(swing, "update_daily_dataset_from_confirmation", fail_dataset_update)

    result = run(
        swing.save_confirmation_row(
            confirmation(strategy_type="swing", tv_status="CONFIRMED_SIGNAL", index_name="BROAD_MARKET_750")
        )
    )

    assert result["error_count"] == 1
    assert result["updated_count"] == 0
    assert "RuntimeError: dataset write unavailable" in result["validation_errors"][0]["errors"]
    assert len(db.swing_tv_confirmations.rows) == 1
    assert db.daily_trade_dataset.update_calls == []
