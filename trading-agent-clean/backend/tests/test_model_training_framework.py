import os
import sys
import pandas as pd
import pytest

backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from ai.dataset_loader import load_versioned_dataset
from ai.dataset_preprocessor import DatasetPreprocessor
from ai.model_trainers import train_model, SUPPORTED_ALGORITHMS
from ai.benchmarking import evaluate_classification_model, evaluate_regression_model
from ai.feature_importance import extract_feature_importance


def test_dataset_preprocessor_scaling_and_encoding():
    df_raw = pd.DataFrame({
        "current_price": [100.0, 105.0, 110.0, 115.0],
        "volume": [1000, 2000, 1500, 2500],
        "market_trend": ["BULLISH", "BEARISH", "BULLISH", "NEUTRAL"],
    })

    prep = DatasetPreprocessor()
    df_trans = prep.fit_transform(df_raw)

    assert not df_trans.empty
    assert len(df_trans) == 4
    assert prep.is_fitted is True

    # Transform test row
    df_test = pd.DataFrame({
        "current_price": [102.0],
        "volume": [1200],
        "market_trend": ["BULLISH"],
    })
    df_test_trans = prep.transform(df_test)
    assert df_test_trans.shape[1] == df_trans.shape[1]


def test_model_trainers_and_evaluation():
    X_train = pd.DataFrame({
        "feat1": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0],
        "feat2": [10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0],
    })
    y_train = pd.Series([0, 0, 0, 0, 0, 1, 1, 1, 1, 1])

    X_val = pd.DataFrame({
        "feat1": [2.5, 7.5],
        "feat2": [7.5, 2.5],
    })
    y_val = pd.Series([0, 1])

    for algo in ["random_forest", "xgboost", "lightgbm", "linear"]:
        model = train_model(algo, "classification", X_train, y_train)
        metrics = evaluate_classification_model(model, X_val, y_val)

        assert "accuracy" in metrics
        assert "f1_score" in metrics
        assert "roc_auc" in metrics

        imp = extract_feature_importance(model, list(X_train.columns), X_val, y_val)
        assert "top_10" in imp
        assert len(imp["top_10"]) == 2
