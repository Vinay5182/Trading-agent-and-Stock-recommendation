import asyncio
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.daily_dataset_contract import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_AMBIGUOUS,
    LIFECYCLE_CLOSED,
    LIFECYCLE_EXPIRED,
    LIFECYCLE_NO_ENTRY,
    LIFECYCLE_PARTIAL,
    LIFECYCLE_WAITING_FOR_ENTRY,
    PAPER_LINK_LINKED,
    STAGE_ENTRY_EVALUATION,
    STAGE_PAPER_SYNC,
)
from services.daily_dataset import build_daily_dataset_candidate_row, update_daily_dataset_from_paper_trade
from services.paper_sync import sync_trade_ready


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
    def __init__(self, rows: list[dict] | None = None, *, fail_find: bool = False) -> None:
        self.rows = [deepcopy(row) for row in rows or []]
        self.update_calls = []
        self.fail_find = fail_find

    def find(self, query: dict | None = None, *_args, **_kwargs):
        if self.fail_find:
            raise RuntimeError("dataset find failed")
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
    def __init__(self, rows: list[dict] | None = None, *, fail_dataset_find: bool = False) -> None:
        self.daily_trade_dataset = FakeDatasetCollection(rows, fail_find=fail_dataset_find)

    def __getitem__(self, name: str):
        return getattr(self, name)


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
        "momentum_candidate": True,
        "momentum_status": "MOMENTUM_PRECHECK_PASSED",
    }
    row.update(overrides)
    return row


def dataset_row(strategy_type: str = "SWING", **candidate_overrides):
    return build_daily_dataset_candidate_row(
        scored_candidate(**candidate_overrides),
        strategy_type=strategy_type,
        trade_date="2026-07-08",
        audit_time="2026-07-08T10:30:00.000000Z",
    )


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
        "entry_price": 102.0,
        "stop_loss": 98.0,
        "target_1": 110.0,
        "target_2": 116.0,
        "target_3": 124.0,
        "quantity": 10,
        "quantity_remaining": 10,
    }
    row.update(overrides)
    return row


def test_paper_trade_links_matching_dataset_row_by_setup_id():
    db = FakeDb([dataset_row(setup_id="setup-123")])

    result = run(update_daily_dataset_from_paper_trade(db, paper_trade(setup_id="setup-123"), audit_time=FIXED_NOW))

    stored = db.daily_trade_dataset.rows[0]
    assert result["updated_count"] == 1
    assert nested_get(stored, "paper_trade_link.link_status") == PAPER_LINK_LINKED
    assert nested_get(stored, "paper_trade_link.paper_trade_id") == "paper-1"
    assert nested_get(stored, "paper_trade_link.setup_id") == "setup-123"
    assert nested_get(stored, "paper_trade_link.linked_at") == FIXED_NOW
    assert nested_get(stored, "identity.current_stage") == STAGE_PAPER_SYNC


def test_daily_dataset_lifecycle_snapshot_preserves_new_target_ladder():
    db = FakeDb([dataset_row(setup_id="setup-123")])

    result = run(
        update_daily_dataset_from_paper_trade(
            db,
            paper_trade(
                setup_id="setup-123",
                entry_price=100.0,
                stop_loss=90.0,
                target_1=110.0,
                target_2=120.0,
                target_3=130.0,
            ),
            audit_time=FIXED_NOW,
        )
    )

    stored = db.daily_trade_dataset.rows[0]
    assert result["updated_count"] == 1
    assert nested_get(stored, "lifecycle_snapshot.entry_price") == 100.0
    assert nested_get(stored, "lifecycle_snapshot.stop_loss") == 90.0
    assert nested_get(stored, "lifecycle_snapshot.target_1") == 110.0
    assert nested_get(stored, "lifecycle_snapshot.target_2") == 120.0
    assert nested_get(stored, "lifecycle_snapshot.target_3") == 130.0


