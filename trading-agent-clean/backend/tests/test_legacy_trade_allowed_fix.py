import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime

# Add backend directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from unittest.mock import MagicMock
from database import get_database
from services.paper_sync import is_current_schema, is_trade_ready_saved_row, sync_trade_ready
from services.paper_identity import apply_setup_identity
from routes.swing import save_confirmation_row
from routes.momentum import save_momentum_confirmation_row
from cli.cleanup_legacy_trade_allowed import build_cleanup_preview_plan, run as run_cleanup

from tests.test_paper_auto_sync import FakeDb, FakeCollection, trade_ready_row, existing_trade, matches

# Monkeypatch FakeCollection.update_one to support $unset
_orig_update_one = FakeCollection.update_one

async def patched_update_one(self, query: dict, update: dict, upsert: bool = False):
    self.update_calls.append((query, update, upsert))
    row = next((row for row in self.rows if matches(row, query)), None)
    if row is not None:
        row.update(update.get("$set", {}))
        for key, value in update.get("$inc", {}).items():
            row[key] = row.get(key, 0) + value
        if "$unset" in update:
            for key in update["$unset"]:
                row.pop(key, None)
        return SimpleNamespace(modified_count=1 if (update.get("$set") or update.get("$unset")) else 0, matched_count=1, upserted_id=None)
    if not upsert:
        return SimpleNamespace(modified_count=0, matched_count=0, upserted_id=None)
    new_row = {**query, **update.get("$setOnInsert", {}), **update.get("$set", {})}
    if "$unset" in update:
        for key in update["$unset"]:
            new_row.pop(key, None)
    new_row.setdefault("_id", f"id-{len(self.rows) + 1}")
    self.rows.append(new_row)
    return SimpleNamespace(modified_count=0, matched_count=0, upserted_id=new_row["_id"])

FakeCollection.update_one = patched_update_one


def isolated_migration_args(tmp_path, plan):
    plan_path = tmp_path / "legacy-cleanup-plan.json"
    backup_path = tmp_path / "legacy-cleanup-backup.json"
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8")
    backup_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "database_name": "wave0d1_isolated_fake_db",
                "created_at": "2099-01-01T00:00:00Z",
                "backup_location": "temporary-test-backup",
                "collections": ["swing_tv_confirmations", "momentum_tv_confirmations"],
                "backup_id": "legacy-cleanup-backup",
                "verified_by_operator": True,
            }
        ),
        encoding="utf-8",
    )
    return SimpleNamespace(
        apply=True,
        plan_file=str(plan_path),
        confirm_plan_sha256=plan["operation_content_sha256"],
        backup_manifest=str(backup_path),
        maintenance_approved=True,
        runtime_metadata=None,
    )


def test_swing_confirmation_stale_trade_allowed_eligible():
    row = trade_ready_row("ADANIENT")
    row["trade_allowed"] = False
    assert is_current_schema(row) is True
    assert is_trade_ready_saved_row(row) is True

def test_momentum_confirmation_stale_trade_allowed_eligible():
    row = trade_ready_row("ADANIENT")
    row["trade_allowed"] = False
    row["tv_status"] = "MOMENTUM_CONFIRMED"
    assert is_current_schema(row) is True
    assert is_trade_ready_saved_row(row) is True

def test_legacy_only_confirmation_blocked():
    row = trade_ready_row("LEGACYONLY")
    row["trade_allowed"] = False
    del row["paper_plan_valid"]  # Lacks canonical paper_plan_valid field
    assert is_current_schema(row) is False
    assert is_trade_ready_saved_row(row) is False

def test_technical_failure_blocked():
    row = trade_ready_row("TECHFAIL")
    row["tv_status"] = "TECHNICAL_FAILED"
    assert is_trade_ready_saved_row(row) is False

def test_rejected_plan_blocked():
    row = trade_ready_row("REJECTEDPLAN")
    row["paper_plan_valid"] = False
    assert is_trade_ready_saved_row(row) is False

def test_missing_levels_blocked():
    row = trade_ready_row("MISSINGLEVELS")
    row["paper_entry_price"] = None
    assert is_current_schema(row) is False
    assert is_trade_ready_saved_row(row) is False

def test_swing_save_unsets_legacy_field(monkeypatch):
    row = trade_ready_row("SWINGSAVE")
    db = FakeDb()
    monkeypatch.setattr("routes.swing.get_database", lambda: db)

    # We call save_confirmation_row
    asyncio.run(save_confirmation_row(row))

    update_calls = db.swing_tv_confirmations.update_calls
    assert len(update_calls) == 1
    query, update, upsert = update_calls[0]
    assert "$unset" not in update

def test_momentum_save_unsets_legacy_field(monkeypatch):
    row = trade_ready_row("MOMENTSAVE")
    row["tv_status"] = "MOMENTUM_CONFIRMED"
    db = FakeDb()
    monkeypatch.setattr("routes.momentum.get_database", lambda: db)

    asyncio.run(save_momentum_confirmation_row(row))

    update_calls = db.momentum_tv_confirmations.update_calls
    assert len(update_calls) == 1
    query, update, upsert = update_calls[0]
    assert "$unset" not in update

