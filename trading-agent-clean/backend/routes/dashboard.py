from fastapi import APIRouter
from math import floor

from database import get_database
from services.trade_journal import (
    analytics_pnl_value,
    analytics_profit_percent_value,
    analytics_realized_pnl_record,
    build_trade_analytics,
    is_ambiguous_trade,
    load_trade_journal,
    number_or_none,
)
from services.capital_accounting import is_genuine_open_trade, trade_open_margin_used, trade_open_sl_risk
from config import GENUINE_OPEN_STATUSES


router = APIRouter()
STARTING_VIRTUAL_BALANCE = 250000.0
MINIMUM_TRADE_CAPITAL = 5000.0
LEVERAGE = 2.5
WAITING_STATUSES = {"PLANNED", "NOT_TRIGGERED", "WAITING", "WAITING_FOR_ENTRY"}
T1_PARTIAL_STATUS = "T1_PARTIAL"
T2_PARTIAL_STATUS = "T2_PARTIAL"
PARTIAL_STATUSES = {s for s in GENUINE_OPEN_STATUSES if s != "ACTIVE"}
ACTIVE_STATUSES = GENUINE_OPEN_STATUSES
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
RR_BY_STATUS = {
    "T1_HIT": 2,
    "WON_T1": 2,
    "T2_HIT": 3,
    "WON_T2": 3,
    "T3_HIT": 4,
    "WON_T3": 4,
    "SL_HIT": -1,
    "LOST_SL": -1,
    "AMBIGUOUS": 0,
}


def _round(value, digits: int = 2) -> float:
    return round(float(value or 0.0), digits)


def _first_number(*values) -> float | None:
    for value in values:
        numeric = number_or_none(value)
        if numeric is not None:
            return numeric
    return None


def _statuses(trade: dict) -> set[str]:
    return {
        status
        for status in (
            str(trade.get("status") or "").upper(),
            str(trade.get("outcome_status") or "").upper(),
            str(trade.get("state") or "").upper(),
            str(trade.get("exit_reason") or "").upper(),
        )
        if status
    }


def _is_waiting_trade(trade: dict) -> bool:
    statuses = _statuses(trade)
    if statuses & (ACTIVE_STATUSES | TERMINAL_STATUSES):
        return False
    return bool(statuses & WAITING_STATUSES) or trade.get("entry_triggered") is False


def _is_partial_trade(trade: dict) -> bool:
    statuses = _statuses(trade)
    return bool(statuses & PARTIAL_STATUSES) and not bool(statuses & TERMINAL_STATUSES)


def _is_open_trade(trade: dict) -> bool:
    return is_genuine_open_trade(trade)


def _is_active_trade(trade: dict) -> bool:
    return _is_open_trade(trade) and not _is_partial_trade(trade)


def _has_sl_status(record: dict) -> bool:
    statuses = _statuses(record)
    return bool(record.get("SL_HIT")) or bool(statuses & LOSS_STATUSES)


def _is_completed_trade(trade: dict) -> bool:
    if is_ambiguous_trade(trade) or _has_sl_status(trade):
        return False
    statuses = _statuses(trade)
    return bool(statuses & TARGET_STATUSES)


def _is_sl_record(record: dict) -> bool:
    pnl = analytics_pnl_value(record)
    return _has_sl_status(record) or (pnl is not None and pnl < 0)


def _is_completed_record(record: dict) -> bool:
    if is_ambiguous_trade(record) or _is_sl_record(record):
        return False
    statuses = _statuses(record)
    pnl = analytics_pnl_value(record)
    return bool(statuses & TARGET_STATUSES) or (pnl is not None and pnl >= 0)


def _strategy_label(trade: dict) -> str:
    text = str(trade.get("source_signal_type") or trade.get("signal_type") or trade.get("strategy_type") or "").upper()
    if "MOMENTUM" in text:
        return "Momentum"
    if "SWING" in text:
        return "Swing"
    return text.replace("_", " ").title() if text else "Other"


def _trade_quantity(trade: dict) -> float:
    if _is_partial_trade(trade):
        partial_quantity = _first_number(trade.get("quantity_remaining"))
        if partial_quantity is not None:
            return max(partial_quantity, 0.0)
    quantity = _first_number(trade.get("quantity_remaining"), trade.get("quantity"))
    return max(quantity or 0.0, 0.0)


def _trade_margin(exposure: float) -> float:
    if exposure <= 0:
        return 0.0
    leveraged_margin = exposure / LEVERAGE if LEVERAGE else exposure
    return max(MINIMUM_TRADE_CAPITAL, leveraged_margin)


