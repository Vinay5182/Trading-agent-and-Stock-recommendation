import argparse
import asyncio
import os
import sys

backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from database import connect_to_mongo, close_mongo_connection, get_database
from services.ml_dataset_exporter import export_versioned_ml_dataset


async def run_exporter(
    output_dir: str,
    version: str,
    format_parquet: bool,
    format_csv: bool,
    train_only: bool,
    dry_run: bool,
):
    await connect_to_mongo()
    db = get_database()

    print(f"Starting ML Dataset Export pipeline (version={version}, dry_run={dry_run})...")
    result = await export_versioned_ml_dataset(
        db,
        output_dir=output_dir,
        version=version,
        format_parquet=format_parquet,
        format_csv=format_csv,
        train_only=train_only,
        dry_run=dry_run,
    )

    manifest = result.get("manifest", {})
    print("\n=== DATASET EXPORT MANIFEST ===")
    print(f"Dataset Version:  {manifest.get('dataset_version')}")
    print(f"Schema Hash:      {manifest.get('schema_hash')}")
    print(f"Total Records:    {manifest.get('total_records')}")
    print(f"Feature Count:    {manifest.get('feature_count')}")
    print(f"Target Count:     {manifest.get('target_count')}")
    print(f"Split Counts:")
    for split, count in manifest.get("split_counts", {}).items():
        dr = manifest.get("date_ranges", {}).get(split, [])
        dr_str = f" ({dr[0]} to {dr[1]})" if dr else ""
        print(f"  {split:12s}: {count:4d} records{dr_str}")

    if not dry_run:
        print(f"\nOutput Directory: {result.get('output_directory')}")
        print("Exported Files:")
        for fpath in result.get("exported_files", []):
            print(f"  - {fpath}")

    await close_mongo_connection()
    return result


def main():
    parser = argparse.ArgumentParser(description="Export versioned ML training datasets from candidate_trade_outcomes.")
    parser.add_argument("--output-dir", type=str, default="datasets", help="Directory to save exported dataset files.")
    parser.add_argument("--version", type=str, default="v1.0.0", help="Dataset version identifier.")
    parser.add_argument("--parquet", action="store_true", default=True, help="Export Parquet format (default True).")
    parser.add_argument("--csv", action="store_true", default=False, help="Export CSV format.")
    parser.add_argument("--train-only", action="store_true", help="Export training split only.")
    parser.add_argument("--dry-run", action="store_true", help="Preview dataset counts and manifest without writing files.")
    args = parser.parse_args()

    asyncio.run(
        run_exporter(
            output_dir=args.output_dir,
            version=args.version,
            format_parquet=args.parquet,
            format_csv=args.csv,
            train_only=args.train_only,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()