def test_setup_identity_no_collision():
    swing_signal = {
        "symbol": "SAME",
        "tradingview_symbol": "NSE:SAME",
        "timeframe": "1D",
        "paper_only": True,
        "source_confirmation_id": "conf1",
        "source_collection": "SWING_TV_CONFIRMATIONS",
        "source_signal_type": "SWING_TV_CONFIRMED"
    }
    momentum_signal = {
        "symbol": "SAME",
        "tradingview_symbol": "NSE:SAME",
        "timeframe": "1D",
        "paper_only": True,
        "source_confirmation_id": "conf2",
        "source_collection": "MOMENTUM_TV_CONFIRMATIONS",
        "source_signal_type": "MOMENTUM_TV_CONFIRMED"
    }

    swing_ident = apply_setup_identity(swing_signal)
    momentum_ident = apply_setup_identity(momentum_signal)

    assert swing_ident["setup_id"] != momentum_ident["setup_id"]

def test_terminal_setup_not_recreated():
    # Setup ID is generated from confirmation info
    row = trade_ready_row("TERMINAL")
    row["_id"] = "terminal_conf"
    # Must add a fixed timestamp so setup_date is deterministic.
    # Without it, setup_date defaults to utcnow() inside sync_trade_ready,
    # which differs from the timestamp passed here, causing different setup_id hashes.
    row["updated_at"] = "2026-06-16T09:00:00"

    # Let's generate setup_id manually to match the synced trade
    # _paper_docs_from_saved_row generates trade setup_id
    from services.paper_sync import _paper_docs_from_saved_row
    _, trade = _paper_docs_from_saved_row(row, "SWING_TV_CONFIRMED", "swing_tv_confirmations", "2026-06-16T09:00:00")
    setup_id = trade["setup_id"]

    db = FakeDb(
        swing_rows=[row],
        trades=[{
            "symbol": "TERMINAL",
            "setup_id": setup_id,
            "status": "STOPPED",
            "outcome_status": "STOPPED",
            "paper_only": True
        }]
    )

    # Sync trade ready
    result = asyncio.run(sync_trade_ready(db_override=db))

    # Ensure it was protected and not recreated
    assert result["completed_outcomes_protected"] == 1
    assert result["paper_trades_upserted"] == 0
    # There should only be one trade in paper_trades (the stopped one)
    assert len(db.paper_trades.rows) == 1
    assert db.paper_trades.rows[0]["status"] == "STOPPED"

def test_cleanup_dry_run_writes_nothing(monkeypatch):
    row = trade_ready_row("DRYRUN")
    row["trade_allowed"] = False
    row["_id"] = "dry_id"
    db = FakeDb(swing_rows=[row])

    db.name = "wave0d1_isolated_fake_db"

    result = asyncio.run(run_cleanup(SimpleNamespace(apply=False, plan_output=None), db_override=db))

    assert result["apply"] is False
    assert result["total_identified_eligible"] == 1
    assert result["total_modified"] == 0
    assert len(db.swing_tv_confirmations.update_calls) == 0

def test_cleanup_apply_unsets_targeted_fields(monkeypatch, tmp_path):
    row = trade_ready_row("APPLYRUN")
    row["trade_allowed"] = False
    row["_id"] = "apply_id"
    db = FakeDb(swing_rows=[row])

    db.name = "wave0d1_isolated_fake_db"
    preview = asyncio.run(build_cleanup_preview_plan(db))

    result = asyncio.run(run_cleanup(isolated_migration_args(tmp_path, preview["plan"]), db_override=db))

    assert result["apply"] is True
    assert result["applied_operations"] == 1
    assert result["total_modified"] == 1
    assert len(db.swing_tv_confirmations.update_calls) == 1
    query, update, _upsert = db.swing_tv_confirmations.update_calls[0]
    assert set(query) != {"_id"}
    assert update["$unset"] == {"trade_allowed": ""}

def test_cleanup_apply_is_idempotent(monkeypatch, tmp_path):
    row = trade_ready_row("IDEMPOTENT")
    row["trade_allowed"] = False
    row["_id"] = "idem_id"
    db = FakeDb(swing_rows=[row])

    db.name = "wave0d1_isolated_fake_db"

    # First apply
    preview = asyncio.run(build_cleanup_preview_plan(db))
    result1 = asyncio.run(run_cleanup(isolated_migration_args(tmp_path, preview["plan"]), db_override=db))

    # Simulate DB mutation (removing the trade_allowed field)
    # FakeCollection update_one updates key-value on matching documents
    assert result1["total_modified"] == 1
    assert "trade_allowed" not in db.swing_tv_confirmations.rows[0]

    # Second apply
    result2 = asyncio.run(run_cleanup(SimpleNamespace(apply=False, plan_output=None), db_override=db))
    assert result2["total_identified_eligible"] == 0
    assert result2["total_modified"] == 0

def test_sync_creates_one_trade_per_setup():
    row = trade_ready_row("SYNCONCE")
    db = FakeDb(swing_rows=[row])

    result = asyncio.run(sync_trade_ready(db_override=db))
    assert result["paper_trades_upserted"] == 1
    assert len(db.paper_trades.rows) == 1
    assert db.paper_trades.rows[0]["status"] == "WAITING_FOR_ENTRY"

def test_rerunning_sync_creates_no_duplicate():
    row = trade_ready_row("NODUP")
    db = FakeDb(swing_rows=[row])

    result1 = asyncio.run(sync_trade_ready(db_override=db))
    assert result1["paper_trades_upserted"] == 1

    result2 = asyncio.run(sync_trade_ready(db_override=db))
    assert result2["paper_trades_upserted"] == 0
    assert len(db.paper_trades.rows) == 1

class AsyncMock(MagicMock):
    async def __call__(self, *args, **kwargs):
        return super().__call__(*args, **kwargs)