def _open_trade_exposure(trade: dict) -> float:
    entry = _first_number(trade.get("entry_price"), trade.get("entry"), trade.get("paper_entry_price"))
    quantity = _trade_quantity(trade)
    if entry is not None and quantity:
        return max(entry * quantity, 0.0)
    return 0.0


def _open_trade_unrealized_pnl(trade: dict) -> float:
    if not _is_open_trade(trade):
        return 0.0
    pnl = _first_number(
        trade.get("remaining_unrealized_pnl"),
        trade.get("unrealized_pnl"),
        trade.get("paper_pnl"),
        trade.get("total_trade_pnl"),
    )
    return pnl or 0.0


def _paper_quantity(trade: dict) -> int:
    entry_price = _first_number(trade.get("entry_price"), trade.get("entry"), trade.get("paper_entry_price"))
    exposure = _open_trade_exposure(trade) or (MINIMUM_TRADE_CAPITAL * LEVERAGE)
    return floor(exposure / entry_price) if entry_price and entry_price > 0 else 0


def _record_sort_value(record: dict) -> str:
    return str(record.get("exit_date") or record.get("journaled_at") or record.get("created_at") or "")


def _open_position_rows(open_trades: list[dict], current_balance: float) -> list[dict]:
    from routes.paper import realized_partial_pnl
    available_margin = current_balance
    rows = []
    for trade in sorted(open_trades, key=lambda row: str(row.get("entry_triggered_at") or row.get("created_at") or row.get("updated_at") or "")):
        exposure = _open_trade_exposure(trade)
        margin_used = trade_open_margin_used(trade)
        available_margin -= margin_used
        broker_funded = exposure - margin_used

        pnl = trade.get("realized_pnl")
        if pnl is None:
            pnl = realized_partial_pnl(trade)

        rows.append(
            {
                "symbol": trade.get("symbol"),
                "strategy": _strategy_label(trade),
                "status": "Partial" if _is_partial_trade(trade) else "Active",
                "entry_price": _first_number(trade.get("entry_price"), trade.get("entry"), trade.get("paper_entry_price")),
                "quantity": _first_number(trade.get("quantity")),
                "original_quantity": _first_number(trade.get("original_quantity"), trade.get("quantity")),
                "quantity_remaining": _trade_quantity(trade),
                "current_price": _first_number(trade.get("latest_close"), trade.get("current_price")),
                "effective_exposure": _round(exposure),
                "actual_trade_capital": _round(margin_used),
                "initial_margin_reserved": _round(trade.get("initial_margin_reserved") or margin_used),
                "margin_remaining": _round(trade.get("margin_remaining") if trade.get("margin_remaining") is not None else margin_used),
                "margin_released_total": _round(trade.get("margin_released_total") or 0.0),
                "margin_used": _round(margin_used),
                "broker_funded": _round(broker_funded),
                "available_margin_after_trade": _round(available_margin),
                "realized_pnl": _round(pnl or 0.0),
                "unrealized_pnl": _round(_open_trade_unrealized_pnl(trade)),
                "updated_at": trade.get("updated_at") or trade.get("last_checked_at"),
            }
        )
    return rows


def _equity_curve(records: list[dict]) -> tuple[list[dict], float]:
    equity = STARTING_VIRTUAL_BALANCE
    peak = STARTING_VIRTUAL_BALANCE
    max_drawdown = 0.0
    points = [
        {
            "index": 0,
            "date": None,
            "symbol": "Starting Balance",
            "value": _round(equity),
            "realized_pnl": 0.0,
            "drawdown_percent": 0.0,
        }
    ]
    for index, record in enumerate(sorted(records, key=_record_sort_value), start=1):
        pnl = analytics_pnl_value(record) or 0.0
        equity += pnl
        peak = max(peak, equity)
        drawdown = ((peak - equity) / peak * 100) if peak else 0.0
        max_drawdown = max(max_drawdown, drawdown)
        points.append(
            {
                "index": index,
                "date": record.get("exit_date") or record.get("journaled_at") or record.get("created_at"),
                "symbol": record.get("symbol"),
                "value": _round(equity),
                "realized_pnl": _round(pnl),
                "drawdown_percent": _round(drawdown),
            }
        )
    return points, _round(max_drawdown)


def _monthly_rows(monthly_pnl: dict) -> list[dict]:
    return [{"month": key, "pnl": value} for key, value in sorted((monthly_pnl or {}).items())]


def _strategy_rows(strategy_comparison: dict) -> list[dict]:
    rows = []
    for strategy in ("Swing", "Momentum"):
        summary = (strategy_comparison or {}).get(strategy, {})
        rows.append(
            {
                "strategy": strategy,
                "total_trades": summary.get("total_trades", 0),
                "win_rate": summary.get("win_rate", 0),
                "profit_factor": summary.get("profit_factor", 0),
                "average_rr": summary.get("average_rr", 0),
            }
        )
    return rows