def test_paper_trade_links_matching_dataset_row_by_trade_date_when_history_exists():
    old_row = build_daily_dataset_candidate_row(
        scored_candidate(_id="scored-old", source_candle_at="2026-06-18T09:45:00+00:00"),
        strategy_type="SWING",
        trade_date="2026-06-18",
        audit_time="2026-06-18T10:30:00.000000Z",
    )
    today_row = build_daily_dataset_candidate_row(
        scored_candidate(_id="scored-today", source_candle_at="2026-07-10T09:45:00+00:00"),
        strategy_type="SWING",
        trade_date="2026-07-10",
        audit_time="2026-07-10T10:30:00.000000Z",
    )
    db = FakeDb([old_row, today_row])

    result = run(
        update_daily_dataset_from_paper_trade(
            db,
            paper_trade(
                setup_id="paper_setup:v2:today",
                setup_date="2026-07-10",
                source_trade_date="2026-07-10",
                source_candle_at="2026-07-10T09:45:00Z",
            ),
            audit_time=FIXED_NOW,
        )
    )

    stored_old = next(row for row in db.daily_trade_dataset.rows if nested_get(row, "identity.trade_date") == "2026-06-18")
    stored_today = next(row for row in db.daily_trade_dataset.rows if nested_get(row, "identity.trade_date") == "2026-07-10")
    assert result["updated_count"] == 1
    assert nested_get(stored_old, "paper_trade_link.link_status") != PAPER_LINK_LINKED
    assert nested_get(stored_today, "paper_trade_link.link_status") == PAPER_LINK_LINKED
    assert nested_get(stored_today, "paper_trade_link.paper_trade_id") == "paper-1"
    assert nested_get(stored_today, "paper_trade_link.setup_id") == "paper_setup:v2:today"


def test_paper_trade_links_matching_dataset_row_by_candidate_id():
    db = FakeDb([dataset_row(candidate_id="candidate-123")])

    result = run(update_daily_dataset_from_paper_trade(db, paper_trade(candidate_id="candidate-123"), audit_time=FIXED_NOW))

    stored = db.daily_trade_dataset.rows[0]
    assert result["updated_count"] == 1
    assert nested_get(stored, "paper_trade_link.candidate_id") == "candidate-123"


def test_unmatched_paper_trade_does_not_create_duplicate_dataset_row():
    db = FakeDb([dataset_row(setup_id="setup-123")])

    result = run(
        update_daily_dataset_from_paper_trade(
            db,
            paper_trade(setup_id="setup-missing", symbol="OTHER", canonical_symbol="OTHER", tradingview_symbol="NSE:OTHER"),
            audit_time=FIXED_NOW,
        )
    )

    assert result["unmatched_count"] == 1
    assert len(db.daily_trade_dataset.rows) == 1
    assert db.daily_trade_dataset.update_calls == []


def test_multiple_unsafe_matches_are_skipped_with_validation_error():
    first = dataset_row(candidate_id="candidate-1")
    second = dataset_row(candidate_id="candidate-2")
    db = FakeDb([first, second])

    result = run(
        update_daily_dataset_from_paper_trade(
            db,
            paper_trade(source_candle_at=None, setup_date="2026-07-08"),
            audit_time=FIXED_NOW,
        )
    )

    assert result["skipped_count"] == 1
    assert "multiple matching daily_trade_dataset rows" in result["validation_errors"][0]["errors"][0]
    assert db.daily_trade_dataset.update_calls == []


def test_waiting_for_entry_maps_to_lifecycle_waiting_for_entry():
    db = FakeDb([dataset_row(setup_id="setup-123")])

    result = run(update_daily_dataset_from_paper_trade(db, paper_trade(setup_id="setup-123"), audit_time=FIXED_NOW))

    assert result["status_counts"][LIFECYCLE_WAITING_FOR_ENTRY] == 1
    assert nested_get(db.daily_trade_dataset.rows[0], "lifecycle_snapshot.lifecycle_status") == LIFECYCLE_WAITING_FOR_ENTRY
    assert nested_get(db.daily_trade_dataset.rows[0], "lifecycle_snapshot.entry_triggered") is False


def test_active_maps_to_active_and_entry_triggered_true():
    db = FakeDb([dataset_row(setup_id="setup-123")])

    run(update_daily_dataset_from_paper_trade(db, paper_trade(setup_id="setup-123", status="ACTIVE", outcome_status="ACTIVE"), audit_time=FIXED_NOW))

    assert nested_get(db.daily_trade_dataset.rows[0], "lifecycle_snapshot.lifecycle_status") == LIFECYCLE_ACTIVE
    assert nested_get(db.daily_trade_dataset.rows[0], "lifecycle_snapshot.entry_triggered") is True
    assert nested_get(db.daily_trade_dataset.rows[0], "identity.current_stage") == STAGE_ENTRY_EVALUATION


