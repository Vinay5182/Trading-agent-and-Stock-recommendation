from __future__ import annotations

from services.system_errors import ensure_system_error_indexes


INDEX_DOCUMENTATION = {
    "swing_tv_confirmations_identity_lookup": "Supports saved Swing TV confirmation lookup/upsert filters by symbol/tradingview/index/timeframes hash.",
    "swing_tv_confirmations_lookup": "Supports GET /api/swing/tv-confirmed by index/timeframes and updated_at sort.",
    "momentum_tv_confirmations_identity_lookup": "Supports saved Momentum TV confirmation lookup/upsert filters by symbol/tradingview/index/timeframes hash.",
    "momentum_tv_confirmations_lookup": "Supports GET /api/momentum/tv-confirmed by index/timeframes and updated_at sort.",
    "scan_runs_created_at": "Supports latest scan-run lookup and scan history sorting.",
    "scan_rows_run_score": "Supports GET /api/scan/rows and signal-builder scan row queries.",
    "scan_rows_run_momentum": "Supports momentum signal-builder scan row queries.",
    "paper_update_runs_started_at": "Supports latest paper update run/status queries.",
    "paper_update_runs_scheduled": "Supports latest scheduled dry-run lookup.",
    "scheduler_status_job_name_unique": "Supports one persistent scheduler_status document per job.",
    "paper_market_snapshots_trade_observed_unique": "Prevents duplicate timestamped paper-market snapshots for the same trade.",
    "paper_market_snapshots_trade_observed": "Supports post-setup price lookup for paper trade entry/outcome processing.",
    "paper_market_snapshots_symbol_observed": "Supports symbol-level paper market snapshot audits.",
    "paper_market_snapshots_ttl": "Expires old paper-market snapshots after the configured retention window.",
}


async def ensure_active_indexes(db) -> dict:
    created = []

    async def create(collection_name: str, keys, *, name: str, **kwargs) -> None:
        collection = getattr(db, collection_name, None)
        if collection is None or not hasattr(collection, "create_index"):
            return
        await collection.create_index(keys, name=name, **kwargs)
        created.append({"collection": collection_name, "name": name, "keys": keys, "reason": INDEX_DOCUMENTATION.get(name)})

    await create(
        "swing_tv_confirmations",
        [("symbol", 1), ("tradingview_symbol", 1), ("index_name", 1), ("timeframes_hash", 1)],
        name="swing_tv_confirmations_identity_lookup",
    )
    await create(
        "swing_tv_confirmations",
        [("index_name", 1), ("timeframes_hash", 1), ("updated_at", -1)],
        name="swing_tv_confirmations_lookup",
    )
    await create(
        "momentum_tv_confirmations",
        [("symbol", 1), ("tradingview_symbol", 1), ("index_name", 1), ("timeframes_hash", 1)],
        name="momentum_tv_confirmations_identity_lookup",
    )
    await create(
        "momentum_tv_confirmations",
        [("index_name", 1), ("timeframes_hash", 1), ("updated_at", -1)],
        name="momentum_tv_confirmations_lookup",
    )
    await create("scan_runs", [("created_at", -1)], name="scan_runs_created_at")
    await create("scan_rows", [("scan_run_id", 1), ("selected_for_tv", 1), ("status", 1), ("score", -1)], name="scan_rows_run_score")
    await create("scan_rows", [("scan_run_id", 1), ("momentum_candidate", 1), ("momentum_score", -1)], name="scan_rows_run_momentum")
    await create("paper_update_runs", [("started_at", -1)], name="paper_update_runs_started_at")
    await create("paper_update_runs", [("owner", 1), ("endpoint_mode", 1), ("started_at", -1)], name="paper_update_runs_scheduled")
    await create(
        "paper_market_snapshots",
        [("paper_trade_id", 1), ("observed_at", 1)],
        name="paper_market_snapshots_trade_observed_unique",
        unique=True,
    )
    await create(
        "paper_market_snapshots",
        [("paper_trade_id", 1), ("observed_at", -1)],
        name="paper_market_snapshots_trade_observed",
    )
    await create(
        "paper_market_snapshots",
        [("canonical_symbol", 1), ("observed_at", -1)],
        name="paper_market_snapshots_symbol_observed",
    )
    await create(
        "paper_market_snapshots",
        [("expires_at", 1)],
        name="paper_market_snapshots_ttl",
        expireAfterSeconds=0,
    )
    await create("scheduler_status", "job_name", name="scheduler_status_job_name_unique", unique=True)
    await create("scheduler_status", "updated_at", name="scheduler_status_updated_at")
    await create("scheduler_status", "running", name="scheduler_status_running")
    await ensure_system_error_indexes(db)
    return {"created_or_existing": created, "documentation": INDEX_DOCUMENTATION}
