from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime

from config import settings, GENUINE_OPEN_STATUSES
from database import close_mongo_connection, connect_to_mongo, get_database
from services.capital_accounting import is_genuine_open_trade
from services.migration_safety import (
    MIGRATION_APPLY_TIMESTAMP,
    MigrationSafetyError,
    apply_migration_plan,
    build_cas_filter,
    build_preview_plan,
    build_update_operation,
    load_and_validate_apply_controls,
    plan_summary,
    safe_json_value,
    write_plan_output,
)
from services.position_sizing import calculate_proposed_sizing

logger = logging.getLogger("uvicorn.error")
MIGRATION_NAME = "capital_backfill"
CAPITAL_PRECONDITION_FIELDS = {
    "paper_only",
    "status",
    "outcome_status",
    "entry_price",
    "entry",
    "paper_entry_price",
    "stop_loss",
    "sl",
    "quantity",
    "original_quantity",
    "quantity_remaining",
    "capital_model_version",
    "proposed_capital_model_version",
    "updated_at",
}

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capital Reservation Backfill Migration Utility")
    parser.add_argument("--apply", action="store_true", help="Apply backfill updates to MongoDB")
    parser.add_argument("--limit", type=int, default=None, help="Scanned trades limit")
    parser.add_argument("--plan-output", help="Write preview plan JSON to this path")
    parser.add_argument("--plan-file", help="Approved preview plan JSON for apply mode")
    parser.add_argument("--confirm-plan-sha256", help="Expected canonical preview plan SHA-256")
    parser.add_argument("--backup-manifest", help="Verified backup manifest JSON")
    parser.add_argument("--maintenance-approved", action="store_true", help="Confirm an operator-approved maintenance window")
    parser.add_argument("--runtime-metadata", help="Optional local runtime metadata JSON used to detect active workers")
    return parser.parse_args(argv)


def stale_row_update_filter(operation: dict) -> dict:
    return build_cas_filter(operation)


