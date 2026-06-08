import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Mapping


CLOSED_TRADE_STATUSES = {
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

WIN_STATUSES = {
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

LOSS_STATUSES = {"STOP_HIT", "STOPPED", "STOPPED_AFTER_T1", "SL_HIT", "LOST_SL"}

OUTCOME_FIELDS = (
    "outcome_status",
    "outcome_label",
    "outcome_pnl",
    "outcome_pnl_percent",
    "outcome_exit_price",
    "outcome_exit_reason",
    "outcome_closed_at",
)

ATTACHED_OUTCOME_FIELDS = (
    "outcome_status",
    "final_status",
    "paper_pnl",
    "paper_pnl_percent",
    "realized_pnl",
    "exit_price",
    "exit_time",
    "holding_time",
    "result_label",
    "outcome_attached_at",
)

TREND_BREAKDOWN_KEYS = {
    "swing": ("price_strength", "near_day_high", "thirty_day_momentum", "above_open", "above_previous_close"),
    "momentum": ("price_strength", "near_high", "thirty_day_momentum", "clean_price_behavior"),
}

VOLUME_BREAKDOWN_KEYS = {
    "swing": ("traded_value", "relative_volume"),
    "momentum": ("liquidity",),
}


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _doc(document: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return document or {}


def _first_value(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _number(value: Any) -> float | int | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return int(number) if number.is_integer() else number


def _first_number(*values: Any) -> float | int | None:
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def _split_symbol(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    text = str(value).strip()
    if not text:
        return None, None
    if ":" in text:
        exchange, symbol = text.split(":", 1)
        return exchange.strip().upper() or None, symbol.strip().upper() or None
    return None, text.upper()


def _document_id(document: Mapping[str, Any]) -> str | None:
    value = _first_value(document.get("_id"), document.get("id"), document.get("run_id"))
    return str(value) if value is not None else None


def _source_ids(
    scored_candidate: Mapping[str, Any],
    market_data: Mapping[str, Any],
    tv_confirmation: Mapping[str, Any],
    paper_signal: Mapping[str, Any],
    paper_trade: Mapping[str, Any],
) -> dict[str, str]:
    sources = {
        "scored_candidate_id": _document_id(scored_candidate),
        "market_data_id": _document_id(market_data),
        "tv_confirmation_id": _document_id(tv_confirmation),
        "paper_signal_id": _document_id(paper_signal),
        "paper_trade_id": _document_id(paper_trade),
        "scan_run_id": _first_value(scored_candidate.get("scan_run_id"), paper_signal.get("scan_run_id")),
    }
    return {key: str(value) for key, value in sources.items() if value is not None}


def _strategy_type(*documents: Mapping[str, Any]) -> str | None:
    raw = _first_value(
        *(
            _first_value(
                document.get("strategy_type"),
                document.get("strategy"),
                document.get("signal_type"),
                document.get("source_signal_type"),
            )
            for document in documents
        )
    )
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if "momentum" in text:
        return "momentum"
    if "swing" in text:
        return "swing"
    return text or None


def _score_breakdown(scored_candidate: Mapping[str, Any], strategy_type: str | None) -> Mapping[str, Any]:
    breakdown = scored_candidate.get("score_breakdown")
    if not isinstance(breakdown, Mapping):
        return {}
    if strategy_type and isinstance(breakdown.get(strategy_type), Mapping):
        return breakdown[strategy_type]
    for key in ("swing", "momentum"):
        if isinstance(breakdown.get(key), Mapping):
            return breakdown[key]
    return breakdown


def _breakdown_sum(breakdown: Mapping[str, Any], keys: tuple[str, ...]) -> float | int | None:
    values = [_number(breakdown.get(key)) for key in keys]
    values = [value for value in values if value is not None]
    if not values:
        return None
    total = sum(values)
    return int(total) if float(total).is_integer() else total


def _status_values(document: Mapping[str, Any]) -> set[str]:
    return {
        str(value).upper()
        for value in (document.get("status"), document.get("outcome_status"))
        if value is not None and value != ""
    }


def is_closed_paper_trade(paper_trade: Mapping[str, Any] | None) -> bool:
    return bool(_status_values(_doc(paper_trade)) & CLOSED_TRADE_STATUSES)


def _setup_status(
    strategy_type: str | None,
    scored_candidate: Mapping[str, Any],
    tv_confirmation: Mapping[str, Any],
    paper_signal: Mapping[str, Any],
    paper_trade: Mapping[str, Any],
) -> Any:
    trade_status = None if is_closed_paper_trade(paper_trade) else paper_trade.get("status")
    strategy_status = scored_candidate.get(f"{strategy_type}_status") if strategy_type else None
    return _first_value(
        trade_status,
        paper_signal.get("status"),
        tv_confirmation.get("tv_status"),
        tv_confirmation.get("status"),
        strategy_status,
        scored_candidate.get("swing_status"),
        scored_candidate.get("momentum_status"),
    )


def initial_snapshot_has_no_outcome(snapshot: Mapping[str, Any]) -> bool:
    return all(snapshot.get(field) is None for field in set(OUTCOME_FIELDS) | set(ATTACHED_OUTCOME_FIELDS))


def snapshot_has_no_attached_outcome(snapshot: Mapping[str, Any]) -> bool:
    return all(snapshot.get(field) in (None, "") for field in set(OUTCOME_FIELDS) | set(ATTACHED_OUTCOME_FIELDS))


def initial_snapshot_has_no_leakage(snapshot: Mapping[str, Any]) -> bool:
    leakage_tokens = ("outcome", "pnl", "exit", "closed")
    setup_status = str(snapshot.get("setup_status") or "").upper()
    return (
        setup_status not in CLOSED_TRADE_STATUSES
        and initial_snapshot_has_no_outcome(snapshot)
        and all(
            value is None
            for key, value in snapshot.items()
            if any(token in str(key).lower() for token in leakage_tokens)
        )
    )


def ai_feature_snapshot_identity(snapshot: Mapping[str, Any]) -> str:
    identity = {
        "symbol": snapshot.get("symbol"),
        "strategy_type": snapshot.get("strategy_type"),
        "timeframe": snapshot.get("timeframe"),
        "data_source_ids": snapshot.get("data_source_ids") or {},
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def build_ai_feature_snapshot(
    scored_candidate: Mapping[str, Any],
    market_data: Mapping[str, Any] | None = None,
    tv_confirmation: Mapping[str, Any] | None = None,
    paper_signal: Mapping[str, Any] | None = None,
    paper_trade: Mapping[str, Any] | None = None,
    *,
    snapshot_time: str | None = None,
    timeframe: str | None = None,
) -> dict[str, Any]:
    market_data = _doc(market_data)
    tv_confirmation = _doc(tv_confirmation)
    paper_signal = _doc(paper_signal)
    paper_trade = _doc(paper_trade)
    strategy_type = _strategy_type(paper_trade, paper_signal, tv_confirmation, scored_candidate)
    breakdown = _score_breakdown(scored_candidate, strategy_type)
    exchange_from_symbol, symbol_from_tv = _split_symbol(
        _first_value(
            scored_candidate.get("tradingview_symbol"),
            market_data.get("tradingview_symbol"),
            paper_signal.get("symbol"),
            paper_trade.get("tradingview_symbol"),
        )
    )

    trend_score = _first_number(
        scored_candidate.get("trend_score"),
        tv_confirmation.get("trend_score"),
        paper_signal.get("trend_score"),
        _breakdown_sum(breakdown, TREND_BREAKDOWN_KEYS.get(strategy_type or "", ())),
    )
    volume_score = _first_number(
        scored_candidate.get("volume_score"),
        tv_confirmation.get("volume_score"),
        paper_signal.get("volume_score"),
        _breakdown_sum(breakdown, VOLUME_BREAKDOWN_KEYS.get(strategy_type or "", ())),
    )

    snapshot = {
        "feature_snapshot_version": 1,
        "paper_only": True,
        "symbol": _first_value(
            scored_candidate.get("canonical_symbol"),
            market_data.get("canonical_symbol"),
            scored_candidate.get("symbol"),
            market_data.get("symbol"),
            symbol_from_tv,
        ),
        "exchange": _first_value(scored_candidate.get("exchange"), market_data.get("exchange"), exchange_from_symbol),
        "strategy_type": strategy_type,
        "timeframe": _first_value(
            timeframe,
            paper_trade.get("timeframe"),
            paper_signal.get("timeframe"),
            tv_confirmation.get("timeframe"),
            scored_candidate.get("timeframe"),
            market_data.get("timeframe"),
        ),
        "snapshot_time": snapshot_time or utc_now_iso(),
        "data_source_ids": _source_ids(scored_candidate, market_data, tv_confirmation, paper_signal, paper_trade),
        "rule_score": _first_number(
            scored_candidate.get("rule_score"),
            scored_candidate.get("score"),
            scored_candidate.get("nse_score"),
            paper_signal.get("score"),
        ),
        "trend_score": trend_score,
        "momentum_score": _first_number(
            scored_candidate.get("momentum_score"),
            paper_signal.get("momentum_score"),
            tv_confirmation.get("momentum_score"),
        ),
        "volume_score": volume_score,
        "risk_score": _first_number(
            scored_candidate.get("risk_score"),
            tv_confirmation.get("risk_score"),
            paper_signal.get("risk_score"),
            paper_trade.get("risk_score"),
        ),
        "setup_status": _setup_status(strategy_type, scored_candidate, tv_confirmation, paper_signal, paper_trade),
        "entry_price": _first_number(
            paper_trade.get("entry_price"),
            paper_signal.get("entry"),
            paper_signal.get("paper_entry_price"),
            tv_confirmation.get("entry_price"),
            tv_confirmation.get("paper_entry_price"),
        ),
        "stop_loss": _first_number(
            paper_trade.get("stop_loss"),
            paper_signal.get("sl"),
            paper_signal.get("paper_stop_loss"),
            tv_confirmation.get("stop_loss"),
            tv_confirmation.get("paper_stop_loss"),
        ),
        "target": _first_number(
            paper_trade.get("target_1"),
            paper_signal.get("t1"),
            paper_signal.get("paper_target_1"),
            tv_confirmation.get("target_1"),
            tv_confirmation.get("paper_target_1"),
        ),
        "risk_reward": _first_number(
            paper_trade.get("risk_reward_1"),
            paper_signal.get("rr"),
            paper_signal.get("paper_rr_1"),
            tv_confirmation.get("risk_reward_1"),
            tv_confirmation.get("paper_rr_1"),
        ),
        "paper_trade_id": _document_id(paper_trade),
    }
    snapshot.update({field: None for field in OUTCOME_FIELDS})
    return snapshot


def _outcome_label(statuses: set[str], pnl: float | int | None) -> str | None:
    if statuses & WIN_STATUSES:
        return "WIN"
    if statuses & LOSS_STATUSES:
        return "LOSS"
    if "AMBIGUOUS" in statuses:
        return "UNKNOWN"
    if pnl is not None:
        if pnl > 0:
            return "WIN"
        if pnl < 0:
            return "LOSS"
        return "BREAKEVEN"
    return "UNKNOWN"


def build_closed_paper_trade_outcome(
    paper_trade: Mapping[str, Any],
    *,
    outcome_time: str | None = None,
) -> dict[str, Any]:
    if not is_closed_paper_trade(paper_trade):
        raise ValueError("Paper outcome can only be attached after the paper trade closes.")
    statuses = _status_values(paper_trade)
    terminal_status = next(
        (
            str(value)
            for value in (paper_trade.get("outcome_status"), paper_trade.get("status"))
            if value is not None and str(value).upper() in CLOSED_TRADE_STATUSES
        ),
        None,
    )
    paper_pnl = _number(paper_trade.get("paper_pnl"))
    paper_pnl_percent = _number(paper_trade.get("paper_pnl_percent"))
    realized_pnl = _number(paper_trade.get("realized_pnl"))
    exit_price = _number(paper_trade.get("exit_price"))
    exit_time = _first_value(
        paper_trade.get("exit_time"),
        paper_trade.get("closed_at"),
        paper_trade.get("status_updated_at"),
        paper_trade.get("updated_at"),
    )
    result_label = _outcome_label(statuses, _first_number(paper_pnl, realized_pnl))
    return {
        "outcome_status": terminal_status,
        "final_status": terminal_status,
        "paper_pnl": paper_pnl,
        "paper_pnl_percent": paper_pnl_percent,
        "realized_pnl": realized_pnl,
        "exit_price": exit_price,
        "exit_time": exit_time,
        "holding_time": _first_value(paper_trade.get("holding_time"), paper_trade.get("holding_duration")),
        "result_label": result_label,
        "outcome_attached_at": outcome_time or utc_now_iso(),
        "outcome_label": result_label,
        "outcome_pnl": paper_pnl,
        "outcome_pnl_percent": paper_pnl_percent,
        "outcome_exit_price": exit_price,
        "outcome_exit_reason": paper_trade.get("exit_reason"),
        "outcome_closed_at": exit_time,
    }


def attach_closed_paper_trade_outcome(
    snapshot: Mapping[str, Any],
    paper_trade: Mapping[str, Any],
    *,
    outcome_time: str | None = None,
) -> dict[str, Any]:
    updated = dict(snapshot)
    updated.update(build_closed_paper_trade_outcome(paper_trade, outcome_time=outcome_time))
    return updated
