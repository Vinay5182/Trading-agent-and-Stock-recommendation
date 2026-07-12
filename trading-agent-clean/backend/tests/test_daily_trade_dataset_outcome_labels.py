import asyncio
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.daily_dataset_contract import (
    ACTION_NO_TRADE,
    ACTION_TAKE_TRADE,
    LABEL_CONFIRMED_TRADE_HIT_SL,
    LABEL_CONFIRMED_TRADE_HIT_TARGET,
    LABEL_CONFIRMED_TRADE_NO_ENTRY_WENT_DOWN,
    LABEL_CONFIRMED_TRADE_NO_ENTRY_WENT_UP,
    LABEL_CONFIRMED_TRADE_WENT_DOWN,
    LABEL_CONFIRMED_TRADE_WENT_UP,
    LABEL_EXCLUDED,
    LABEL_PENDING,
    LABEL_READY,
    OUTCOME_EXCLUDED,
    OUTCOME_INSUFFICIENT_DATA,
    OUTCOME_READY,
    STAGE_ENTRY_EVALUATION,
    STAGE_OUTCOME_LABEL,
)
from services.daily_dataset import (
    build_daily_dataset_candidate_row,
    normalize_accepted_trade_outcome_label,
    update_daily_dataset_outcome_from_paper_trade,
)


FIXED_NOW = "2026-07-08T11:00:00.000000Z"
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
        actual = nested_get(row, key)
        if isinstance(expected, dict) and "$in" in expected:
            if actual not in expected["$in"]:
                return False
            continue
        if actual != expected:
            return False
    return True


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = [deepcopy(row) for row in rows]

    def sort(self, *_args):
        return self

    def limit(self, limit: int):
        self.rows = self.rows[:limit]
        return self

    def __iter__(self):
        return iter(self.rows)

    def __aiter__(self):
        self._index = 0
        return self

    async def __anext__(self):
        if self._index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self._index]
        self._index += 1
        return deepcopy(row)


class FakeDatasetCollection:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = [deepcopy(row) for row in rows or []]
        self.update_calls = []

    def find(self, query: dict | None = None, *_args, **_kwargs):
        return FakeCursor([row for row in self.rows if matches_query(row, query)])

    async def find_one(self, query: dict | None = None, *_args, **_kwargs):
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


class FakeDb:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.daily_trade_dataset = FakeDatasetCollection(rows)

    def __getitem__(self, name: str):
        return getattr(self, name)


def scored_candidate(**overrides):
    row = {
        "_id": "scored-1",
        "exchange": "NSE",
        "symbol": "TEST",
        "canonical_symbol": "TEST",
        "tradingview_symbol": "NSE:TEST",
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
    }
    row.update(overrides)
    return row


def accepted_dataset_row(**candidate_overrides):
    row = build_daily_dataset_candidate_row(
        scored_candidate(**candidate_overrides),
        strategy_type="SWING",
        trade_date="2026-07-08",
        audit_time="2026-07-08T10:30:00.000000Z",
    )
    row["decision_snapshot"] = {"action": ACTION_TAKE_TRADE}
    row["identity"]["current_stage"] = STAGE_ENTRY_EVALUATION
    return row


def paper_trade(**overrides):
    row = {
        "_id": "paper-1",
        "symbol": "TEST",
        "canonical_symbol": "TEST",
        "tradingview_symbol": "NSE:TEST",
        "source_signal_type": "SWING_TV_CONFIRMED",
        "paper_only": True,
        "status": "WAITING_FOR_ENTRY",
        "outcome_status": "WAITING_FOR_ENTRY",
        "setup_date": "2026-07-08",
        "source_candle_at": SOURCE_CANDLE_AT,
        "setup_id": "setup-123",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "target_1": 110.0,
        "target_2": 116.0,
        "target_3": 124.0,
        "quantity": 10,
        "quantity_remaining": 10,
    }
    row.update(overrides)
    return row


def label_for(trade: dict, *, row: dict | None = None, journal: dict | None = None):
    db = FakeDb([row or accepted_dataset_row(setup_id="setup-123")])
    result = run(update_daily_dataset_outcome_from_paper_trade(db, trade, trade_journal=journal, audit_time=FIXED_NOW))
    return result, db.daily_trade_dataset.rows[0], db.daily_trade_dataset.update_calls