async def build_field_level_preview(db, *, limit: int | None = None) -> dict:
    collection = db.paper_trades
    cursor = collection.find({"paper_only": True}).sort("updated_at", -1)
    if limit is not None:
        cursor = cursor.limit(limit)

    trades = [trade async for trade in cursor]
    scanned = len(trades)

    # 1. Group active setups to detect duplicates
    setup_groups: dict[str, list[dict]] = {}
    for trade in trades:
        setup_id = trade.get("setup_id")
        if setup_id and is_genuine_open_trade(trade):
            setup_groups.setdefault(setup_id, []).append(trade)

    duplicate_active_setup_ids = [sid for sid, lst in setup_groups.items() if len(lst) > 1]
    duplicates_detected = 0
    for sid in duplicate_active_setup_ids:
        duplicates_detected += len(setup_groups[sid]) - 1

    operations = []
    skipped = 0
    errors = []

    # Terminal statuses that release all capital
    TERMINAL_STATUSES = {
        "AMBIGUOUS", "CLOSED", "COMPLETED", "EXPIRED", "LOST_SL", "SL_HIT", "STOP_HIT",
        "STOPPED", "STOPPED_AFTER_T1", "T1_HIT", "T2_HIT", "T3_HIT", "TARGET_HIT",
        "TARGET_1_HIT_FINAL", "TARGET_2_HIT", "TARGET_3_HIT", "WON_T1", "WON_T2", "WON_T3"
    }

    for trade in trades:
        trade_id = str(trade["_id"])
        status = str(trade.get("status") or "").upper()

        # Determine baseline variables
        entry = trade.get("entry_price") or trade.get("entry") or trade.get("paper_entry_price")
        sl = trade.get("stop_loss") or trade.get("sl")
        q = trade.get("quantity") or trade.get("original_quantity")
        q_rem = trade.get("quantity_remaining") if trade.get("quantity_remaining") is not None else q

        if status in GENUINE_OPEN_STATUSES:
            # Active/Partial trade backfill
            if not entry or not sl or q is None:
                errors.append({"trade_id": trade_id, "reason": "MISSING_LEVELS_OR_QUANTITY"})
                continue

            entry_val = float(entry)
            sl_val = float(sl)
            q_val = float(q)
            q_rem_val = float(q_rem)

            # Proportional accounting logic
            exposure = q_val * entry_val
            initial_margin = max(settings.MINIMUM_ENTRY_MARGIN, exposure / settings.LEVERAGE)

            if q_val > 0:
                rem_ratio = q_rem_val / q_val
            else:
                rem_ratio = 1.0

            margin_rem = initial_margin * rem_ratio
            initial_risk = q_val * abs(entry_val - sl_val)
            open_risk = q_rem_val * abs(entry_val - sl_val)
            margin_released = initial_margin - margin_rem

            update_doc = {
                "original_quantity": q_val,
                "quantity_remaining": q_rem_val,
                "initial_margin_reserved": initial_margin,
                "margin_remaining": margin_rem,
                "initial_sl_risk": initial_risk,
                "open_sl_risk": open_risk,
                "margin_released_total": margin_released,
                "capital_model_version": "v2",
            }
            # Only add missing fields or model upgrade
            if trade.get("capital_model_version") != "v2":
                operations.append(build_update_operation(
                    collection="paper_trades",
                    document=trade,
                    set_fields=update_doc,
                    eligibility_fields=CAPITAL_PRECONDITION_FIELDS,
                ))
            else:
                skipped += 1

        elif status == "WAITING_FOR_ENTRY":
            # Waiting trade backfill: proposed sizing + zero reserved
            if not entry or not sl:
                # If levels are missing, skip or use defaults
                proposed_qty = 0
                proposed_exposure = 0.0
                proposed_margin = 0.0
                proposed_risk = 0.0
            else:
                grade = trade.get("trade_quality_grade") or trade.get("grade")
                sizing = calculate_proposed_sizing(
                    entry_price=float(entry),
                    stop_loss=float(sl),
                    grade=grade,
                    current_balance=settings.STARTING_VIRTUAL_BALANCE,
                    available_margin=settings.STARTING_VIRTUAL_BALANCE,
                    open_margin=0.0,
                    combined_open_risk=0.0,
                    paper_mode=True,
                )
                if sizing["ok"]:
                    proposed_qty = sizing["final_quantity"]
                    proposed_exposure = sizing["exposure"]
                    proposed_margin = sizing["required_margin"]
                    proposed_risk = sizing["estimated_sl_risk"]
                else:
                    proposed_qty = 0
                    proposed_exposure = 0.0
                    proposed_margin = 0.0
                    proposed_risk = 0.0

            update_doc = {
                "proposed_quantity": proposed_qty,
                "proposed_exposure": proposed_exposure,
                "proposed_margin": proposed_margin,
                "proposed_sl_risk": proposed_risk,
                "proposed_capital_model_version": "v2",

                # Zeros for WAITING_FOR_ENTRY
                "margin_remaining": 0.0,
                "open_sl_risk": 0.0,
                "initial_margin_reserved": 0.0,
                "initial_sl_risk": 0.0,
                "margin_released_total": 0.0,
                "quantity": proposed_qty,
                "quantity_remaining": proposed_qty,
            }
            if trade.get("proposed_capital_model_version") != "v2":
                operations.append(build_update_operation(
                    collection="paper_trades",
                    document=trade,
                    set_fields=update_doc,
                    eligibility_fields=CAPITAL_PRECONDITION_FIELDS,
                ))
            else:
                skipped += 1

        elif status in TERMINAL_STATUSES:
            # Terminal trades backfill
            q_val = float(q) if q is not None else 0.0
            entry_val = float(entry) if entry is not None else 0.0
            sl_val = float(sl) if sl is not None else 0.0

            exposure = q_val * entry_val
            initial_margin = max(settings.MINIMUM_ENTRY_MARGIN, exposure / settings.LEVERAGE) if (entry_val and q_val) else 0.0
            initial_risk = q_val * abs(entry_val - sl_val) if (entry_val and sl_val and q_val) else 0.0

            update_doc = {
                "margin_remaining": 0.0,
                "open_sl_risk": 0.0,
                "initial_margin_reserved": trade.get("initial_margin_reserved", initial_margin),
                "initial_sl_risk": trade.get("initial_sl_risk", initial_risk),
                "margin_released_total": trade.get("initial_margin_reserved", initial_margin),
                "capital_model_version": "v2",
            }
            if trade.get("capital_model_version") != "v2":
                operations.append(build_update_operation(
                    collection="paper_trades",
                    document=trade,
                    set_fields=update_doc,
                    eligibility_fields=CAPITAL_PRECONDITION_FIELDS,
                ))
            else:
                skipped += 1
        else:
            skipped += 1

    plan = build_preview_plan(
        migration_name=MIGRATION_NAME,
        target_database=db.name,
        operations=operations,
    )

    return {
        "ok": len(errors) == 0,
        "apply": False,
        "scanned": scanned,
        "proposed_updates_count": len(operations),
        "applied_updates_count": 0,
        "skipped_count": skipped,
        "duplicates_detected": duplicates_detected,
        "duplicate_active_setup_ids": duplicate_active_setup_ids,
        "errors_count": len(errors),
        "errors": errors,
        **plan_summary(plan),
    }


async def backfill_capital_accounting(
    db,
    *,
    apply: bool = False,
    limit: int | None = None,
    plan: dict | None = None,
) -> dict:
    if not apply:
        return await build_field_level_preview(db, limit=limit)
    if plan is None:
        raise MigrationSafetyError("MIGRATION_PLAN_FILE_REQUIRED", "Capital backfill apply requires an approved plan.")
    return await apply_migration_plan(db, plan)


async def run(args: argparse.Namespace | None = None, *, db_override=None) -> dict:
    args = args or parse_args()
    target_database = getattr(db_override, "name", settings.DATABASE_NAME)
    controls = None
    if args.apply:
        controls = load_and_validate_apply_controls(
            args=args,
            migration_name=MIGRATION_NAME,
            target_database=target_database,
        )
    if db_override is not None:
        if args.apply:
            return await backfill_capital_accounting(db_override, apply=True, plan=controls["plan"])
        result = await backfill_capital_accounting(db_override, apply=False, limit=args.limit)
        write_plan_output(result["plan"], getattr(args, "plan_output", None))
        return safe_json_value(result)

    await connect_to_mongo()
    try:
        db = get_database()
        if args.apply:
            return await backfill_capital_accounting(db, apply=True, plan=controls["plan"])
        result = await backfill_capital_accounting(db, apply=False, limit=args.limit)
        write_plan_output(result["plan"], getattr(args, "plan_output", None))
        return safe_json_value(result)
    finally:
        await close_mongo_connection()


def main(argv: list[str] | None = None) -> None:
    try:
        result = asyncio.run(run(parse_args(argv)))
    except MigrationSafetyError as exc:
        result = {"ok": False, "code": exc.code, "message": exc.message, "details": exc.details}
    print(json.dumps(result, indent=2, default=str))

if __name__ == "__main__":
    main()
