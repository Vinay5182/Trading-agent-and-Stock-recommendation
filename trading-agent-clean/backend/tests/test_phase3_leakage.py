import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import math
from datetime import datetime, timezone
import pytest


from ai.feature_contract import (
    AI_FEATURE_CONTRACT_VERSION,
    APPROVED_MODEL_FEATURES,
    BLOCKED_IDENTIFIERS,
    BLOCKED_LABEL_FIELDS,
    BLOCKED_LIFECYCLE_FIELDS,
    BLOCKED_ACCOUNT_FIELDS,
)
from ai.training_schema import (
    CANONICAL_SCHEMA_VERSION,
    build_canonical_training_row,
    validation_errors,
)


def _base_valid_source():
    return {
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "strategy_type": "SWING",
        "exchange": "NSE",
        "symbol": "TATASTEEL",
        "timeframe": "1D",
        "canonical_setup_id": "setup-12345",
        "source_candle_at": "2026-07-02T10:30:00.000000Z",
        "feature_as_of": "2026-07-02T11:00:00.000000Z",
        "feature_source_timestamp": "2026-07-02T10:45:00.000000Z",
        "score_version": "v1",
        "calculation_version": "v1",
        "label_version": "unlabeled_v1",
        "rule_score": 85.0,
        "trend_score": 10.0,
        "momentum_score": 5.0,
        "volume_score": 3.0,
        "risk_score": 1.0,
        "confidence_score": 0.9,
        "quality_score": 0.8,
        "momentum_trap_score": 0.0,
        "daily_ema20": 150.5,
        "daily_ema50": 145.2,
        "rsi14": 55.4,
        "atr14": 4.2,
    }


def test_exact_whitelist_enforcement_and_order():
    source = _base_valid_source()
    # Inject unknown fields in source_row
    source["unknown_field_abc"] = 123.4
    source["nested_unknown"] = {"a": 1, "b": 2}

    row = build_canonical_training_row(source, generated_at="2026-07-02T12:00:00Z")

    assert row["schema_version"] == CANONICAL_SCHEMA_VERSION
    assert row["feature_contract_version"] == AI_FEATURE_CONTRACT_VERSION
    assert row["audit"]["training_eligible"] is True

    # Check that model_features has EXACTLY the approved features in deterministic order
    model_features = row["model_features"]
    assert list(model_features.keys()) == list(APPROVED_MODEL_FEATURES)
    assert "unknown_field_abc" not in model_features
    assert "nested_unknown" not in model_features

    # Check that identity/metadata fields are not in model_features
    for field in row["identity"]:
        assert field not in model_features


def test_leakage_isolation_deny_rules():
    source = _base_valid_source()

    # Inject blocked identifiers
    for field in BLOCKED_IDENTIFIERS:
        source[field] = "blocked_id_val"

    # Inject blocked label fields
    for field in BLOCKED_LABEL_FIELDS:
        if field in ("paper_pnl", "realized_pnl", "exit_price"):
            source[field] = 250.0
        else:
            source[field] = "blocked_label_val"

    # Inject blocked lifecycle fields
    for field in BLOCKED_LIFECYCLE_FIELDS:
        source[field] = "blocked_lifecycle_val"

    # Inject blocked account fields
    for field in BLOCKED_ACCOUNT_FIELDS:
        source[field] = 10000.0

    row = build_canonical_training_row(source, generated_at="2026-07-02T12:00:00Z")
    model_features = row["model_features"]

    # Prove that absolutely none of the blocked fields enter model_features
    for field in BLOCKED_IDENTIFIERS:
        assert field not in model_features
    for field in BLOCKED_LABEL_FIELDS:
        assert field not in model_features
    for field in BLOCKED_LIFECYCLE_FIELDS:
        assert field not in model_features
    for field in BLOCKED_ACCOUNT_FIELDS:
        assert field not in model_features

    # Verify that plan context columns go to metadata, not features
    assert row["metadata"]["entry_price"] is None  # not present in valid base source
    assert row["metadata"]["setup_status"] == "blocked_lifecycle_val"


