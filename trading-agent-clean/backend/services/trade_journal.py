from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import mean

from pymongo.errors import DuplicateKeyError


TERMINAL_STATUSES = {
    "AMBIGUOUS",
    "CLOSED",
    "COMPLETED",
    "EXPIRED",
    "LOST_SL",
    "SL_HIT",
    "STOP_HIT",
    "STOPPED",
    "STOPPED_AFTER_T1",
    "T1_HIT",
    "T2_HIT",
    "T3_HIT",
    "TARGET_HIT",
    "TARGET_1_HIT_FINAL",
    "TARGET_2_HIT",
    "TARGET_3_HIT",
    "WON_T1",
    "WON_T2",
    "WON_T3",
}
TARGET_STATUSES = {
    "COMPLETED",
    "T1_HIT",
    "T2_HIT",
    "T3_HIT",
    "TARGET_HIT",
    "TARGET_1_HIT_FINAL",
    "TARGET_2_HIT",
    "TARGET_3_HIT",
    "WON_T1",
    "WON_T2",
    "WON_T3",
}
LOSS_STATUSES = {"SL_HIT", "LOST_SL", "STOP_HIT", "STOPPED", "STOPPED_AFTER_T1"}
GRADE_BUCKETS = ("A+", "A", "B", "C")
STRATEGY_BUCKETS = ("Swing", "Momentum")
TRAP_BUCKETS = ("CLEAN", "CAUTION", "DANGER")
STOPPED_ANALYTICS_TREATMENT = "STOPPED and STOPPED_AFTER_T1 are treated as losses unless the record is AMBIGUOUS."


class JournalWriteError(RuntimeError):
    pass


def normalize_status(value) -> str:
    return str(value or "").upper()


def trade_statuses(trade: dict) -> set[str]:
    return {
        status
        for status in (
            normalize_status(trade.get("status")),
            normalize_status(trade.get("outcome_status")),
            normalize_status(trade.get("state")),
        )
        if status
    }


def is_completed_trade(trade: dict) -> bool:
    return bool(trade_statuses(trade) & TERMINAL_STATUSES)


def is_ambiguous_trade(record: dict) -> bool:
    return bool(record.get("ambiguous")) or "AMBIGUOUS" in trade_statuses(record) or normalize_status(record.get("exit_reason")) == "AMBIGUOUS"


def analytics_eligible_record(record: dict) -> bool:
    return not is_ambiguous_trade(record)


