import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
import joblib

backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from ai.dataset_loader import load_versioned_dataset
from ai.dataset_preprocessor import DatasetPreprocessor
from ai.model_trainers import train_model, SUPPORTED_ALGORITHMS
from ai.benchmarking import (
    evaluate_classification_model,
    evaluate_regression_model,
    benchmark_all_models,
)
from ai.feature_importance import extract_feature_importance


def run_training_pipeline(
    dataset_path: str,
    algorithm: str = "random_forest",
    task_type: str = "classification",
    target_column: str = "binary_label",
    output_dir: str = "models",
    benchmark_mode: bool = False,
    dry_run: bool = False,
):
    start_time = time.time()
    val_dataset_path = dataset_path.replace("training_", "validation_")
    if not os.path.exists(val_dataset_path):
        val_dataset_path = dataset_path

    print(f"Loading training dataset from '{dataset_path}'...")
    X_train_raw, y_train, meta_train, manifest = load_versioned_dataset(dataset_path, target_column=target_column)
    X_val_raw, y_val, meta_val, _ = load_versioned_dataset(val_dataset_path, target_column=target_column)

    if X_val_raw.empty and "training_" in dataset_path:
        full_dataset_path = dataset_path.replace("training_", "full_")
        if os.path.exists(full_dataset_path):
            X_full_raw, y_full, meta_full, _ = load_versioned_dataset(full_dataset_path, target_column=target_column)
            # Pick last 20% of valid labeled rows for validation
            n_full = len(X_full_raw)
            n_val = int(n_full * 0.20)
            if n_val > 0:
                X_val_raw = X_full_raw.iloc[-n_val:].copy()
                y_val = y_full.iloc[-n_val:].copy()
                meta_val = meta_full.iloc[-n_val:].copy()

    print(f"Dataset Loaded: Train shape={X_train_raw.shape}, Val shape={X_val_raw.shape}")


    if dry_run:
        print("\n=== DRY RUN PREVIEW ===")
        print(f"Task Type:     {task_type}")
        print(f"Target Column: {target_column}")
        print(f"Algorithms:    {list(SUPPORTED_ALGORITHMS.keys()) if benchmark_mode else [algorithm]}")
        print(f"Feature Count: {X_train_raw.shape[1]}")
        return {"dry_run": True}

    # Preprocessing
    preprocessor = DatasetPreprocessor()
    X_train = preprocessor.fit_transform(X_train_raw)
    X_val = preprocessor.transform(X_val_raw)

    print(f"Preprocessed Feature Matrix: {X_train.shape[1]} input features")

    trained_models_map = {}
    benchmark_results = []

    if benchmark_mode:
        print("\nRunning Multi-Algorithm Benchmark...")
        for algo in SUPPORTED_ALGORITHMS.keys():
            try:
                m = train_model(algo, task_type, X_train, y_train)
                trained_models_map[algo] = m
            except Exception as e:
                print(f"Error training {algo}: {e}")

        benchmark_results = benchmark_all_models(
            trained_models_map, X_train, y_train, X_val, y_val, task_type=task_type
        )
        best_result = benchmark_results[0]
        best_algo = best_result["algorithm"]
        best_model = best_result["model"]
    else:
        best_algo = algorithm
        best_model = train_model(algorithm, task_type, X_train, y_train)
        if task_type == "classification":
            metrics = evaluate_classification_model(best_model, X_val, y_val)
        else:
            metrics = evaluate_regression_model(best_model, X_val, y_val)
        best_result = {"algorithm": best_algo, "metrics": metrics, "model": best_model}

    duration = round(time.time() - start_time, 2)

    # Feature Importance
    feat_imp = extract_feature_importance(best_model, list(X_train.columns), X_val, y_val)

    os.makedirs(output_dir, exist_ok=True)
    version = manifest.get("dataset_version", "v1.0.0")

    model_path = os.path.join(output_dir, f"model_{best_algo}_{task_type}_{version}.joblib")
    prep_path = os.path.join(output_dir, f"preprocessor_{version}.joblib")
    imp_path = os.path.join(output_dir, f"feature_importance_{best_algo}_{version}.json")
    manifest_path = os.path.join(output_dir, "training_manifest.json")

    joblib.dump(best_model, model_path)
    preprocessor.save(prep_path)

    with open(imp_path, "w", encoding="utf-8") as f:
        json.dump(feat_imp, f, indent=2)

    training_manifest = {
        "dataset_version": version,
        "schema_hash": manifest.get("schema_hash"),
        "training_timestamp": datetime.now(timezone.utc).isoformat(),
        "task_type": task_type,
        "target_column": target_column,
        "best_algorithm": best_algo,
        "metrics": best_result["metrics"],
        "feature_count": X_train.shape[1],
        "training_duration_seconds": duration,
        "model_artifact": model_path,
        "preprocessor_artifact": prep_path,
        "feature_importance_artifact": imp_path,
        "benchmark_summary": [
            {"rank": b.get("rank"), "algorithm": b.get("algorithm"), "metrics": b.get("metrics")}
            for b in benchmark_results
        ],
    }

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(training_manifest, f, indent=2)

    print("\n=== TRAINING & BENCHMARK RESULTS ===")
    print(f"Best Algorithm:    {best_algo}")
    print(f"Training Duration: {duration}s")
    print("Validation Metrics:")
    for k, v in best_result["metrics"].items():
        print(f"  {k:18s}: {v}")

    if benchmark_results:
        print("\nBenchmark Rankings:")
        for b in benchmark_results:
            score_str = f"F1={b['metrics'].get('f1_score')}" if task_type == "classification" else f"RMSE={b['metrics'].get('rmse')}"
            print(f"  Rank {b['rank']}: {b['algorithm']:15s} -> {score_str}")

    print("\nTop 10 Important Features:")
    for f_info in feat_imp["top_10"]:
        print(f"  Rank {f_info['rank']:2d}: {f_info['feature']:25s} ({f_info['importance']:.4f})")

    print(f"\nModel artifacts successfully saved to '{output_dir}/'.")
    return training_manifest


def main():
    parser = argparse.ArgumentParser(description="Train and benchmark ML models on versioned dataset exports.")
    parser.add_argument("--dataset", type=str, default="datasets/training_dataset_v1.0.0.csv", help="Path to training dataset CSV.")
    parser.add_argument("--algorithm", type=str, default="random_forest", help="Model algorithm (random_forest, xgboost, lightgbm, extra_trees, linear).")
    parser.add_argument("--output", type=str, default="models", help="Directory to save trained model artifacts.")
    parser.add_argument("--classification", action="store_true", default=True, help="Perform classification task.")
    parser.add_argument("--regression", action="store_true", help="Perform regression task.")
    parser.add_argument("--target", type=str, default="binary_label", help="Target column name.")
    parser.add_argument("--benchmark", action="store_true", help="Run benchmark across all supported algorithms.")
    parser.add_argument("--dry-run", action="store_true", help="Preview pipeline without training models.")

    args = parser.parse_args()

    task_type = "regression" if args.regression else "classification"
    target_col = args.target if args.target != "binary_label" else ("binary_label" if task_type == "classification" else "future_return_10d")

    run_training_pipeline(
        dataset_path=args.dataset,
        algorithm=args.algorithm,
        task_type=task_type,
        target_column=target_col,
        output_dir=args.output,
        benchmark_mode=args.benchmark,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
