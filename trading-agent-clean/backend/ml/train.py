import os
import json
import joblib
from datetime import datetime, timezone
import numpy as np
from sklearn.ensemble import RandomForestClassifier

from database import get_database

async def run_model_training() -> dict:
    db = get_database()
    cursor = db.ai_feature_snapshots.find({
        "result_label": {"$in": ["WIN", "LOSS", "BREAKEVEN"]}
    })
    snapshots = [row async for row in cursor]
    
    if not snapshots:
        return {"accuracy": 0.0, "dataset_size": 0, "top_feature": None}

    feature_keys = ["rule_score", "trend_score", "momentum_score", "volume_score", "risk_score"]
    
    X = []
    y = []
    
    for row in snapshots:
        x_row = []
        for k in feature_keys:
            val = row.get(k)
            x_row.append(float(val) if val is not None else 0.0)
        X.append(x_row)
        y.append(row["result_label"])
        
    X = np.array(X)
    y = np.array(y)
    
    model = RandomForestClassifier(max_depth=3, n_estimators=50, random_state=42)
    model.fit(X, y)
    
    accuracy = float(model.score(X, y))
    
    importances = model.feature_importances_
    top_feature_idx = int(np.argmax(importances))
    top_feature = feature_keys[top_feature_idx]
    
    model_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_rf"
    
    artifacts_dir = os.path.join(os.path.dirname(__file__), "artifacts")
    os.makedirs(artifacts_dir, exist_ok=True)
    
    model_path = os.path.join(artifacts_dir, f"{model_id}.joblib")
    meta_path = os.path.join(artifacts_dir, f"{model_id}_meta.json")
    
    joblib.dump(model, model_path)
    
    meta_data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dataset_size": len(X),
        "accuracy": accuracy,
        "top_feature": top_feature,
        "features": feature_keys
    }
    
    with open(meta_path, "w") as f:
        json.dump(meta_data, f, indent=2)
    
    return {
        "model_id": model_id,
        "accuracy": accuracy,
        "dataset_size": len(X),
        "top_feature": top_feature,
        "model_path": model_path,
        "meta_path": meta_path
    }
