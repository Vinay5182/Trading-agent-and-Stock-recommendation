import os
import sys
import pandas as pd
import pytest

backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from services.ml_dataset_exporter import (
    build_ml_dataset_dataframe,
    split_ml_dataset,
    compute_schema_hash,
    FEATURE_COLUMNS,
    TARGET_COLUMNS,
    METADATA_COLUMNS,
)
from services.candidate_trade_outcomes_service import create_candidate_trade_outcome_doc


def test_build_ml_dataset_dataframe_parsing():
    doc1 = create_candidate_trade_outcome_doc(
        symbol="NSE:STOCK_A",
        scan_date="2026-06-01",
        candidate_features={"score": 85, "momentum_score": 90, "strategy_type": "MULTI"},
        trade_plan={"entry_price": 100.0, "stop_loss": 95.0, "target_1": 105.0, "atr_14": 2.0},
    )
    doc2 = create_candidate_trade_outcome_doc(
        symbol="NSE:STOCK_B",
        scan_date="2026-06-02",
        candidate_features={"score": 75, "momentum_score": 80, "strategy_type": "MULTI"},
        trade_plan={"entry_price": 200.0, "stop_loss": 190.0, "target_1": 210.0, "atr_14": 4.0},
    )

    df = build_ml_dataset_dataframe([doc1, doc2])

    assert not df.empty
    assert len(df) == 2

    # Check feature presence
    for col in FEATURE_COLUMNS:
        assert col in df.columns, f"Feature column '{col}' missing from DataFrame"

    for col in TARGET_COLUMNS:
        assert col in df.columns, f"Target column '{col}' missing from DataFrame"

    for col in METADATA_COLUMNS:
        assert col in df.columns, f"Metadata column '{col}' missing from DataFrame"


def test_split_ml_dataset_chronological():
    rows = []
    for i in range(100):
        day_str = f"2026-06-{(i % 25 + 1):02d}"
        rows.append({
            "candidate_id": f"id_{i:03d}",
            "scan_date": day_str,
            "entry_price": 100.0 + i,
            "trade_outcome": "WIN" if i % 2 == 0 else "LOSS",
        })

    df = pd.DataFrame(rows).sort_values(by="scan_date").reset_index(drop=True)

    df_train, df_val, df_test = split_ml_dataset(df, train_ratio=0.70, val_ratio=0.15, test_ratio=0.15)

    assert len(df_train) == 70
    assert len(df_val) == 15
    assert len(df_test) == 15

    # Check chronological boundary ordering
    train_max_date = df_train["scan_date"].max()
    val_min_date = df_val["scan_date"].min()
    val_max_date = df_val["scan_date"].max()
    test_min_date = df_test["scan_date"].min()

    assert train_max_date <= val_min_date
    assert val_max_date <= test_min_date


def test_schema_hash_determinism():
    df = pd.DataFrame({"col1": [1, 2], "col2": [3.0, 4.0]})
    hash1 = compute_schema_hash(df)
    hash2 = compute_schema_hash(df)
    assert hash1 == hash2
    assert len(hash1) == 16


def test_candidate_id_filtering_and_deduplication():
    doc_none = {
        "candidate_id": None,
        "identity": {"candidate_id": None, "symbol": "NSE:NONE"},
        "scan_date": "2026-06-01",
    }
    doc_valid_1 = {
        "identity": {"candidate_id": "cand_001", "symbol": "NSE:STOCK_A", "scan_date": "2026-06-01"},
    }
    doc_valid_1_dup = {
        "identity": {"candidate_id": "cand_001", "symbol": "NSE:STOCK_A", "scan_date": "2026-06-01"},
    }
    doc_valid_2 = {
        "identity": {"candidate_id": "cand_002", "symbol": "NSE:STOCK_B", "scan_date": "2026-06-02"},
    }

    df = build_ml_dataset_dataframe([doc_none, doc_valid_1, doc_valid_1_dup, doc_valid_2])

    assert len(df) == 2
    assert set(df["candidate_id"]) == {"cand_001", "cand_002"}

