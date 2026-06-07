from fastapi import APIRouter
from math import floor

from database import get_database


router = APIRouter()
VIRTUAL_BALANCE = 50000.0
TRADE_CAPITAL = 5000.0
LEVERAGE = 2.5
WAITING_STATUSES = {"PLANNED", "NOT_TRIGGERED"}
OPEN_STATUSES = {"ACTIVE", "TARGET_1_HIT"}
TERMINAL_STATUSES = {
    "CLOSED",
    "EXPIRED",
    "TARGET_HIT",
    "TARGET_1_HIT_FINAL",
    "TARGET_2_HIT",
    "TARGET_3_HIT",
    "T1_HIT",
    "T2_HIT",
    "T3_HIT",
    "WON_T1",
    "WON_T2",
    "WON_T3",
    "STOP_HIT",
    "STOPPED",
    "STOPPED_AFTER_T1",
    "SL_HIT",
    "LOST_SL",
    "AMBIGUOUS",
}
TARGET_STATUSES = {
    "TARGET_HIT",
    "TARGET_1_HIT_FINAL",
    "TARGET_2_HIT",
    "TARGET_3_HIT",
    "T1_HIT",
    "T2_HIT",
    "T3_HIT",
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


def _number(value) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _statuses(trade: dict) -> set[str]:
    return {str(trade.get(field) or "").upper() for field in ("status", "outcome_status")}


def _is_waiting_trade(trade: dict) -> bool:
    statuses = _statuses(trade)
    if statuses & (OPEN_STATUSES | TERMINAL_STATUSES):
        return False
    return bool(statuses & WAITING_STATUSES) or trade.get("entry_triggered") is False


def _is_open_trade(trade: dict) -> bool:
    statuses = _statuses(trade)
    return bool(statuses & OPEN_STATUSES) and not bool(statuses & TERMINAL_STATUSES)


def _realized_rr(trade: dict) -> float:
    statuses = _statuses(trade)
    for status in ("T3_HIT", "WON_T3", "T2_HIT", "WON_T2", "T1_HIT", "WON_T1", "SL_HIT", "LOST_SL", "AMBIGUOUS"):
        if status in statuses:
            return RR_BY_STATUS[status]
    return 0


def _outcome_type(trade: dict) -> str | None:
    statuses = _statuses(trade)
    if statuses & TARGET_STATUSES:
        return "WIN"
    if statuses & LOSS_STATUSES:
        return "LOSS"
    return None


def _paper_quantity(trade: dict) -> int:
    entry_price = _number(trade.get("entry_price"))
    return floor((TRADE_CAPITAL * LEVERAGE) / entry_price) if entry_price > 0 else 0


def _leveraged_pnl(trade: dict) -> float:
    raw_pnl = _number(trade.get("paper_pnl"))
    per_share_statuses = {"NOT_TRIGGERED", "ACTIVE", "T1_HIT", "T2_HIT", "T3_HIT", "SL_HIT", "AMBIGUOUS"}
    return raw_pnl * _paper_quantity(trade) if _statuses(trade) & per_share_statuses else raw_pnl


def _counts_for_pnl(trade: dict) -> bool:
    return bool(_is_open_trade(trade) or _outcome_type(trade) or (_statuses(trade) & TERMINAL_STATUSES))


def _reportable_pnl(trade: dict) -> float:
    return _leveraged_pnl(trade) if _counts_for_pnl(trade) else 0.0


@router.get("/paper-equity")
async def get_paper_equity() -> dict:
    cursor = get_database().paper_trades.find({"paper_only": True}, {"_id": 0})
    trades = [trade async for trade in cursor]
    ordered = sorted(trades, key=lambda trade: str(trade.get("updated_at") or trade.get("created_at") or ""))
    latest = list(reversed(ordered))

    open_trades = sum(1 for trade in trades if _is_open_trade(trade))
    cumulative_pnl = sum(_reportable_pnl(trade) for trade in trades)
    cumulative_rr = sum(_realized_rr(trade) for trade in trades)
    open_margin_used = TRADE_CAPITAL * open_trades
    effective_exposure = open_margin_used * LEVERAGE
    broker_funded = effective_exposure - open_margin_used
    max_buying_power = VIRTUAL_BALANCE * LEVERAGE
    available_margin = VIRTUAL_BALANCE - open_margin_used
    available_buying_power = max_buying_power - effective_exposure

    equity = VIRTUAL_BALANCE
    peak = VIRTUAL_BALANCE
    equity_curve = []
    drawdown_curve = []
    for index, trade in enumerate(ordered, start=1):
        equity += _reportable_pnl(trade)
        peak = max(peak, equity)
        drawdown = ((peak - equity) / peak * 100) if peak else 0
        point = {
            "index": index,
            "date": trade.get("updated_at") or trade.get("created_at"),
            "symbol": trade.get("symbol"),
            "value": round(equity, 2),
        }
        equity_curve.append(point)
        drawdown_curve.append({**point, "value": round(drawdown, 2)})

    completed_latest = [trade for trade in latest if _outcome_type(trade)]
    latest_outcome = _outcome_type(completed_latest[0]) if completed_latest else None
    streak = 0
    for trade in completed_latest:
        if _outcome_type(trade) != latest_outcome:
            break
        streak += 1

    target_hit_count = sum(1 for trade in trades if _statuses(trade) & TARGET_STATUSES)
    sl_hit_count = sum(1 for trade in trades if _statuses(trade) & LOSS_STATUSES)
    ambiguous_count = sum(1 for trade in trades if "AMBIGUOUS" in _statuses(trade))
    waiting_count = sum(1 for trade in trades if _is_waiting_trade(trade))
    active_count = sum(1 for trade in trades if _is_open_trade(trade))
    completed_trades = target_hit_count + sl_hit_count + ambiguous_count
    win_rate_percent = (target_hit_count / completed_trades * 100) if completed_trades else 0
    current_drawdown = drawdown_curve[-1]["value"] if drawdown_curve else 0

    latest_fields = (
        "symbol", "source_signal_type", "trade_quality_grade", "status", "outcome_status",
        "entry_price", "stop_loss", "target_1", "target_2", "target_3", "paper_pnl", "updated_at", "created_at",
    )
    return {
        "virtual_balance": VIRTUAL_BALANCE,
        "trade_capital": TRADE_CAPITAL,
        "leverage": LEVERAGE,
        "total_trades": len(trades),
        "open_trades": open_trades,
        "open_margin_used": open_margin_used,
        "effective_exposure": effective_exposure,
        "broker_funded": broker_funded,
        "max_buying_power": max_buying_power,
        "available_margin": available_margin,
        "available_buying_power": available_buying_power,
        "buying_power_usage_percent": round(effective_exposure / max_buying_power * 100, 2),
        "cumulative_pnl": round(cumulative_pnl, 2),
        "cumulative_rr": cumulative_rr,
        "virtual_return_percent": round(cumulative_pnl / VIRTUAL_BALANCE * 100, 2),
        "drawdown_percent": current_drawdown,
        "win_streak": streak if latest_outcome == "WIN" else 0,
        "loss_streak": streak if latest_outcome == "LOSS" else 0,
        "waiting_count": waiting_count,
        "active_count": active_count,
        "completed_trades": completed_trades,
        "win_rate_percent": round(win_rate_percent, 2),
        "target_hit_count": target_hit_count,
        "sl_hit_count": sl_hit_count,
        "ambiguous_count": ambiguous_count,
        "equity_curve": equity_curve,
        "drawdown_curve": drawdown_curve,
        "latest_trades": [
            {
                **{field: trade.get(field) for field in latest_fields},
                "planned_capital": TRADE_CAPITAL,
                "effective_exposure": TRADE_CAPITAL * LEVERAGE,
                "paper_quantity": _paper_quantity(trade),
                "leveraged_pnl": round(_reportable_pnl(trade), 2),
            }
            for trade in latest[:10]
        ],
    }
