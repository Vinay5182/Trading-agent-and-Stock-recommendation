import os
import json
import joblib
import numpy as np

_loaded_model = None
_loaded_meta = None

def load_latest_model():
    global _loaded_model, _loaded_meta
    
    artifacts_dir = os.path.join(os.path.dirname(__file__), "artifacts")
    if not os.path.exists(artifacts_dir):
        raise FileNotFoundError("Artifacts directory not found.")
        
    meta_files = [f for f in os.listdir(artifacts_dir) if f.endswith("_meta.json")]
    if not meta_files:
        raise FileNotFoundError("No model metadata found.")
        
    # Sort by modification time
    meta_files.sort(key=lambda x: os.path.getmtime(os.path.join(artifacts_dir, x)), reverse=True)
    latest_meta_file = meta_files[0]
    
    with open(os.path.join(artifacts_dir, latest_meta_file), "r") as f:
        meta = json.load(f)
        
    model_id = latest_meta_file.replace("_meta.json", "")
    model_path = os.path.join(artifacts_dir, f"{model_id}.joblib")
    
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"Model file {model_path} not found.")
        
    model = joblib.load(model_path)
    
    _loaded_model = model
    _loaded_meta = meta
    
    return model, meta

def predict_outcome(feature_row: dict) -> dict:
    if _loaded_model is None or _loaded_meta is None:
        load_latest_model()
        
    features = _loaded_meta.get("features", [])
    if not features:
        raise ValueError("Model metadata missing features list.")
        
    x_row = []
    for k in features:
        val = feature_row.get(k)
        x_row.append(float(val) if val is not None else 0.0)
        
    X = np.array([x_row])
    
    prediction = _loaded_model.predict(X)[0]
    probabilities = _loaded_model.predict_proba(X)[0]
    classes = _loaded_model.classes_
    
    confidence = {str(c): float(p) for c, p in zip(classes, probabilities)}
    
    return {
        "prediction": str(prediction),
        "confidence": confidence,
        "model_used": _loaded_meta.get("timestamp", "unknown")
    }
