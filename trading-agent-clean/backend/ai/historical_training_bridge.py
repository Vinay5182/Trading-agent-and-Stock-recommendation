from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ai.feature_contract import (
    APPROVED_MODEL_FEATURES,
    BLOCKED_ACCOUNT_FIELDS,
    BLOCKED_AUDIT_FIELDS,
    BLOCKED_IDENTIFIERS,
    BLOCKED_LABEL_FIELDS,
    BLOCKED_LIFECYCLE_FIELDS,
)
from ai.label_contract import (
    LABEL_STATE_EXCLUDED,
    LABEL_STATE_LABELED,
    LABEL_STATE_UNLABELED,
    OUTCOME_CLASS_AMBIGUOUS,
    OUTCOME_CLASS_INCOMPLETE,
    OUTCOME_CLASS_LOSS,
    OUTCOME_CLASS_NO_ENTRY,
    OUTCOME_CLASS_WIN,
)
from ai.training_schema import build_canonical_training_row
from services.historical_ohlcv_store import HISTORICAL_OHLCV_COLLECTION


HISTORICAL_TRAINING_BRIDGE_VERSION = "phase5b7d-v1"
HISTORICAL_TRAINING_CALCULATION_VERSION = "historical-training-preview-v1"
HISTORICAL_TRAINING_LABEL_VERSION = "historical-outcome-simulation-v1"
DEFAULT_LOOKBACK = 30
DEFAULT_HORIZON = 20
DEFAULT_STRATEGIES = ("momentum", "swing")
SUPPORTED_STRATEGIES = frozenset(DEFAULT_STRATEGIES)
FEATURE_COLUMNS = tuple(APPROVED_MODEL_FEATURES)
BLOCKED_MODEL_FEATURES = frozenset(
    BLOCKED_IDENTIFIERS
    | BLOCKED_LABEL_FIELDS
    | BLOCKED_LIFECYCLE_FIELDS
    | BLOCKED_ACCOUNT_FIELDS
    | BLOCKED_AUDIT_FIELDS
)


@dataclass(frozen=True)
class HistoricalCandle:
    symbol: str
    exchange: str
    timeframe: str
    provider: str
    candle_id: str
    candle_open_at: str
    candle_close_at: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool


@dataclass(frozen=True)
class SimulatedOutcome:
    outcome_class: str
    label_state: str
    status: str
    terminal_timestamp: str | None
    entry_timestamp: str | None
    exit_price: float | None
    entry_triggered: bool
    ambiguous: bool = False


def parse_strategies(value: str | Sequence[str] | None) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_STRATEGIES
    raw_values: list[str] = []
    if isinstance(value, str):
        raw_values = [part.strip() for part in value.split(",")]
    else:
        for item in value:
            raw_values.extend(part.strip() for part in str(item).split(","))
    strategies = tuple(dict.fromkeys(item.lower() for item in raw_values if item))
    invalid = sorted(set(strategies) - SUPPORTED_STRATEGIES)
    if invalid:
        raise ValueError(f"Unsupported historical training strategy: {', '.join(invalid)}")
    return strategies or DEFAULT_STRATEGIES


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def _score(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 4)


def _candle_from_doc(row: Mapping[str, Any]) -> HistoricalCandle | None:
    if row.get("is_closed") is not True:
        return None
    symbol = str(row.get("canonical_symbol") or row.get("symbol") or "").strip().upper()
    open_at = str(row.get("candle_open_at") or "").strip()
    close_at = str(row.get("candle_close_at") or "").strip()
    candle_id = str(row.get("candle_id") or "").strip()
    if not (symbol and open_at and close_at and candle_id):
        return None
    return HistoricalCandle(
        symbol=symbol,
        exchange=str(row.get("exchange") or "NSE").strip().upper(),
        timeframe=str(row.get("timeframe") or "1d").strip(),
        provider=str(row.get("provider") or "unknown").strip().lower(),
        candle_id=candle_id,
        candle_open_at=open_at,
        candle_close_at=close_at,
        open=_number(row.get("open")),
        high=_number(row.get("high")),
        low=_number(row.get("low")),
        close=_number(row.get("close")),
        volume=_number(row.get("volume")),
        is_closed=True,
    )


