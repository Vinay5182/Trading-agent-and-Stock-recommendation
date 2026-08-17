from typing import Any, Dict, Tuple
import pandas as pd
import numpy as np

from sklearn.ensemble import (
    RandomForestClassifier,
    RandomForestRegressor,
    GradientBoostingClassifier,
    GradientBoostingRegressor,
    HistGradientBoostingClassifier,
    HistGradientBoostingRegressor,
    ExtraTreesClassifier,
    ExtraTreesRegressor,
)
from sklearn.linear_model import LogisticRegression, Ridge


SUPPORTED_ALGORITHMS = {
    "random_forest": (RandomForestClassifier, RandomForestRegressor),
    "xgboost": (GradientBoostingClassifier, GradientBoostingRegressor),
    "lightgbm": (HistGradientBoostingClassifier, HistGradientBoostingRegressor),
    "extra_trees": (ExtraTreesClassifier, ExtraTreesRegressor),
    "linear": (LogisticRegression, Ridge),
}


def get_model_instance(
    algorithm: str,
    task_type: str = "classification",
    params: Dict[str, Any] = None,
) -> Any:
    algo_clean = algorithm.lower().strip()
    if algo_clean not in SUPPORTED_ALGORITHMS:
        raise ValueError(f"Unsupported algorithm '{algorithm}'. Choose from: {list(SUPPORTED_ALGORITHMS.keys())}")

    clf_cls, reg_cls = SUPPORTED_ALGORITHMS[algo_clean]
    model_cls = clf_cls if task_type == "classification" else reg_cls

    model_params = params or {}
    if algo_clean in {"random_forest", "extra_trees"} and "random_state" not in model_params:
        model_params["random_state"] = 42
    elif algo_clean in {"xgboost", "lightgbm"} and "random_state" not in model_params:
        model_params["random_state"] = 42

    return model_cls(**model_params)


def train_model(
    algorithm: str,
    task_type: str,
    X_train: pd.DataFrame,
    y_train: pd.Series,
    params: Dict[str, Any] = None,
) -> Any:
    """
    Fits specified machine learning model on training data.
    """
    model = get_model_instance(algorithm, task_type=task_type, params=params)
    model.fit(X_train, y_train)
    return model