def number_or_none(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def first_number(*values) -> float | None:
    for value in values:
        numeric = number_or_none(value)
        if numeric is not None:
            return numeric
    return None


def analytics_pnl_value(record: dict) -> float | None:
    return first_number(
        record.get("total_trade_pnl"),
        record.get("realized_pnl"),
        record.get("paper_pnl"),
        record.get("profit_percent"),
    )


def analytics_profit_percent_value(record: dict) -> float | None:
    return first_number(
        record.get("profit_percent"),
        record.get("paper_pnl_percent"),
        record.get("total_trade_pnl"),
        record.get("paper_pnl"),
    )


def parse_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def days_between(start, end) -> int | None:
    start_dt = parse_datetime(start)
    end_dt = parse_datetime(end)
    if start_dt is None or end_dt is None:
        return None
    return max((end_dt.date() - start_dt.date()).days, 0)


def strategy_type_for_trade(trade: dict) -> str:
    text = str(trade.get("strategy_type") or trade.get("source_signal_type") or "").upper()
    return "Momentum" if "MOMENTUM" in text else "Swing"


def grade_for_trade(trade: dict) -> str:
    text = str(trade.get("grade") or trade.get("trade_quality_grade") or "").strip().upper()
    if text in {"A+", "A_PLUS", "A PLUS", "APLUS"}:
        return "A+"
    return text or "UNKNOWN"


def risk_value(trade: dict, field: str) -> str | None:
    risk_summary = trade.get("risk_summary") if isinstance(trade.get("risk_summary"), dict) else {}
    value = trade.get(field)
    if value is None:
        value = risk_summary.get(field)
    return str(value).upper() if value not in (None, "") else None


def trap_status_for_trade(trade: dict) -> str:
    risk_summary = trade.get("risk_summary") if isinstance(trade.get("risk_summary"), dict) else {}
    value = trade.get("trap_status") or risk_summary.get("trap_status")
    text = str(value or "CLEAN").upper()
    if "DANGER" in text:
        return "DANGER"
    if "CAUTION" in text or "WARN" in text:
        return "CAUTION"
    return "CLEAN"


def partial_exits_for_trade(trade: dict) -> dict:
    return {
        field: trade.get(field)
        for field in ("partial_exit_1", "partial_exit_2", "partial_exit_3")
        if trade.get(field)
    }


def planned_rr(trade: dict) -> float | None:
    direct = first_number(
        trade.get("RR"),
        trade.get("risk_reward"),
        trade.get("risk_reward_3"),
        trade.get("paper_rr_3"),
        trade.get("risk_reward_2"),
        trade.get("paper_rr_2"),
        trade.get("risk_reward_1"),
        trade.get("paper_rr_1"),
        trade.get("rr"),
    )
    if direct is not None:
        return direct
    entry = number_or_none(trade.get("entry_price"))
    stop = number_or_none(trade.get("initial_stop_loss") or trade.get("stop_loss"))
    target = number_or_none(trade.get("target_3") or trade.get("target_2") or trade.get("target_1"))
    if entry is None or stop is None or target is None or entry <= stop:
        return None
    return (target - entry) / (entry - stop)


def profit_percent_for_trade(trade: dict) -> float:
    direct = number_or_none(trade.get("profit_percent") or trade.get("paper_pnl_percent"))
    if direct is not None:
        return direct
    pnl = number_or_none(trade.get("paper_pnl"))
    entry = number_or_none(trade.get("entry_price"))
    quantity = number_or_none(trade.get("quantity"))
    if pnl is None or entry is None or quantity in (None, 0):
        return 0.0
    return (pnl / (entry * quantity)) * 100


def journal_record_from_trade(trade: dict, *, journaled_at: str | None = None) -> dict:
    statuses = trade_statuses(trade)
    entry_date = trade.get("entry_triggered_at") or trade.get("created_at")
    exit_date = trade.get("status_updated_at") or trade.get("updated_at") or trade.get("exit_date")
    partial_exits = partial_exits_for_trade(trade)
    t3_hit = "T3_HIT" in statuses or bool(partial_exits.get("partial_exit_3"))
    t2_hit = t3_hit or "T2_HIT" in statuses or bool(partial_exits.get("partial_exit_2"))
    t1_hit = t2_hit or "T1_HIT" in statuses or bool(partial_exits.get("partial_exit_1"))
    sl_hit = bool(statuses & LOSS_STATUSES)
    now = journaled_at or datetime.utcnow().isoformat()
    paper_trade_id = str(trade.get("_id") or trade.get("paper_trade_id") or "")
    if not paper_trade_id:
        paper_trade_id = f"{trade.get('symbol')}:{entry_date}:{exit_date}"
    return {
        "paper_trade_id": paper_trade_id,
        "symbol": trade.get("symbol"),
        "strategy_type": strategy_type_for_trade(trade),
        "entry": number_or_none(trade.get("entry_price")),
        "stop_loss": number_or_none(trade.get("initial_stop_loss") or trade.get("stop_loss")),
        "T1": number_or_none(trade.get("target_1")),
        "T2": number_or_none(trade.get("target_2")),
        "T3": number_or_none(trade.get("target_3")),
        "entry_date": entry_date,
        "exit_date": exit_date,
        "exit_reason": trade.get("exit_reason") or trade.get("outcome_status") or trade.get("status"),
        "SL_HIT": sl_hit,
        "T1_HIT": t1_hit,
        "T2_HIT": t2_hit,
        "T3_HIT": t3_hit,
        "grade": grade_for_trade(trade),
        "RR": first_number(trade.get("realized_rr"), planned_rr(trade)),
        "realized_rr": number_or_none(trade.get("realized_rr")),
        "initial_risk_amount": number_or_none(trade.get("initial_risk_amount")),
        "realized_pnl": number_or_none(trade.get("realized_pnl")),
        "remaining_unrealized_pnl": number_or_none(trade.get("remaining_unrealized_pnl")),
        "total_trade_pnl": number_or_none(trade.get("total_trade_pnl")),
        "invested_amount": number_or_none(trade.get("invested_amount")),
        "trap_status": trap_status_for_trade(trade),
        "fake_breakout_risk": risk_value(trade, "fake_breakout_risk"),
        "retail_trap_risk": risk_value(trade, "retail_trap_risk"),
        "partial_exits": partial_exits,
        "profit_percent": profit_percent_for_trade(trade),
        "paper_pnl": number_or_none(trade.get("total_trade_pnl") or trade.get("paper_pnl")) or 0.0,
        "ambiguous": "AMBIGUOUS" in statuses,
        "days_held": days_between(entry_date, exit_date),
        "status": trade.get("status"),
        "outcome_status": trade.get("outcome_status"),
        "journaled_at": now,
        "created_at": now,
        "immutable": True,
    }


def journal_collection(db):
    return getattr(db, "trade_journal", None)


async def ensure_trade_journal_indexes(db) -> None:
    collection = journal_collection(db)
    if collection is None or not hasattr(collection, "create_index"):
        raise JournalWriteError("NO_JOURNAL_COLLECTION")
    await collection.create_index("paper_trade_id", unique=True, name="trade_journal_paper_trade_id_unique")
    await collection.create_index("exit_date", name="trade_journal_exit_date")
    await collection.create_index("strategy_type", name="trade_journal_strategy_type")


async def journal_completed_trade(db, trade: dict) -> dict:
    if not is_completed_trade(trade):
        return {"journaled": False, "duplicate": False, "reason": "NOT_COMPLETED"}
    collection = journal_collection(db)
    if collection is None or not hasattr(collection, "insert_one"):
        raise JournalWriteError("NO_JOURNAL_COLLECTION")
    await ensure_trade_journal_indexes(db)
    record = journal_record_from_trade(trade)
    try:
        await collection.insert_one(record)
    except DuplicateKeyError:
        return {
            "journaled": False,
            "duplicate": True,
            "reason": "ALREADY_JOURNALED",
            "paper_trade_id": record["paper_trade_id"],
        }
    except Exception as exc:
        raise JournalWriteError(f"JOURNAL_INSERT_FAILED: {exc}") from exc
    return {"journaled": True, "duplicate": False, "record": record, "paper_trade_id": record["paper_trade_id"]}


async def sync_completed_trades_to_journal(db, limit: int = 1000) -> dict:
    trades_collection = getattr(db, "paper_trades", None)
    collection = journal_collection(db)
    if trades_collection is None or collection is None:
        return {"ok": False, "processed": 0, "journaled": 0, "duplicates": 0, "skipped": 0, "errors_count": 1, "errors": [{"reason": "NO_JOURNAL_COLLECTION"}]}
    processed = journaled = duplicates = skipped = errors_count = 0
    errors = []
    await ensure_trade_journal_indexes(db)
    cursor = trades_collection.find({"paper_only": True}).sort("updated_at", -1).limit(limit)
    async for trade in cursor:
        if not is_completed_trade(trade):
            skipped += 1
            continue
        processed += 1
        try:
            result = await journal_completed_trade(db, trade)
        except JournalWriteError as exc:
            errors_count += 1
            errors.append({"paper_trade_id": str(trade.get("_id") or trade.get("paper_trade_id") or ""), "error": str(exc)})
            continue
        if result.get("journaled"):
            journaled += 1
        elif result.get("duplicate"):
            duplicates += 1
        else:
            skipped += 1
        if result.get("journaled") or result.get("duplicate"):
            if trade.get("journal_pending") or trade.get("journal_status") == "PENDING":
                await trades_collection.update_one(
                    {"_id": trade["_id"], "paper_only": True},
                    {
                        "$set": {
                            "journal_status": "JOURNALED",
                            "journal_pending": False,
                            "completion_pending": False,
                            "journaled_at": datetime.utcnow().isoformat(),
                            "journal_paper_trade_id": result.get("paper_trade_id"),
                        }
                    },
                    upsert=False,
                )
    return {
        "ok": errors_count == 0,
        "processed": processed,
        "journaled": journaled,
        "duplicates": duplicates,
        "skipped": skipped,
        "errors_count": errors_count,
        "errors": errors,
    }


async def load_trade_journal(db, limit: int = 1000) -> list[dict]:
    collection = journal_collection(db)
    if collection is None:
        return []
    cursor = collection.find({}, {"_id": 0}).sort("exit_date", -1).limit(limit)
    return [row async for row in cursor]


def compact_trade(record: dict | None) -> dict | None:
    if not record:
        return None
    return {
        "symbol": record.get("symbol"),
        "strategy_type": record.get("strategy_type"),
        "grade": record.get("grade"),
        "trap_status": record.get("trap_status"),
        "profit_percent": round(analytics_profit_percent_value(record) or 0.0, 2),
        "total_trade_pnl": round(analytics_pnl_value(record) or 0.0, 2),
        "exit_date": record.get("exit_date"),
        "exit_reason": record.get("exit_reason"),
    }


def comparison(records: list[dict], field: str, buckets: tuple[str, ...]) -> dict:
    grouped = {bucket: [] for bucket in buckets}
    for record in records:
        if not analytics_eligible_record(record):
            continue
        key = str(record.get(field) or "UNKNOWN")
        if field == "grade" and key == "A_PLUS":
            key = "A+"
        if key in grouped:
            grouped[key].append(record)
    return {bucket: analytics_summary(rows) for bucket, rows in grouped.items()}


def analytics_summary(records: list[dict]) -> dict:
    eligible = [row for row in records if analytics_eligible_record(row)]
    profits = [analytics_profit_percent_value(row) or 0.0 for row in eligible]
    winners = [value for value in profits if value > 0]
    losers = [value for value in profits if value < 0]
    rr_values = [number_or_none(row.get("RR")) for row in eligible]
    rr_values = [value for value in rr_values if value is not None]
    pnl_values = [analytics_pnl_value(row) or 0.0 for row in eligible]
    winning_pnl = [value for value in pnl_values if value > 0]
    losing_pnl = [value for value in pnl_values if value < 0]
    total_profit = sum(winning_pnl)
    total_loss = abs(sum(losing_pnl))
    return {
        "total_trades": len(eligible),
        "ambiguous_count": len(records) - len(eligible),
        "win_rate": round((len(winners) / len(eligible) * 100) if eligible else 0.0, 2),
        "average_profit": round(mean(winners), 2) if winners else 0.0,
        "average_loss": round(mean(losers), 2) if losers else 0.0,
        "profit_factor": round(total_profit / total_loss, 2) if total_loss else (round(total_profit, 2) if total_profit else 0.0),
        "average_rr": round(mean(rr_values), 2) if rr_values else 0.0,
        "stopped_treatment": STOPPED_ANALYTICS_TREATMENT,
    }


def period_pnl(records: list[dict], period: str) -> dict:
    values = defaultdict(float)
    for record in records:
        if not analytics_eligible_record(record):
            continue
        exit_dt = parse_datetime(record.get("exit_date"))
        if exit_dt is None:
            continue
        key = exit_dt.strftime("%Y-%m" if period == "monthly" else "%Y")
        values[key] += analytics_pnl_value(record) or 0.0
    return {key: round(values[key], 2) for key in sorted(values)}


def build_trade_analytics(records: list[dict]) -> dict:
    eligible = [row for row in records if analytics_eligible_record(row)]
    ordered = sorted(eligible, key=lambda row: analytics_pnl_value(row) or 0.0)
    worst = ordered[0] if ordered else None
    best = ordered[-1] if ordered else None
    summary = analytics_summary(records)
    monthly_pnl = period_pnl(records, "monthly")
    yearly_pnl = period_pnl(records, "yearly")
    latest_monthly = list(monthly_pnl.values())[-1] if monthly_pnl else 0.0
    return {
        **summary,
        "best_trade": compact_trade(best),
        "worst_trade": compact_trade(worst),
        "monthly_pnl": monthly_pnl,
        "yearly_pnl": yearly_pnl,
        "strategy_comparison": comparison(records, "strategy_type", STRATEGY_BUCKETS),
        "grade_comparison": comparison(records, "grade", GRADE_BUCKETS),
        "trap_comparison": comparison(records, "trap_status", TRAP_BUCKETS),
        "dashboard_cards": {
            "Total Trades": summary["total_trades"],
            "Win Rate": summary["win_rate"],
            "Profit Factor": summary["profit_factor"],
            "Average RR": summary["average_rr"],
            "Best Trade": compact_trade(best),
            "Worst Trade": compact_trade(worst),
            "Monthly Return": round(latest_monthly, 2),
        },
    }


async def get_trade_analytics(db, limit: int = 1000, *, sync_missing: bool = True) -> dict:
    sync_result = await sync_completed_trades_to_journal(db, limit) if sync_missing else None
    records = await load_trade_journal(db, limit)
    return {
        "journal_count": len(records),
        "sync_result": sync_result,
        "analytics": build_trade_analytics(records),
        "journal": records,
    }