def _recent_completed_rows(records: list[dict], limit: int = 10) -> list[dict]:
    latest = sorted(records, key=_record_sort_value, reverse=True)[:limit]
    return [
        {
            "symbol": record.get("symbol"),
            "strategy": record.get("strategy_type"),
            "exit_date": record.get("exit_date"),
            "exit_reason": record.get("exit_reason"),
            "realized_pnl": _round(analytics_pnl_value(record) or 0.0),
            "profit_percent": _round(analytics_profit_percent_value(record) or 0.0),
            "RR": _round(number_or_none(record.get("RR")) or 0.0),
        }
        for record in latest
    ]


@router.get("/paper-equity")
async def get_paper_equity() -> dict:
    from routes.paper import realized_partial_pnl
    db = get_database()
    cursor = db.paper_trades.find({"paper_only": True}, {"_id": 0})
    trades = [trade async for trade in cursor]
    journal_records = await load_trade_journal(db, 5000)
    analytics = build_trade_analytics(journal_records)
    realized_records = [record for record in journal_records if analytics_realized_pnl_record(record)]
    realized_pnl = sum(analytics_pnl_value(record) or 0.0 for record in realized_records)
    settled_balance = STARTING_VIRTUAL_BALANCE + realized_pnl

    open_trades = [trade for trade in trades if is_genuine_open_trade(trade)]
    active_count = sum(1 for trade in trades if _is_active_trade(trade))
    partial_count = sum(1 for trade in trades if _is_partial_trade(trade))
    waiting_count = sum(1 for trade in trades if _is_waiting_trade(trade))
    completed_count = sum(1 for trade in trades if _is_completed_trade(trade))
    sl_hit_count = sum(1 for trade in trades if _has_sl_status(trade))
    ambiguous_count = sum(1 for trade in trades if is_ambiguous_trade(trade))
    effective_exposure = sum(_open_trade_exposure(trade) for trade in open_trades)

    # Reserved Margin: Sum of margin_remaining for genuinely open positions
    reserved_margin = sum(trade_open_margin_used(trade) for trade in open_trades)
    combined_open_risk = sum(trade_open_sl_risk(trade) for trade in open_trades)
    available_cash = settled_balance - reserved_margin
    max_buying_power = settled_balance * LEVERAGE
    available_buying_power = available_cash * LEVERAGE
    broker_funded = effective_exposure - reserved_margin
    usage_percent = (reserved_margin / settled_balance * 100) if settled_balance else 0.0
    unrealized_pnl = sum(_open_trade_unrealized_pnl(trade) for trade in open_trades)
    total_equity = settled_balance + unrealized_pnl
    total_pnl = realized_pnl + unrealized_pnl

    # Total Margin Released: Sum of margin_released_total across all database trades.
    total_margin_released = sum(float(trade.get("margin_released_total") or 0.0) for trade in trades)

    # Fetch recent completed trades from paper_trades collection instead of trade journal
    completed_cursor = db.paper_trades.find(
        {"paper_only": True, "status": {"$in": list(TERMINAL_STATUSES)}},
    ).sort("updated_at", -1).limit(10)
    completed_trades = [t async for t in completed_cursor]

    recent_completed_rows = []
    for trade in completed_trades:
        pnl = trade.get("realized_pnl")
        if pnl is None:
            pnl = trade.get("paper_pnl") or realized_partial_pnl(trade)

        cash_returned = trade.get("cash_returned_on_final_exit")
        if cash_returned is None:
            init_margin = float(trade.get("initial_margin_reserved") or 0.0)
            cash_returned = init_margin + float(pnl or 0.0)

        recent_completed_rows.append({
            "symbol": trade.get("symbol"),
            "strategy": _strategy_label(trade),
            "exit_date": trade.get("exit_date") or trade.get("updated_at") or trade.get("status_updated_at"),
            "exit_reason": trade.get("exit_reason") or trade.get("status") or trade.get("outcome_status"),
            "initial_margin_reserved": _round(trade.get("initial_margin_reserved") or 0.0),
            "margin_released_total": _round(trade.get("margin_released_total") or 0.0),
            "realized_pnl": _round(pnl or 0.0),
            "cash_returned_on_final_exit": _round(cash_returned),
        })

    capital_returned_from_latest_exits = sum(r["cash_returned_on_final_exit"] for r in recent_completed_rows)

    equity_curve, drawdown_percent = _equity_curve(realized_records)
    open_positions = _open_position_rows(open_trades, settled_balance)

    latest_fields = (
        "symbol",
        "source_signal_type",
        "trade_quality_grade",
        "status",
        "outcome_status",
        "entry_price",
        "stop_loss",
        "target_1",
        "target_2",
        "target_3",
        "paper_pnl",
        "updated_at",
        "created_at",
    )
    latest = list(reversed(sorted(trades, key=lambda trade: str(trade.get("updated_at") or trade.get("created_at") or ""))))
    return {
        "starting_virtual_capital": _round(STARTING_VIRTUAL_BALANCE),
        "starting_virtual_balance": _round(STARTING_VIRTUAL_BALANCE),
        "settled_balance": _round(settled_balance),
        "current_virtual_balance": _round(settled_balance), # backward compatibility
        "reserved_margin": _round(reserved_margin),
        "open_margin_used": _round(reserved_margin), # backward compatibility
        "available_cash": _round(available_cash),
        "available_margin": _round(available_cash), # backward compatibility
        "unrealized_pnl": _round(unrealized_pnl),
        "total_equity": _round(total_equity),
        "total_realized_pnl": _round(realized_pnl),
        "realized_pnl": _round(realized_pnl), # backward compatibility
        "effective_open_exposure": _round(effective_exposure),
        "effective_exposure": _round(effective_exposure), # backward compatibility
        "broker_funded_exposure": _round(broker_funded),
        "broker_funded": _round(broker_funded), # backward compatibility
        "active_partial_trade_count": f"{active_count} / {partial_count}",
        "active_partial_count": f"{active_count} / {partial_count}", # backward compatibility
        "total_margin_released": _round(total_margin_released),
        "capital_returned_from_latest_exits": _round(capital_returned_from_latest_exits),

        "minimum_trade_capital": _round(MINIMUM_TRADE_CAPITAL),
        "leverage": LEVERAGE,
        "combined_open_risk": _round(combined_open_risk),
        "max_buying_power": _round(max_buying_power),
        "available_buying_power": _round(available_buying_power),
        "buying_power_usage_percent": _round(usage_percent),
        "total_pnl": _round(total_pnl),
        "virtual_return_percent": _round((total_pnl / STARTING_VIRTUAL_BALANCE * 100) if STARTING_VIRTUAL_BALANCE else 0.0),
        "drawdown_percent": drawdown_percent,
        "win_rate_percent": analytics["win_rate"],
        "profit_factor": analytics["profit_factor"],
        "average_rr": analytics["average_rr"],
        "status_counts": {
            "waiting": waiting_count,
            "active": active_count,
            "partial": partial_count,
            "completed": completed_count,
            "sl_hit": sl_hit_count,
            "ambiguous": ambiguous_count,
        },
        "open_position_exposure": open_positions,
        "recent_completed_trades": recent_completed_rows,
        "equity_curve": equity_curve,
        "monthly_pnl": analytics["monthly_pnl"],
        "monthly_pnl_rows": _monthly_rows(analytics["monthly_pnl"]),
        "strategy_comparison": analytics["strategy_comparison"],
        "strategy_comparison_rows": _strategy_rows(analytics["strategy_comparison"]),
        "journal_count": len(journal_records),
        "open_trades": len(open_trades),
        "paper_trade_count": len(trades),
        "formulas": {
            "starting_virtual_capital": "starting account capital",
            "settled_balance": "starting_virtual_capital + realized_pnl",
            "reserved_margin": "sum(margin_remaining for genuinely open positions)",
            "available_cash": "settled_balance - reserved_margin",
            "total_equity": "settled_balance + unrealized_pnl",
            "total_pnl": "realized_pnl + unrealized_pnl",
            "broker_funded_exposure": "open_exposure - reserved_margin",
        },
        "virtual_balance": _round(settled_balance),
        "trade_capital": _round(MINIMUM_TRADE_CAPITAL),
        "cumulative_pnl": _round(total_pnl),
        "cumulative_rr": analytics["average_rr"],
        "completed_trades": completed_count,
        "target_hit_count": completed_count,
        "sl_hit_count": sl_hit_count,
        "ambiguous_count": ambiguous_count,
        "latest_trades": [
            {
                **{field: trade.get(field) for field in latest_fields},
                "planned_capital": _round(MINIMUM_TRADE_CAPITAL),
                "effective_exposure": _round(_open_trade_exposure(trade)),
                "paper_quantity": _paper_quantity(trade),
                "leveraged_pnl": _round(_open_trade_unrealized_pnl(trade)),
            }
            for trade in latest[:10]
        ],
    }


@router.get("/trade-analytics")
async def get_dashboard_trade_analytics() -> dict:
    records = await load_trade_journal(get_database(), 5000)
    analytics = build_trade_analytics(records)
    return {
        "cards": analytics["dashboard_cards"],
        "analytics": analytics,
        "journal_count": len(records),
        "completed_trades_immutable": True,
    }