def test_temporal_leakage_firewall():
    # 1. Source timestamp <= feature_as_of -> valid
    source = _base_valid_source()
    row = build_canonical_training_row(source, generated_at="2026-07-02T12:00:00Z")
    assert row["audit"]["training_eligible"] is True

    # 2. Source timestamp > feature_as_of -> blocked
    source_future = _base_valid_source()
    source_future["feature_source_timestamp"] = "2026-07-02T11:15:00.000000Z" # after as_of (11:00:00)
    row_future = build_canonical_training_row(source_future, generated_at="2026-07-02T12:00:00Z")
    assert row_future["audit"]["training_eligible"] is False
    assert any("FEATURE_SOURCE_AFTER_FEATURE_AS_OF" in err for err in row_future["audit"]["validation_errors"])

    # 3. Naive source timestamp -> fail closed
    source_naive = _base_valid_source()
    source_naive["feature_source_timestamp"] = "2026-07-02 10:45:00" # naive
    row_naive = build_canonical_training_row(source_naive, generated_at="2026-07-02T12:00:00Z")
    assert row_naive["audit"]["training_eligible"] is False
    assert any("FEATURE_SOURCE_TIME_UNSAFE" in err for err in row_naive["audit"]["validation_errors"])

    # 4. Malformed source timestamp -> fail closed
    source_bad = _base_valid_source()
    source_bad["feature_source_timestamp"] = "bad_date_format"
    row_bad = build_canonical_training_row(source_bad, generated_at="2026-07-02T12:00:00Z")
    assert row_bad["audit"]["training_eligible"] is False
    assert any("FEATURE_SOURCE_TIME_UNSAFE" in err for err in row_bad["audit"]["validation_errors"])


def test_numeric_safety_validation():
    # 1. Valid zero preserved
    source = _base_valid_source()
    source["momentum_trap_score"] = 0.0
    row = build_canonical_training_row(source, generated_at="2026-07-02T12:00:00Z")
    assert row["model_features"]["momentum_trap_score"] == 0.0
    assert row["audit"]["training_eligible"] is True

    # 2. None remains missing (None)
    source_missing = _base_valid_source()
    source_missing["rsi14"] = None
    row_missing = build_canonical_training_row(source_missing, generated_at="2026-07-02T12:00:00Z")
    assert row_missing["model_features"]["rsi14"] is None
    assert row_missing["audit"]["training_eligible"] is True # optional features can be None

    # 3. NaN blocked
    source_nan = _base_valid_source()
    source_nan["rsi14"] = float("nan")
    row_nan = build_canonical_training_row(source_nan, generated_at="2026-07-02T12:00:00Z")
    assert row_nan["audit"]["training_eligible"] is False
    assert any("FEATURE_NON_FINITE:rsi14" in err for err in row_nan["audit"]["validation_errors"])

    # 4. Infinity blocked
    source_inf = _base_valid_source()
    source_inf["rsi14"] = float("inf")
    row_inf = build_canonical_training_row(source_inf, generated_at="2026-07-02T12:00:00Z")
    assert row_inf["audit"]["training_eligible"] is False
    assert any("FEATURE_NON_FINITE:rsi14" in err for err in row_inf["audit"]["validation_errors"])

    # 5. Invalid numeric string blocked (is parsed as string and rejected)
    source_str = _base_valid_source()
    source_str["rsi14"] = "not-a-number"
    row_str = build_canonical_training_row(source_str, generated_at="2026-07-02T12:00:00Z")
    assert row_str["audit"]["training_eligible"] is False
    assert any("FEATURE_TYPE_INVALID:rsi14" in err for err in row_str["audit"]["validation_errors"])


    # 6. Boolean type rejected (does not pass as numeric)
    source_bool = _base_valid_source()
    source_bool["rsi14"] = True
    row_bool = build_canonical_training_row(source_bool, generated_at="2026-07-02T12:00:00Z")
    assert row_bool["audit"]["training_eligible"] is False
    assert any("FEATURE_TYPE_INVALID:rsi14" in err for err in row_bool["audit"]["validation_errors"])

    # 7. Documented range rules enforced
    source_range = _base_valid_source()
    source_range["rsi14"] = 120.0 # out of range (max 100)
    row_range = build_canonical_training_row(source_range, generated_at="2026-07-02T12:00:00Z")
    assert row_range["audit"]["training_eligible"] is False
    assert any("FEATURE_OUT_OF_RANGE:rsi14" in err for err in row_range["audit"]["validation_errors"])