def test_target_hit_paper_trade_becomes_confirmed_trade_hit_target():
    result, stored, _calls = label_for(paper_trade(status="COMPLETED", outcome_status="T3_HIT", t3_hit=True))

    assert result["updated_count"] == 1
    assert nested_get(stored, "future_outcome.outcome_state") == OUTCOME_READY
    assert nested_get(stored, "future_outcome.target_hit") is True
    assert nested_get(stored, "ml_label.label_state") == LABEL_READY
    assert nested_get(stored, "ml_label.label_category") == LABEL_CONFIRMED_TRADE_HIT_TARGET
    assert nested_get(stored, "identity.current_stage") == STAGE_OUTCOME_LABEL


def test_sl_hit_paper_trade_becomes_confirmed_trade_hit_sl():
    result, stored, _calls = label_for(paper_trade(status="SL_HIT", outcome_status="SL_HIT", stop_loss_hit=True))

    assert result["updated_count"] == 1
    assert nested_get(stored, "future_outcome.stop_loss_hit") is True
    assert nested_get(stored, "ml_label.label_category") == LABEL_CONFIRMED_TRADE_HIT_SL


def test_entry_triggered_positive_outcome_becomes_confirmed_trade_went_up():
    result, stored, _calls = label_for(
        paper_trade(status="CLOSED", outcome_status="CLOSED", entry_triggered=True, realized_pnl=72.5)
    )

    assert result["updated_count"] == 1
    assert nested_get(stored, "ml_label.label_category") == LABEL_CONFIRMED_TRADE_WENT_UP
    assert nested_get(stored, "future_outcome.realized_pnl") == 72.5


def test_entry_triggered_negative_outcome_becomes_confirmed_trade_went_down():
    result, stored, _calls = label_for(
        paper_trade(status="CLOSED", outcome_status="CLOSED", entry_triggered=True, pnl_per_share=-3.25)
    )

    assert result["updated_count"] == 1
    assert nested_get(stored, "ml_label.label_category") == LABEL_CONFIRMED_TRADE_WENT_DOWN
    assert nested_get(stored, "future_outcome.pnl_per_share") == -3.25


def test_no_entry_positive_move_becomes_confirmed_trade_no_entry_went_up():
    result, stored, _calls = label_for(
        paper_trade(status="EXPIRED", outcome_status="EXPIRED", entry_triggered=False, current_price=106.0)
    )

    assert result["updated_count"] == 1
    assert nested_get(stored, "future_outcome.entry_triggered") is False
    assert nested_get(stored, "ml_label.label_category") == LABEL_CONFIRMED_TRADE_NO_ENTRY_WENT_UP


def test_no_entry_negative_move_becomes_confirmed_trade_no_entry_went_down():
    result, stored, _calls = label_for(
        paper_trade(status="NO_ENTRY", outcome_status="NO_ENTRY", entry_triggered=False, current_price=94.0)
    )

    assert result["updated_count"] == 1
    assert nested_get(stored, "future_outcome.entry_triggered") is False
    assert nested_get(stored, "ml_label.label_category") == LABEL_CONFIRMED_TRADE_NO_ENTRY_WENT_DOWN


def test_ambiguous_trade_becomes_excluded_not_forced_into_label():
    result, stored, _calls = label_for(
        paper_trade(status="AMBIGUOUS", outcome_status="AMBIGUOUS", target_1_hit=True, stop_loss_hit=True)
    )

    assert result["updated_count"] == 1
    assert nested_get(stored, "future_outcome.outcome_state") == OUTCOME_EXCLUDED
    assert nested_get(stored, "ml_label.label_state") == LABEL_EXCLUDED
    assert nested_get(stored, "ml_label.label_category") is None
    assert nested_get(stored, "ml_label.exclusion_reason") == "AMBIGUOUS_TARGET_STOP_EVIDENCE"