def test_partial_status_maps_to_partial():
    db = FakeDb([dataset_row(setup_id="setup-123")])

    run(update_daily_dataset_from_paper_trade(db, paper_trade(setup_id="setup-123", status="T1_PARTIAL"), audit_time=FIXED_NOW))

    stored = db.daily_trade_dataset.rows[0]
    assert nested_get(stored, "lifecycle_snapshot.lifecycle_status") == LIFECYCLE_PARTIAL
    assert nested_get(stored, "lifecycle_snapshot.target_1_hit") is True


def test_completed_and_target_hit_statuses_map_to_closed():
    for status in ("COMPLETED", "TARGET_HIT", "T1_HIT", "T2_HIT", "T3_HIT"):
        db = FakeDb([dataset_row(setup_id="setup-123")])

        run(update_daily_dataset_from_paper_trade(db, paper_trade(setup_id="setup-123", status=status), audit_time=FIXED_NOW))

        assert nested_get(db.daily_trade_dataset.rows[0], "lifecycle_snapshot.lifecycle_status") == LIFECYCLE_CLOSED


def test_sl_hit_maps_to_closed_with_stop_loss_hit_true():
    db = FakeDb([dataset_row(setup_id="setup-123")])

    run(update_daily_dataset_from_paper_trade(db, paper_trade(setup_id="setup-123", status="SL_HIT"), audit_time=FIXED_NOW))

    assert nested_get(db.daily_trade_dataset.rows[0], "lifecycle_snapshot.lifecycle_status") == LIFECYCLE_CLOSED
    assert nested_get(db.daily_trade_dataset.rows[0], "lifecycle_snapshot.stop_loss_hit") is True


def test_ambiguous_maps_to_ambiguous():
    db = FakeDb([dataset_row(setup_id="setup-123")])

    run(update_daily_dataset_from_paper_trade(db, paper_trade(setup_id="setup-123", status="AMBIGUOUS"), audit_time=FIXED_NOW))

    assert nested_get(db.daily_trade_dataset.rows[0], "lifecycle_snapshot.lifecycle_status") == LIFECYCLE_AMBIGUOUS
    assert nested_get(db.daily_trade_dataset.rows[0], "lifecycle_snapshot.ambiguity_status") == "AMBIGUOUS"


def test_expired_maps_to_expired():
    db = FakeDb([dataset_row(setup_id="setup-123")])

    run(update_daily_dataset_from_paper_trade(db, paper_trade(setup_id="setup-123", status="EXPIRED"), audit_time=FIXED_NOW))

    assert nested_get(db.daily_trade_dataset.rows[0], "lifecycle_snapshot.lifecycle_status") == LIFECYCLE_EXPIRED


def test_no_entry_maps_to_no_entry():
    db = FakeDb([dataset_row(setup_id="setup-123")])

    run(update_daily_dataset_from_paper_trade(db, paper_trade(setup_id="setup-123", status="NO_ENTRY"), audit_time=FIXED_NOW))

    assert nested_get(db.daily_trade_dataset.rows[0], "lifecycle_snapshot.lifecycle_status") == LIFECYCLE_NO_ENTRY


def test_raw_market_snapshot_is_not_overwritten():
    db = FakeDb([dataset_row(setup_id="setup-123")])
    before = deepcopy(db.daily_trade_dataset.rows[0]["raw_market_snapshot"])

    run(update_daily_dataset_from_paper_trade(db, paper_trade(setup_id="setup-123", current_price=130.0), audit_time=FIXED_NOW))

    assert db.daily_trade_dataset.rows[0]["raw_market_snapshot"] == before
    assert nested_get(db.daily_trade_dataset.rows[0], "lifecycle_snapshot.current_price") == 130.0


def test_score_snapshot_is_not_overwritten():
    db = FakeDb([dataset_row(setup_id="setup-123")])
    before = deepcopy(db.daily_trade_dataset.rows[0]["score_snapshot"])

    run(update_daily_dataset_from_paper_trade(db, paper_trade(setup_id="setup-123", score=1, confidence_score=1), audit_time=FIXED_NOW))

    assert db.daily_trade_dataset.rows[0]["score_snapshot"] == before