def _ema(values: Sequence[float], period: int) -> float | None:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    if not clean:
        return None
    multiplier = 2 / (period + 1)
    ema = clean[0]
    for value in clean[1:]:
        ema = (value - ema) * multiplier + ema
    return ema


def _rsi(closes: Sequence[float], period: int = 14) -> float | None:
    if len(closes) < 2:
        return None
    recent = list(closes)[-(period + 1) :]
    gains = 0.0
    losses = 0.0
    for previous, current in zip(recent, recent[1:]):
        delta = current - previous
        if delta >= 0:
            gains += delta
        else:
            losses += abs(delta)
    if gains == 0 and losses == 0:
        return 50.0
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - (100 / (1 + rs))


def _atr(candles: Sequence[HistoricalCandle], period: int = 14) -> float | None:
    if len(candles) < 2:
        return None
    recent = list(candles)[-(period + 1) :]
    ranges: list[float] = []
    previous_close = recent[0].close
    for candle in recent[1:]:
        ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )
        previous_close = candle.close
    return sum(ranges) / len(ranges) if ranges else None


def _average(values: Iterable[float]) -> float | None:
    clean = [value for value in values if math.isfinite(value)]
    return sum(clean) / len(clean) if clean else None


def _historical_setup_id(strategy: str, candle: HistoricalCandle) -> str:
    payload = "|".join(
        (
            HISTORICAL_TRAINING_BRIDGE_VERSION,
            strategy,
            candle.exchange,
            candle.symbol,
            candle.timeframe.upper(),
            candle.candle_id,
            candle.candle_close_at,
        )
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"hist-{digest}"


def _features_for_window(window: Sequence[HistoricalCandle]) -> dict[str, float | None]:
    closes = [candle.close for candle in window]
    volumes = [candle.volume for candle in window]
    last = window[-1]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    rsi14 = _rsi(closes, 14)
    atr14 = _atr(window, 14)
    avg_volume = _average(volumes[-20:])
    close_5d_ago = closes[-6] if len(closes) >= 6 else closes[0]
    momentum_5d = ((last.close - close_5d_ago) / close_5d_ago * 100) if close_5d_ago else 0.0
    volume_ratio = (last.volume / avg_volume) if avg_volume else 1.0
    atr_percent = (atr14 / last.close * 100) if atr14 and last.close else 0.0
    trend_basis = 50.0
    if ema20 and ema50:
        trend_basis += 25.0 if last.close >= ema20 else -20.0
        trend_basis += 20.0 if ema20 >= ema50 else -15.0
        trend_basis += max(-15.0, min(15.0, (last.close - ema20) / ema20 * 100)) if ema20 else 0.0
    momentum_score = _score(50.0 + momentum_5d * 4.0 + ((rsi14 or 50.0) - 50.0))
    volume_score = _score(45.0 + min(volume_ratio, 3.0) * 20.0)
    risk_score = _score(100.0 - atr_percent * 12.0)
    trend_score = _score(trend_basis)
    momentum_trap_score = _score(max(0.0, (rsi14 or 50.0) - 70.0) * 2.5 + max(0.0, volume_ratio - 2.0) * 10.0)
    quality_score = _score((trend_score + momentum_score + volume_score + risk_score) / 4.0)
    confidence_score = _score((quality_score + trend_score + momentum_score) / 3.0)
    rule_score = _score((quality_score + confidence_score + risk_score) / 3.0)
    return {
        "rule_score": rule_score,
        "trend_score": trend_score,
        "momentum_score": momentum_score,
        "volume_score": volume_score,
        "risk_score": risk_score,
        "confidence_score": confidence_score,
        "quality_score": quality_score,
        "momentum_trap_score": momentum_trap_score,
        "daily_ema20": round(ema20, 6) if ema20 else None,
        "daily_ema50": round(ema50, 6) if ema50 else None,
        "rsi14": round(rsi14, 6) if rsi14 is not None else None,
        "atr14": round(atr14, 6) if atr14 is not None else None,
    }


def _is_setup(strategy: str, features: Mapping[str, float | None], last: HistoricalCandle) -> bool:
    trend = _number(features.get("trend_score"))
    momentum = _number(features.get("momentum_score"))
    quality = _number(features.get("quality_score"))
    ema20 = features.get("daily_ema20")
    rsi = _number(features.get("rsi14"), 50.0)
    if strategy == "momentum":
        return momentum >= 62 and quality >= 50 and bool(ema20 and last.close >= ema20)
    return trend >= 45 and quality >= 48 and 38 <= rsi <= 75 and bool(ema20 and last.close >= ema20 * 0.995)


def _entry_plan(strategy: str, source: HistoricalCandle, features: Mapping[str, float | None]) -> dict[str, float]:
    atr = _number(features.get("atr14"))
    risk = max(atr, source.close * (0.018 if strategy == "momentum" else 0.022))
    entry = source.close * (1.001 if strategy == "momentum" else 1.0)
    stop = max(0.0001, entry - risk)
    target = entry + (risk * (2.0 if strategy == "momentum" else 1.6))
    return {
        "entry_price": round(entry, 6),
        "stop_loss": round(stop, 6),
        "target_1": round(target, 6),
        "risk_reward_1": round((target - entry) / (entry - stop), 6) if entry > stop else 0.0,
    }


def simulate_historical_outcome(
    *,
    entry_price: float,
    stop_loss: float,
    target_1: float,
    future_candles: Sequence[Mapping[str, Any] | HistoricalCandle],
) -> SimulatedOutcome:
    entered = False
    entry_timestamp: str | None = None
    for raw in future_candles:
        high = _number(getattr(raw, "high", None) if isinstance(raw, HistoricalCandle) else raw.get("high"))
        low = _number(getattr(raw, "low", None) if isinstance(raw, HistoricalCandle) else raw.get("low"))
        close_at = str(
            getattr(raw, "candle_close_at", None)
            if isinstance(raw, HistoricalCandle)
            else raw.get("candle_close_at") or raw.get("time") or raw.get("timestamp")
        )
        if not entered:
            if high >= entry_price:
                entered = True
                entry_timestamp = close_at
            else:
                continue
        hit_stop = low <= stop_loss
        hit_target = high >= target_1
        if hit_stop and hit_target:
            return SimulatedOutcome(
                outcome_class=OUTCOME_CLASS_AMBIGUOUS,
                label_state=LABEL_STATE_EXCLUDED,
                status="AMBIGUOUS",
                terminal_timestamp=close_at,
                entry_timestamp=entry_timestamp,
                exit_price=None,
                entry_triggered=True,
                ambiguous=True,
            )
        if hit_stop:
            return SimulatedOutcome(
                outcome_class=OUTCOME_CLASS_LOSS,
                label_state=LABEL_STATE_LABELED,
                status="SL_HIT",
                terminal_timestamp=close_at,
                entry_timestamp=entry_timestamp,
                exit_price=round(stop_loss, 6),
                entry_triggered=True,
            )
        if hit_target:
            return SimulatedOutcome(
                outcome_class=OUTCOME_CLASS_WIN,
                label_state=LABEL_STATE_LABELED,
                status="T1_HIT",
                terminal_timestamp=close_at,
                entry_timestamp=entry_timestamp,
                exit_price=round(target_1, 6),
                entry_triggered=True,
            )
    if entered:
        return SimulatedOutcome(
            outcome_class=OUTCOME_CLASS_INCOMPLETE,
            label_state=LABEL_STATE_UNLABELED,
            status="ACTIVE",
            terminal_timestamp=None,
            entry_timestamp=entry_timestamp,
            exit_price=None,
            entry_triggered=True,
        )
    return SimulatedOutcome(
        outcome_class=OUTCOME_CLASS_NO_ENTRY,
        label_state=LABEL_STATE_EXCLUDED,
        status="NOT_TRIGGERED",
        terminal_timestamp=None,
        entry_timestamp=None,
        exit_price=None,
        entry_triggered=False,
    )


def _paper_trade_evidence(
    *,
    source: HistoricalCandle,
    plan: Mapping[str, float],
    outcome: SimulatedOutcome,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "_id": _historical_setup_id("label", source),
        "paper_only": True,
        "symbol": source.symbol,
        "exchange": source.exchange,
        "timeframe": source.timeframe.upper(),
        "status": outcome.status,
        "outcome_status": outcome.status,
        "entry_triggered": outcome.entry_triggered,
        "entry_price": plan["entry_price"],
        "stop_loss": plan["stop_loss"],
        "initial_stop_loss": plan["stop_loss"],
        "target_1": plan["target_1"],
        "risk_reward_1": plan["risk_reward_1"],
        "quantity": 1.0 if outcome.entry_triggered else 0.0,
        "proposed_quantity": 1.0,
    }
    if outcome.entry_timestamp:
        evidence["entry_time"] = outcome.entry_timestamp
        evidence["entry_triggered_at"] = outcome.entry_timestamp
    if outcome.terminal_timestamp:
        evidence["exit_time"] = outcome.terminal_timestamp
        evidence["closed_at"] = outcome.terminal_timestamp
        evidence["completed_at"] = outcome.terminal_timestamp
    if outcome.outcome_class == OUTCOME_CLASS_WIN and outcome.exit_price is not None:
        evidence["partial_exit_1"] = {
            "exit_price": outcome.exit_price,
            "quantity": 1.0,
            "exited_at": outcome.terminal_timestamp,
        }
    elif outcome.outcome_class == OUTCOME_CLASS_LOSS and outcome.exit_price is not None:
        evidence["stop_exit"] = {
            "exit_price": outcome.exit_price,
            "quantity": 1.0,
            "exited_at": outcome.terminal_timestamp,
        }
    elif outcome.ambiguous:
        evidence["ambiguous"] = True
    return evidence


def _source_snapshot(
    *,
    strategy: str,
    source: HistoricalCandle,
    features: Mapping[str, float | None],
    plan: Mapping[str, float],
    horizon: int,
) -> dict[str, Any]:
    setup_id = _historical_setup_id(strategy, source)
    source_at = source.candle_close_at
    return {
        "source_mode": "historical_ohlcv_preview",
        "data_completeness": "historical_closed_candles",
        "strategy_type": strategy,
        "exchange": source.exchange,
        "canonical_symbol": source.symbol,
        "symbol": source.symbol,
        "timeframe": source.timeframe.upper(),
        "canonical_setup_id": setup_id,
        "source_candle_at": source_at,
        "feature_as_of": source_at,
        "confirmed_at": source_at,
        "calculation_timestamp": source_at,
        "feature_source_timestamp": source_at,
        "feature_source_timestamp_field": "source_candle_at",
        "prediction_horizon": horizon,
        "score_version": HISTORICAL_TRAINING_BRIDGE_VERSION,
        "calculation_version": HISTORICAL_TRAINING_CALCULATION_VERSION,
        "label_version": HISTORICAL_TRAINING_LABEL_VERSION,
        "setup_status": "HISTORICAL_PREVIEW_SETUP",
        "entry_price": plan["entry_price"],
        "stop_loss": plan["stop_loss"],
        "final_stop_loss": plan["stop_loss"],
        "target_1": plan["target_1"],
        "risk_reward_1": plan["risk_reward_1"],
        "data_source_ids": {
            "historical_candle_id": source.candle_id,
            "historical_training_bridge_version": HISTORICAL_TRAINING_BRIDGE_VERSION,
        },
        **{field: features.get(field) for field in FEATURE_COLUMNS},
    }


def _apply_historical_preview_audit(row: dict[str, Any]) -> dict[str, Any]:
    label = row.get("label") if isinstance(row.get("label"), dict) else {}
    audit = row.get("audit") if isinstance(row.get("audit"), dict) else {}
    errors = list(audit.get("validation_errors") or [])
    outcome = label.get("outcome_class")
    if outcome == OUTCOME_CLASS_NO_ENTRY:
        label["label_state"] = LABEL_STATE_EXCLUDED
        if "LABEL_EXCLUDED_NO_ENTRY" not in errors:
            errors.append("LABEL_EXCLUDED_NO_ENTRY")
    elif outcome == OUTCOME_CLASS_INCOMPLETE:
        label["label_state"] = LABEL_STATE_UNLABELED
        if "LABEL_UNRESOLVED_INCOMPLETE" not in errors:
            errors.append("LABEL_UNRESOLVED_INCOMPLETE")
    elif outcome == OUTCOME_CLASS_AMBIGUOUS:
        label["label_state"] = LABEL_STATE_EXCLUDED
        if "LABEL_EXCLUDED_AMBIGUOUS" not in errors:
            errors.append("LABEL_EXCLUDED_AMBIGUOUS")
    row["label"] = label
    audit["validation_errors"] = errors
    audit["label_validation_errors"] = list(dict.fromkeys((audit.get("label_validation_errors") or []) + [
        error for error in errors if error.startswith("LABEL_")
    ]))
    audit["training_eligible"] = (
        label.get("eligible_for_training") is True
        and label.get("label_state") == LABEL_STATE_LABELED
        and not errors
    )
    if not audit["training_eligible"] and not audit.get("exclusion_reason"):
        audit["exclusion_reason"] = errors[0] if errors else outcome
    for provenance in (audit.get("feature_provenance") or {}).values():
        if isinstance(provenance, dict):
            provenance["source_collection"] = HISTORICAL_OHLCV_COLLECTION
    row["audit"] = audit
    return row


def _row_is_eligible(row: Mapping[str, Any]) -> bool:
    audit = row.get("audit") if isinstance(row.get("audit"), Mapping) else {}
    label = row.get("label") if isinstance(row.get("label"), Mapping) else {}
    return (
        audit.get("training_eligible") is True
        and label.get("eligible_for_training") is True
        and label.get("label_state") == LABEL_STATE_LABELED
    )


def _leakage_check(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    violations: list[str] = []
    for index, row in enumerate(rows):
        model_features = row.get("model_features") if isinstance(row.get("model_features"), Mapping) else {}
        feature_keys = set(model_features)
        extra = sorted(feature_keys - set(FEATURE_COLUMNS))
        missing = sorted(set(FEATURE_COLUMNS) - feature_keys)
        blocked = sorted(feature_keys & BLOCKED_MODEL_FEATURES)
        if extra:
            violations.append(f"row_{index}:FEATURE_NOT_WHITELISTED:{','.join(extra)}")
        if missing:
            violations.append(f"row_{index}:FEATURE_MISSING:{','.join(missing)}")
        if blocked:
            violations.append(f"row_{index}:BLOCKED_MODEL_FEATURE:{','.join(blocked)}")
    return {
        "status": "PASS" if not violations else "FAIL",
        "checked_rows": len(rows),
        "feature_columns": list(FEATURE_COLUMNS),
        "violations": violations,
    }


def build_historical_training_preview(
    candle_documents: Sequence[Mapping[str, Any]],
    *,
    lookback: int = DEFAULT_LOOKBACK,
    horizon: int = DEFAULT_HORIZON,
    strategies: str | Sequence[str] | None = DEFAULT_STRATEGIES,
    limit: int | None = None,
) -> dict[str, Any]:
    if lookback < 2:
        raise ValueError("lookback must be at least 2")
    if horizon < 1:
        raise ValueError("horizon must be at least 1")
    parsed_strategies = parse_strategies(strategies)
    grouped: dict[str, list[HistoricalCandle]] = defaultdict(list)
    skipped = Counter()
    for document in candle_documents:
        candle = _candle_from_doc(document)
        if candle is None:
            skipped["invalid_or_open_candle"] += 1
            continue
        grouped[candle.symbol].append(candle)
    for candles in grouped.values():
        candles.sort(key=lambda candle: candle.candle_open_at)

    rows: list[dict[str, Any]] = []
    per_symbol: dict[str, Counter] = defaultdict(Counter)
    for symbol in sorted(grouped):
        candles = grouped[symbol]
        if len(candles) < lookback + horizon:
            skipped["insufficient_history"] += 1
            continue
        for source_index in range(lookback - 1, len(candles) - horizon):
            window = candles[source_index - lookback + 1 : source_index + 1]
            future = candles[source_index + 1 : source_index + 1 + horizon]
            source = window[-1]
            if source.is_closed is not True:
                skipped["open_source_candle"] += 1
                continue
            features = _features_for_window(window)
            for strategy in parsed_strategies:
                if not _is_setup(strategy, features, source):
                    continue
                plan = _entry_plan(strategy, source, features)
                outcome = simulate_historical_outcome(
                    entry_price=plan["entry_price"],
                    stop_loss=plan["stop_loss"],
                    target_1=plan["target_1"],
                    future_candles=future,
                )
                source_row = _source_snapshot(
                    strategy=strategy,
                    source=source,
                    features=features,
                    plan=plan,
                    horizon=horizon,
                )
                canonical = build_canonical_training_row(
                    source_row,
                    generated_at=source.candle_close_at,
                    paper_trade=_paper_trade_evidence(source=source, plan=plan, outcome=outcome),
                )
                canonical = _apply_historical_preview_audit(canonical)
                rows.append(canonical)
                per_symbol[symbol]["setup_rows"] += 1
                per_symbol[symbol][str(canonical["label"]["outcome_class"])] += 1
                if _row_is_eligible(canonical):
                    per_symbol[symbol]["eligible_rows"] += 1
                elif canonical["label"]["label_state"] == LABEL_STATE_UNLABELED:
                    per_symbol[symbol]["unlabeled_rows"] += 1
                else:
                    per_symbol[symbol]["excluded_rows"] += 1
                if limit is not None and len(rows) >= limit:
                    break
            if limit is not None and len(rows) >= limit:
                break
        if limit is not None and len(rows) >= limit:
            break

    label_counts = Counter(str(row["label"]["outcome_class"]) for row in rows)
    eligible_count = sum(1 for row in rows if _row_is_eligible(row))
    unlabeled_count = sum(1 for row in rows if row["label"]["label_state"] == LABEL_STATE_UNLABELED)
    excluded_count = len(rows) - eligible_count - unlabeled_count
    return {
        "ok": True,
        "mode": "preview",
        "bridge_version": HISTORICAL_TRAINING_BRIDGE_VERSION,
        "read_only": True,
        "mongo_writes_enabled": False,
        "mongo_writes_performed": 0,
        "training_executed": False,
        "model_file_written": False,
        "tradingview_calls": 0,
        "source_collection": HISTORICAL_OHLCV_COLLECTION,
        "lookback": lookback,
        "horizon": horizon,
        "strategies": list(parsed_strategies),
        "source_candles": sum(len(candles) for candles in grouped.values()),
        "symbols": sorted(grouped),
        "setup_rows": len(rows),
        "eligible_rows": eligible_count,
        "excluded_rows": excluded_count,
        "unlabeled_rows": unlabeled_count,
        "per_symbol_counts": {symbol: dict(counter) for symbol, counter in sorted(per_symbol.items())},
        "per_label_counts": dict(sorted(label_counts.items())),
        "feature_columns": list(FEATURE_COLUMNS),
        "leakage_check": _leakage_check(rows),
        "skipped_counts": dict(sorted(skipped.items())),
        "rows": rows,
    }


async def build_historical_training_preview_from_collection(
    collection: Any,
    *,
    provider: str | None = "yfinance",
    exchange: str | None = "NSE",
    timeframe: str | None = "1d",
    symbols: Sequence[str] | None = None,
    lookback: int = DEFAULT_LOOKBACK,
    horizon: int = DEFAULT_HORIZON,
    strategies: str | Sequence[str] | None = DEFAULT_STRATEGIES,
    limit: int | None = None,
) -> dict[str, Any]:
    query: dict[str, Any] = {"is_closed": True}
    if provider:
        query["provider"] = provider
    if exchange:
        query["exchange"] = exchange
    if timeframe:
        query["timeframe"] = timeframe
    if symbols:
        query["canonical_symbol"] = {"$in": [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]}
    cursor = collection.find(query, {"_id": 0})
    try:
        cursor = cursor.sort([("canonical_symbol", 1), ("candle_open_at", 1)])
    except TypeError:
        cursor = cursor.sort("candle_open_at", 1)
    documents = [row async for row in cursor]
    return build_historical_training_preview(
        documents,
        lookback=lookback,
        horizon=horizon,
        strategies=strategies,
        limit=limit,
    )