def test_missing_directional_data_remains_pending_and_insufficient_data():
    row = accepted_dataset_row(setup_id="setup-123")
    before_stage = row["identity"]["current_stage"]

    result, stored, _calls = label_for(
        paper_trade(status="CLOSED", outcome_status="CLOSED", entry_triggered=True),
        row=row,
    )

    assert result["updated_count"] == 1
    assert nested_get(stored, "future_outcome.outcome_state") == OUTCOME_INSUFFICIENT_DATA
    assert nested_get(stored, "ml_label.label_state") == LABEL_PENDING
    assert nested_get(stored, "ml_label.label_category") is None
    assert nested_get(stored, "identity.current_stage") == before_stage


def test_outcome_update_does_not_overwrite_immutable_identity_fields():
    row = accepted_dataset_row(setup_id="setup-123")
    before_identity = deepcopy(row["identity"])

    result, stored, calls = label_for(paper_trade(status="SL_HIT", outcome_status="SL_HIT"), row=row)

    assert result["updated_count"] == 1
    for field in (
        "dataset_id",
        "identity_hash",
        "identity_string",
        "dataset_version",
        "trade_date",
        "canonical_symbol",
        "strategy_type",
        "candidate_key",
        "source_candle_at",
    ):
        assert stored["identity"][field] == before_identity[field]
    assert calls[0][2] is False


def test_outcome_fields_are_not_copied_into_pre_decision_feature_groups():
    result, stored, _calls = label_for(
        paper_trade(status="SL_HIT", outcome_status="SL_HIT", realized_pnl=-50.0, exit_reason="STOP_LOSS_HIT")
    )

    assert result["updated_count"] == 1
    for section in (
        "raw_market_snapshot",
        "score_snapshot",
        "strategy_selection",
        "tv_confirmation_snapshot",
        "decision_snapshot",
        "trade_plan_snapshot",
    ):
        assert "label_category" not in stored[section]
        assert "label_state" not in stored[section]
        assert "outcome_state" not in stored[section]
        assert "realized_pnl" not in stored[section]
    assert nested_get(stored, "ml_label.label_category") == LABEL_CONFIRMED_TRADE_HIT_SL


def test_dataset_outcome_update_is_idempotent_for_existing_row():
    row = accepted_dataset_row(setup_id="setup-123")
    db = FakeDb([row])
    trade = paper_trade(status="COMPLETED", outcome_status="T3_HIT", t3_hit=True)

    first = run(update_daily_dataset_outcome_from_paper_trade(db, trade, audit_time=FIXED_NOW))
    second = run(update_daily_dataset_outcome_from_paper_trade(db, trade, audit_time=FIXED_NOW))

    assert first["updated_count"] == 1
    assert second["updated_count"] == 1
    assert len(db.daily_trade_dataset.rows) == 1
    assert len(db.daily_trade_dataset.update_calls) == 2
    assert nested_get(db.daily_trade_dataset.rows[0], "ml_label.label_category") == LABEL_CONFIRMED_TRADE_HIT_TARGET
    assert all(call[2] is False for call in db.daily_trade_dataset.update_calls)


def test_non_take_trade_rows_are_skipped_without_outcome_label():
    row = accepted_dataset_row(setup_id="setup-123")
    row["decision_snapshot"] = {"action": ACTION_NO_TRADE}
    db = FakeDb([row])

    result = run(update_daily_dataset_outcome_from_paper_trade(db, paper_trade(status="SL_HIT"), audit_time=FIXED_NOW))

    assert result["skipped_count"] == 1
    assert db.daily_trade_dataset.update_calls == []
    assert nested_get(db.daily_trade_dataset.rows[0], "ml_label.label_state") == LABEL_PENDING


def test_normalizer_preserves_advisory_shape_without_mongo():
    row = accepted_dataset_row(setup_id="setup-123")

    normalized = normalize_accepted_trade_outcome_label(
        row,
        paper_trade=paper_trade(status="CLOSED", outcome_status="CLOSED", entry_triggered=True, realized_pnl=5.0),
        label_assigned_at=FIXED_NOW,
    )

    assert normalized["update_allowed"] is True
    assert normalized["future_outcome"]["outcome_state"] == OUTCOME_READY
    assert normalized["ml_label"]["label_category"] == LABEL_CONFIRMED_TRADE_WENT_UP