def test_lifecycle_fields_are_stored_only_under_lifecycle_snapshot():
    db = FakeDb([dataset_row(setup_id="setup-123")])

    run(
        update_daily_dataset_from_paper_trade(
            db,
            paper_trade(setup_id="setup-123", status="COMPLETED", exit_price=124.0, paper_pnl=220.0, pnl_per_share=22.0),
            audit_time=FIXED_NOW,
        )
    )

    stored = db.daily_trade_dataset.rows[0]
    assert nested_get(stored, "lifecycle_snapshot.exit_price") == 124.0
    assert nested_get(stored, "lifecycle_snapshot.realized_pnl") == 220.0
    for section in ("raw_market_snapshot", "score_snapshot", "strategy_selection", "tv_confirmation_snapshot", "decision_snapshot", "trade_plan_snapshot"):
        assert "exit_price" not in stored[section]
        assert "realized_pnl" not in stored[section]
        assert "pnl_per_share" not in stored[section]


def test_pnl_and_outcome_fields_not_copied_into_pre_decision_feature_groups():
    db = FakeDb([dataset_row(setup_id="setup-123")])

    run(
        update_daily_dataset_from_paper_trade(
            db,
            paper_trade(setup_id="setup-123", status="SL_HIT", paper_pnl=-40.0, exit_reason="STOP_LOSS", exit_price=98.0),
            audit_time=FIXED_NOW,
        )
    )

    stored = db.daily_trade_dataset.rows[0]
    for section in ("raw_market_snapshot", "score_snapshot", "strategy_selection"):
        assert "paper_pnl" not in stored[section]
        assert "exit_reason" not in stored[section]
        assert "outcome_status" not in stored[section]
        assert "stop_loss_hit" not in stored[section]


class FakePaperCollection:
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
        if row:
            row.update(deepcopy(update.get("$set", {})))
            return SimpleNamespace(matched_count=1, modified_count=1 if update.get("$set") else 0, upserted_id=None)
        if not upsert:
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)
        new_row = {**deepcopy(query), **deepcopy(update.get("$setOnInsert", {})), **deepcopy(update.get("$set", {}))}
        new_row.setdefault("_id", f"id-{len(self.rows) + 1}")
        self.rows.append(new_row)
        return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=new_row["_id"])


class FakePaperSyncDb:
    def __init__(self, *, fail_dataset_find: bool = False) -> None:
        self.swing_tv_confirmations = FakePaperCollection([trade_ready_row("SYNC")])
        self.momentum_tv_confirmations = FakePaperCollection([])
        self.paper_signals = FakePaperCollection([])
        self.paper_trades = FakePaperCollection([])
        self.daily_trade_dataset = FakeDatasetCollection([], fail_find=fail_dataset_find)

    def __getitem__(self, name: str):
        return getattr(self, name)


def trade_ready_row(symbol: str) -> dict:
    return {
        "_id": f"confirmation-{symbol}",
        "symbol": symbol,
        "canonical_symbol": symbol,
        "tradingview_symbol": f"NSE:{symbol}",
        "index_name": "BROAD_MARKET_750",
        "tv_status": "CONFIRMED_SIGNAL",
        "tv_confirmed": True,
        "trade_quality_grade": "A",
        "trade_allowed": True,
        "paper_plan_valid": True,
        "paper_entry_price": 100.0,
        "paper_stop_loss": 90.0,
        "paper_target_1": 110.0,
        "paper_target_2": 120.0,
        "paper_target_3": 130.0,
        "paper_rr_1": 1.0,
        "paper_rr_2": 2.0,
        "paper_rr_3": 3.0,
        "previous_low": 91.0,
        "next_action_for_paper_trade": "PAPER_PLAN_READY",
        "created_at": "2026-07-08T09:00:00+00:00",
        "updated_at": "2026-07-08T09:05:00+00:00",
    }


def test_dataset_side_effect_failure_does_not_break_paper_sync_flow():
    db = FakePaperSyncDb(fail_dataset_find=True)

    result = run(sync_trade_ready(db_override=db))

    assert result["ok"] is True
    assert result["paper_trades_upserted"] == 1
    assert len(db.paper_trades.rows) == 1
    assert result["daily_dataset_update"]["error_count"] == 1


def test_dry_run_paper_sync_does_not_update_daily_trade_dataset():
    db = FakePaperSyncDb()

    result = run(sync_trade_ready(db_override=db, dry_run=True))

    assert result["paper_trades_upserted"] == 1
    assert len(db.paper_trades.rows) == 0
    assert db.daily_trade_dataset.update_calls == []
    assert "daily_dataset_update" not in result
