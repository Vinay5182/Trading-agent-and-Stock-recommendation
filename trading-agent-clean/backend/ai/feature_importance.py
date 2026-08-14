from typing import Any, Dict, List
import numpy as np
import pandas as pd
from sklearn.inspection import permutation_importance


def extract_feature_importance(
    model: Any,
    feature_names: List[str],
    X_val: pd.DataFrame = None,
    y_val: pd.Series = None,
) -> Dict[str, Any]:
    """
    Extracts feature importances from a trained model using model attribute or permutation importance.
    """
    importances = None

    if hasattr(model, "feature_importances_"):
        importances = model.feature_importances_
    elif hasattr(model, "coef_"):
        importances = np.abs(model.coef_).ravel()
    elif X_val is not None and y_val is not None:
        perm_res = permutation_importance(model, X_val, y_val, n_repeats=5, random_state=42)
        importances = perm_res.importances_mean

    if importances is None or len(importances) != len(feature_names):
        # Fallback to uniform
        importances = np.ones(len(feature_names)) / len(feature_names)

    df_imp = pd.DataFrame({
        "feature": feature_names,
        "importance": importances,
    }).sort_values(by="importance", ascending=False).reset_index(drop=True)

    df_imp["rank"] = df_imp.index + 1
    df_imp["importance"] = df_imp["importance"].round(6)

    top_10 = df_imp.head(10).to_dict(orient="records")
    top_20 = df_imp.head(20).to_dict(orient="records")
    top_50 = df_imp.head(50).to_dict(orient="records")

    return {
        "all_features": df_imp.to_dict(orient="records"),
        "top_10": top_10,
        "top_20": top_20,
        "top_50": top_50,
    }
