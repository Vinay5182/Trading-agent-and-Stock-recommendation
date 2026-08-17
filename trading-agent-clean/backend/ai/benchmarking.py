from typing import Any, Dict, List
import numpy as np
import pandas as pd

from sklearn.metrics import (
    accuracy_score,
    precision_score,
    recall_score,
    f1_score,
    roc_auc_score,
    confusion_matrix,
    mean_absolute_error,
    mean_squared_error,
    r2_score,
)


def evaluate_classification_model(model: Any, X_val: pd.DataFrame, y_val: pd.Series) -> Dict[str, Any]:
    y_pred = model.predict(X_val)

    acc = float(accuracy_score(y_val, y_pred))
    prec = float(precision_score(y_val, y_pred, zero_division=0))
    rec = float(recall_score(y_val, y_pred, zero_division=0))
    f1 = float(f1_score(y_val, y_pred, zero_division=0))

    try:
        if hasattr(model, "predict_proba"):
            y_proba = model.predict_proba(X_val)[:, 1]
            auc = float(roc_auc_score(y_val, y_proba))
        else:
            auc = 0.5
    except Exception:
        auc = 0.5

    cm = confusion_matrix(y_val, y_pred).tolist()

    return {
        "accuracy": round(acc, 4),
        "precision": round(prec, 4),
        "recall": round(rec, 4),
        "f1_score": round(f1, 4),
        "roc_auc": round(auc, 4),
        "confusion_matrix": cm,
    }


def evaluate_regression_model(model: Any, X_val: pd.DataFrame, y_val: pd.Series) -> Dict[str, Any]:
    y_pred = model.predict(X_val)

    mae = float(mean_absolute_error(y_val, y_pred))
    mse = float(mean_squared_error(y_val, y_pred))
    rmse = float(np.sqrt(mse))
    r2 = float(r2_score(y_val, y_pred))

    try:
        mape = float(np.mean(np.abs((y_val - y_pred) / np.maximum(np.abs(y_val), 1e-6))) * 100)
    except Exception:
        mape = 0.0

    return {
        "mae": round(mae, 4),
        "rmse": round(rmse, 4),
        "r2": round(r2, 4),
        "mape": round(mape, 4),
    }


def benchmark_all_models(
    trainers_map: Dict[str, Any],
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    task_type: str = "classification",
) -> List[Dict[str, Any]]:
    """
    Trains and benchmarks all supported algorithms, returning a sorted list of benchmark results.
    """
    results = []

    for algo_name, model in trainers_map.items():
        if task_type == "classification":
            metrics = evaluate_classification_model(model, X_val, y_val)
            rank_score = metrics["f1_score"]
        else:
            metrics = evaluate_regression_model(model, X_val, y_val)
            rank_score = -metrics["rmse"]

        results.append({
            "algorithm": algo_name,
            "rank_score": rank_score,
            "metrics": metrics,
            "model": model,
        })

    # Sort descending by rank_score (highest F1 or lowest RMSE)
    results.sort(key=lambda x: x["rank_score"], reverse=True)
    for idx, r in enumerate(results, start=1):
        r["rank"] = idx

    return results