def test_stability_rules():
    source1 = _base_valid_source()
    source2 = _base_valid_source()

    # Modify post-trade labels in source2
    source2["result_label"] = "WIN"
    source2["paper_pnl"] = 500.0
    source2["exit_price"] = 155.0

    row1 = build_canonical_training_row(source1, generated_at="2026-07-02T12:00:00Z")
    row2 = build_canonical_training_row(source2, generated_at="2026-07-02T12:00:00Z")

    # Check model features are identical
    assert row1["model_features"] == row2["model_features"]

    # Check stable training row ID
    assert row1["training_row_id"] == row2["training_row_id"]
    assert row1["training_row_id"] is not None


@pytest.mark.anyio
async def test_leakage_audit_route_is_read_only(monkeypatch):
    class FakeCursor:
        def __init__(self, r):
            self.r = r
        def sort(self, *args, **kwargs):
            return self
        def limit(self, *args, **kwargs):
            self.r = self.r[:args[0]]
            return self
        def __aiter__(self):
            return self
        async def __anext__(self):
            if not self.r:
                raise StopAsyncIteration
            return self.r.pop(0)

    class FakeCollection:
        def __init__(self, rows):
            self.rows = rows
        def find(self, *args, **kwargs):
            return FakeCursor(list(self.rows))

    class FakeDB:
        def __init__(self):
            self.ai_feature_snapshots = FakeCollection([
                {
                    "schema_version": CANONICAL_SCHEMA_VERSION,
                    "strategy_type": "SWING",
                    "exchange": "NSE",
                    "symbol": "TATASTEEL",
                    "timeframe": "1D",
                    "canonical_setup_id": "setup-12345",
                    "source_candle_at": "2026-07-02T10:30:00.000000Z",
                    "feature_as_of": "2026-07-02T11:00:00.000000Z",
                    "feature_source_timestamp": "2026-07-02T10:45:00.000000Z",
                    "score_version": "v1",
                    "calculation_version": "v1",
                    "label_version": "unlabeled_v1",
                    "rule_score": 85.0,
                    "trend_score": 10.0,
                    "momentum_score": 5.0,
                    "volume_score": 3.0,
                    "risk_score": 1.0,
                    "confidence_score": 0.9,
                    "quality_score": 0.8,
                    "momentum_trap_score": 0.0,
                    "daily_ema20": 150.5,
                    "daily_ema50": 145.2,
                    "rsi14": 55.4,
                    "atr14": 4.2,
                    "paper_only": True,
                    "snapshot_time": "2026-07-02T11:00:00.000000Z",
                }
            ])

    fake_db = FakeDB()
    monkeypatch.setattr("routes.ai.get_database", lambda: fake_db)

    from routes.ai import get_ai_features_leakage_audit

    # Call the audit route helper directly (read-only verification)
    res = await get_ai_features_leakage_audit(limit=10)

    assert res["paper_only"] is True
    assert res["read_only"] is True
    assert res["preview_only"] is True
    assert "feature_contract_version" in res
    assert "ordered_whitelisted_model_features" in res
    assert "blocked_categories" in res
    assert "extra_keys_reconciliation" in res
    assert res["extra_keys_reconciliation"]["reconciliation_proven"] is True
