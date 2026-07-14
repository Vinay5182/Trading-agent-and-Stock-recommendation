from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime

from config import settings, GENUINE_OPEN_STATUSES
from database import close_mongo_connection, connect_to_mongo, get_database
from services.capital_accounting import (
    is_genuine_open_trade,
    trade_open_margin_used,
    trade_open_sl_risk,
    get_current_virtual_balance_and_pnl,
    get_portfolio_totals,
)
from services.position_sizing import calculate_proposed_sizing
from routes.paper import exit_allocations_for_trade
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

logger = logging.getLogger("reconciliation")
MIGRATION_NAME = "paper_reconciliation"

RECONCILIATION_PRECONDITION_FIELDS = {
    "paper_only",
    "status",
    "outcome_status",
    "quantity",
    "original_quantity",
    "quantity_remaining",
    "margin_remaining",
    "open_sl_risk",
    "capital_model_version",
    "updated_at",
}

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Paper Trade Capital & Risk Reconciliation Utility")
    parser.add_argument("--apply", action="store_true", help="Apply cleanup updates to MongoDB")
    parser.add_argument("--limit", type=int, default=None, help="Scanned trades limit")
    parser.add_argument("--plan-output", help="Write preview plan JSON to this path")
    parser.add_argument("--plan-file", help="Approved preview plan JSON for apply mode")
    parser.add_argument("--confirm-plan-sha256", help="Expected canonical preview plan SHA-256")
    parser.add_argument("--backup-manifest", help="Verified backup manifest JSON")
    parser.add_argument("--maintenance-approved", action="store_true", help="Confirm an operator-approved maintenance window")
    parser.add_argument("--runtime-metadata", help="Optional local runtime metadata JSON used to detect active workers")
    return parser.parse_args(argv)

