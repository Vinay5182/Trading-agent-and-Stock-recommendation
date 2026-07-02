AI_FEATURE_CONTRACT_VERSION = "phase3-v1"

APPROVED_MODEL_FEATURES = (
    "rule_score",
    "trend_score",
    "momentum_score",
    "volume_score",
    "risk_score",
    "confidence_score",
    "quality_score",
    "momentum_trap_score",
    "daily_ema20",
    "daily_ema50",
    "rsi14",
    "atr14",
)

FEATURE_METADATA = {
    "rule_score": {
        "canonical_name": "rule_score",
        "expected_type": (int, float),
        "nullable": True,
        "description": "Rule engine score",
        "min_value": 0,
    },
    "trend_score": {
        "canonical_name": "trend_score",
        "expected_type": (int, float),
        "nullable": True,
        "description": "Trend confirmation score",
    },
    "momentum_score": {
        "canonical_name": "momentum_score",
        "expected_type": (int, float),
        "nullable": True,
        "description": "Momentum strength score",
    },
    "volume_score": {
        "canonical_name": "volume_score",
        "expected_type": (int, float),
        "nullable": True,
        "description": "Volume confirmation score",
    },
    "risk_score": {
        "canonical_name": "risk_score",
        "expected_type": (int, float),
        "nullable": True,
        "description": "Strategy risk score",
    },
    "confidence_score": {
        "canonical_name": "confidence_score",
        "expected_type": (int, float),
        "nullable": True,
        "description": "Candidate confidence",
    },
    "quality_score": {
        "canonical_name": "quality_score",
        "expected_type": (int, float),
        "nullable": True,
        "description": "Setup quality score",
    },
    "momentum_trap_score": {
        "canonical_name": "momentum_trap_score",
        "expected_type": (int, float),
        "nullable": True,
        "description": "Momentum trap score",
    },
    "daily_ema20": {
        "canonical_name": "daily_ema20",
        "expected_type": (int, float),
        "nullable": True,
        "description": "Daily EMA 20",
        "min_value": 0.0001,
    },
    "daily_ema50": {
        "canonical_name": "daily_ema50",
        "expected_type": (int, float),
        "nullable": True,
        "description": "Daily EMA 50",
        "min_value": 0.0001,
    },
    "rsi14": {
        "canonical_name": "rsi14",
        "expected_type": (int, float),
        "nullable": True,
        "description": "RSI 14",
        "min_value": 0.0,
        "max_value": 100.0,
    },
    "atr14": {
        "canonical_name": "atr14",
        "expected_type": (int, float),
        "nullable": True,
        "description": "ATR 14",
        "min_value": 0.0,
    },
}

BLOCKED_IDENTIFIERS = {
    "_id", "id", "setup_id", "canonical_setup_id", "paper_trade_id",
    "source_confirmation_id", "snapshot_identity", "training_row_id",
    "run_id", "scan_run_id", "scored_candidate_id", "market_data_id",
    "tv_confirmation_id", "paper_signal_id", "identity_key"
}

BLOCKED_LABEL_FIELDS = {
    "outcome", "outcome_status", "label", "target_hit", "target_hit_level",
    "win", "loss", "result", "realized", "pnl", "paper_pnl", "total_trade_pnl",
    "exit_price", "exit_reason", "stop_reason", "completed_result",
    "ambiguous_outcome", "journal_result", "profit_factor_contribution",
    "outcome_label", "outcome_pnl", "outcome_pnl_percent", "outcome_exit_price",
    "outcome_exit_reason", "outcome_closed_at", "outcome_attached_at",
    "final_status", "realized_pnl", "exit_time", "holding_time", "result_label",
    "label_timestamp", "completed_at", "label_t1_hit", "label_t2_hit",
    "label_t3_hit", "label_sl_hit", "label_ambiguous", "label_excluded",
    "label_exclusion_reason"
}

BLOCKED_LIFECYCLE_FIELDS = {
    "entry_time", "entry_triggered_at", "exit_time", "closed_at", "completed_at",
    "journaled_at", "outcome_attached_at", "label_timestamp", "status_updated_at",
    "status", "quantity_remaining", "partial_exit_state", "current_stop",
    "trailing_stop_state", "setup_status"
}

BLOCKED_ACCOUNT_FIELDS = {
    "virtual_balance", "account_balance", "available_capital", "reserved_capital",
    "reserved_margin", "portfolio_pnl", "open_risk_totals", "bought_quantity",
    "open_quantity", "final_position_size", "account_based_affordability",
    "current_balance", "available_margin", "combined_open_risk", "planned_quantity"
}

BLOCKED_AUDIT_FIELDS = {
    "created_at", "updated_at", "generated_at", "request_time", "server_time",
    "process_id", "project_path", "error_details", "retry_counts", "queue_timings",
    "runtime_diagnostics", "modified_at", "history_enriched_at", "timestamp_quality",
    "timestamp_warnings", "original_timestamp_values", "event_order_valid",
    "timestamp_exclusion_reason", "exclusion_reason", "validation_errors",
    "legacy_record", "source_schema_version", "source_feature_snapshot_version",
    "source_mode", "data_completeness", "data_source_ids", "source_confirmation_created_at"
}
