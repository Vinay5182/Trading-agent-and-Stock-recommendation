import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd

from services.ml_dataset_exporter import FEATURE_COLUMNS, TARGET_COLUMNS, METADATA_COLUMNS


def load_versioned_dataset(
    dataset_path: str,
    manifest_path: Optional[str] = None,
    target_column: str = "binary_label",
) -> Tuple[pd.DataFrame, pd.Series, pd.DataFrame, Dict[str, Any]]:
    """
    Loads versioned ML dataset file (CSV or Parquet) and separates Features (X), Target (y), and Metadata.
    Validates dataset against manifest if manifest_path is provided or found.
    """
    if not os.path.exists(dataset_path):
        raise FileNotFoundError(f"Dataset file not found at: {dataset_path}")

    if dataset_path.endswith(".parquet"):
        df = pd.read_parquet(dataset_path)
    else:
        df = pd.read_csv(dataset_path)

    # Locate manifest if not explicitly passed
    if manifest_path is None:
        dir_name = os.path.dirname(dataset_path)
        base_name = os.path.basename(dataset_path)
        # Attempt to infer manifest file
        if "v1.0.0" in base_name:
            candidate_manifest = os.path.join(dir_name, "dataset_manifest_v1.0.0.json")
            if os.path.exists(candidate_manifest):
                manifest_path = candidate_manifest

    manifest = {}
    if manifest_path and os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

    # Filter available feature columns
    avail_features = [col for col in FEATURE_COLUMNS if col in df.columns]
    avail_metadata = [col for col in METADATA_COLUMNS if col in df.columns]

    if target_column not in df.columns:
        raise KeyError(f"Target column '{target_column}' not found in dataset columns: {list(df.columns)}")

    # Drop rows where target is NaN (e.g. PENDING trades for binary classification)
    valid_df = df[df[target_column].notna()].copy()

    X = valid_df[avail_features].copy()
    y = valid_df[target_column].copy()
    metadata = valid_df[avail_metadata].copy()

    return X, y, metadata, manifest