async def build_reconciliation_preview(db, *, limit: int | None = None) -> dict:
    collection = db.paper_trades
    cursor = collection.find({"paper_only": True}).sort("updated_at", -1)
    if limit is not None:
        cursor = cursor.limit(limit)

    trades = [trade async for trade in cursor]
    scanned = len(trades)

    current_balance, realized_pnl = await get_current_virtual_balance_and_pnl(db)
    open_margin, combined_open_risk = await get_portfolio_totals(db)
    available_margin = current_balance - open_margin

    setup_groups: dict[str, list[dict]] = {}
    for trade in trades:
        setup_id = trade.get("setup_id")
        if setup_id and is_genuine_open_trade(trade):
            setup_groups.setdefault(setup_id, []).append(trade)

    duplicate_active_setup_ids = [sid for sid, lst in setup_groups.items() if len(lst) > 1]

    categories = {
        "SAFE": [],
        "CAPITAL_OVERALLOCATED": [],
        "RISK_OVERALLOCATED": [],
        "NEGATIVE_AVAILABLE_CAPITAL": [],
        "SMALL_QUANTITY_INVALID_ALLOCATION": [],
        "RESERVATION_MISSING": [],
        "RESERVATION_DUPLICATE": [],
        "ACCOUNTING_MISMATCH": [],
        "MANUAL_REVIEW_REQUIRED": [],
    }

    operations = []
    now = datetime.utcnow().isoformat()

    for trade in trades:
        trade_id = str(trade["_id"])
        status = str(trade.get("status") or "").upper()
        setup_id = trade.get("setup_id")
        entry_price = trade.get("entry_price") or trade.get("entry") or trade.get("paper_entry_price")
        stop_loss = trade.get("stop_loss") or trade.get("sl")
        qty = trade.get("quantity") or trade.get("original_quantity") or 0

        # Precedence 1: MANUAL_REVIEW_REQUIRED
        if (status in GENUINE_OPEN_STATUSES or status in ("WAITING_FOR_ENTRY", "ENTRY_TRIGGERED", "WAITING_FOR_CAPITAL")) and (entry_price is None or stop_loss is None or float(entry_price) <= 0.0 or float(stop_loss) <= 0.0):
            categories["MANUAL_REVIEW_REQUIRED"].append(trade_id)
            continue

        # Precedence 2: RESERVATION_MISSING
        if status in GENUINE_OPEN_STATUSES:
            if "margin_remaining" not in trade or "open_sl_risk" not in trade:
                categories["RESERVATION_MISSING"].append(trade_id)
                # Propose heal operation
                ep = float(entry_price)
                sl = float(stop_loss)
                q = float(qty)
                exposure = q * ep
                req_margin = exposure / settings.LEVERAGE
                est_risk = q * abs(ep - sl)

                update_doc = {
                    "initial_margin_reserved": req_margin,
                    "margin_remaining": req_margin,
                    "initial_sl_risk": est_risk,
                    "open_sl_risk": est_risk,
                    "capital_model_version": "v2",
                    "margin_released_total": 0.0,
                }
                operations.append(build_update_operation(
                    collection="paper_trades",
                    document=trade,
                    set_fields=update_doc,
                    eligibility_fields=RECONCILIATION_PRECONDITION_FIELDS,
                ))
                continue

        # Precedence 3: RESERVATION_DUPLICATE
        if setup_id in duplicate_active_setup_ids and is_genuine_open_trade(trade):
            categories["RESERVATION_DUPLICATE"].append(trade_id)
            # Duplicate active setup: transition later one to EXPIRED
            if len(setup_groups[setup_id]) > 1 and trade_id != str(setup_groups[setup_id][0]["_id"]):
                update_doc = {
                    "status": "EXPIRED",
                    "outcome_status": "EXPIRED",
                    "state": "EXPIRED",
                    "status_updated_at": now,
                    "capital_rejection_reason": "DUPLICATE_ACTIVE_SETUP",
                    "margin_remaining": 0.0,
                    "open_sl_risk": 0.0,
                }
                operations.append(build_update_operation(
                    collection="paper_trades",
                    document=trade,
                    set_fields=update_doc,
                    eligibility_fields=RECONCILIATION_PRECONDITION_FIELDS,
                ))
            continue

        # Precedence 4: NEGATIVE_AVAILABLE_CAPITAL
        if status in GENUINE_OPEN_STATUSES:
            if available_margin < 0 or open_margin > current_balance or trade.get("margin_remaining", 0.0) < 0 or trade.get("open_sl_risk", 0.0) < 0:
                categories["NEGATIVE_AVAILABLE_CAPITAL"].append(trade_id)
                continue

        # Precedence 5: SMALL_QUANTITY_INVALID_ALLOCATION
        if status in GENUINE_OPEN_STATUSES or status in ("WAITING_FOR_ENTRY", "ENTRY_TRIGGERED", "WAITING_FOR_CAPITAL"):
            allocations = exit_allocations_for_trade(trade)
            if qty < 4 or not allocations.get("valid"):
                categories["SMALL_QUANTITY_INVALID_ALLOCATION"].append(trade_id)
                if status in GENUINE_OPEN_STATUSES or status in ("WAITING_FOR_ENTRY", "ENTRY_TRIGGERED", "WAITING_FOR_CAPITAL"):
                    update_doc = {
                        "status": "EXPIRED",
                        "outcome_status": "EXPIRED",
                        "state": "EXPIRED",
                        "status_updated_at": now,
                        "capital_rejection_reason": "QUANTITY_BELOW_MINIMUM" if qty < 4 else "INVALID_EXIT_ALLOCATION",
                        "margin_remaining": 0.0,
                        "open_sl_risk": 0.0,
                    }
                    operations.append(build_update_operation(
                        collection="paper_trades",
                        document=trade,
                        set_fields=update_doc,
                        eligibility_fields=RECONCILIATION_PRECONDITION_FIELDS,
                    ))
                continue

        # Precedence 6: ACCOUNTING_MISMATCH
        if status in GENUINE_OPEN_STATUSES:
            ep = float(entry_price)
            sl = float(stop_loss)
            q = float(qty)
            qty_rem = float(trade.get("quantity_remaining") if trade.get("quantity_remaining") is not None else q)

            expected_margin = (qty_rem * ep) / settings.LEVERAGE
            expected_risk = qty_rem * abs(ep - sl)

            mismatch = False
            if trade.get("original_quantity") is not None and trade.get("original_quantity") != q:
                mismatch = True
            if abs(trade.get("margin_remaining", 0.0) - expected_margin) > 0.01:
                mismatch = True
            if abs(trade.get("open_sl_risk", 0.0) - expected_risk) > 0.01:
                mismatch = True

            allocations = exit_allocations_for_trade(trade)
            if status == "T1_PARTIAL" and allocations.get("valid"):
                expected_qty_rem = q - allocations["t1_quantity"]
                if qty_rem != expected_qty_rem:
                    mismatch = True

            if mismatch:
                categories["ACCOUNTING_MISMATCH"].append(trade_id)
                update_doc = {
                    "original_quantity": q,
                    "quantity_remaining": qty_rem,
                    "margin_remaining": expected_margin,
                    "open_sl_risk": expected_risk,
                }
                operations.append(build_update_operation(
                    collection="paper_trades",
                    document=trade,
                    set_fields=update_doc,
                    eligibility_fields=RECONCILIATION_PRECONDITION_FIELDS,
                ))
                continue
        elif status not in {"WAITING_FOR_ENTRY", "ENTRY_TRIGGERED", "WAITING_FOR_CAPITAL"} and (trade.get("margin_remaining", 0.0) > 0.0 or trade.get("open_sl_risk", 0.0) > 0.0):
            categories["ACCOUNTING_MISMATCH"].append(trade_id)
            update_doc = {
                "margin_remaining": 0.0,
                "open_sl_risk": 0.0,
            }
            operations.append(build_update_operation(
                collection="paper_trades",
                document=trade,
                set_fields=update_doc,
                eligibility_fields=RECONCILIATION_PRECONDITION_FIELDS,
            ))
            continue

        # Precedence 7: CAPITAL_OVERALLOCATED / RISK_OVERALLOCATED
        if status in GENUINE_OPEN_STATUSES:
            trade_margin = trade_open_margin_used(trade)
            trade_risk = trade_open_sl_risk(trade)
            grade = trade.get("trade_quality_grade") or trade.get("grade")
            grade_clean = str(grade or "").strip().upper().replace(" ", "_").replace("-", "_")

            grade_margin_cap_pct = 10.0
            if grade_clean == "A":
                grade_margin_cap_pct = 8.0
            elif grade_clean == "B":
                grade_margin_cap_pct = 6.0

            grade_margin_cap = current_balance * (grade_margin_cap_pct / 100.0)

            if trade_margin > grade_margin_cap or open_margin > current_balance * (settings.PORTFOLIO_MARGIN_LIMIT_PERCENT / 100.0):
                categories["CAPITAL_OVERALLOCATED"].append(trade_id)
                continue

            if combined_open_risk > current_balance * (settings.PORTFOLIO_RISK_LIMIT_PERCENT / 100.0):
                categories["RISK_OVERALLOCATED"].append(trade_id)
                continue

        # Precedence 8: SAFE
        if status in GENUINE_OPEN_STATUSES or status in ("WAITING_FOR_ENTRY", "ENTRY_TRIGGERED", "WAITING_FOR_CAPITAL"):
            categories["SAFE"].append(trade_id)

    plan = build_preview_plan(
        migration_name=MIGRATION_NAME,
        target_database=db.name,
        operations=operations,
    )

    return {
        "ok": True,
        "apply": False,
        "scanned": scanned,
        "current_balance": current_balance,
        "open_margin": open_margin,
        "available_margin": available_margin,
        "combined_open_risk": combined_open_risk,
        "reconciliation_evidence": {
            "balance": current_balance,
            "open_margin": open_margin,
            "available_margin": available_margin,
            "configured_limit": settings.PORTFOLIO_MARGIN_LIMIT_PERCENT,
            "computed_excess": max(0.0, open_margin - (current_balance * settings.PORTFOLIO_MARGIN_LIMIT_PERCENT / 100.0))
        },
        "categories_summary": {k: len(v) for k, v in categories.items()},
        "categories": categories,
        "proposed_updates_count": len(operations),
        **plan_summary(plan),
    }

async def run_reconciliation(db, *, apply: bool = False, limit: int | None = None, plan: dict | None = None) -> dict:
    if not apply:
        return await build_reconciliation_preview(db, limit=limit)
    if plan is None:
        raise MigrationSafetyError("MIGRATION_PLAN_FILE_REQUIRED", "Reconciliation apply requires approved plan file.")
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
            return await run_reconciliation(db_override, apply=True, plan=controls["plan"])
        result = await run_reconciliation(db_override, apply=False, limit=args.limit)
        write_plan_output(result["plan"], getattr(args, "plan_output", None))
        return safe_json_value(result)

    await connect_to_mongo()
    try:
        db = get_database()
        if args.apply:
            return await run_reconciliation(db, apply=True, plan=controls["plan"])
        result = await run_reconciliation(db, apply=False, limit=args.limit)
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
