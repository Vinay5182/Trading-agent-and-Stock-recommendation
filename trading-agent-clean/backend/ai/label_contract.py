import math
import json
from typing import Any, Mapping, Sequence
from datetime import datetime, timezone

AI_LABEL_CONTRACT_VERSION = "phase4-v1"

# Label States
LABEL_STATE_LABELED = "LABELED"
LABEL_STATE_UNLABELED = "UNLABELED"
LABEL_STATE_EXCLUDED = "EXCLUDED"

# Outcome Classes
OUTCOME_CLASS_WIN = "WIN"
OUTCOME_CLASS_LOSS = "LOSS"
OUTCOME_CLASS_BREAKEVEN = "BREAKEVEN"
OUTCOME_CLASS_AMBIGUOUS = "AMBIGUOUS"
OUTCOME_CLASS_NO_ENTRY = "NO_ENTRY"
OUTCOME_CLASS_INCOMPLETE = "INCOMPLETE"
OUTCOME_CLASS_INVALID = "INVALID"

# Terminal Statuses
TERMINAL_STATUSES = {
    "AMBIGUOUS", "CLOSED", "COMPLETED", "EXPIRED", "LOST_SL", "SL_HIT",
    "STOP_HIT", "STOPPED", "STOPPED_AFTER_T1", "T1_HIT", "T2_HIT", "T3_HIT",
    "TARGET_HIT", "TARGET_1_HIT_FINAL", "TARGET_2_HIT", "TARGET_3_HIT",
    "WON_T1", "WON_T2", "WON_T3"
}

TARGET_STATUSES = {
    "COMPLETED", "T1_HIT", "T2_HIT", "T3_HIT", "TARGET_HIT",
    "TARGET_1_HIT_FINAL", "TARGET_2_HIT", "TARGET_3_HIT", "WON_T1", "WON_T2", "WON_T3"
}

LOSS_STATUSES = {
    "SL_HIT", "LOST_SL", "STOP_HIT", "STOPPED", "STOPPED_AFTER_T1"
}


def parse_strict_utc(value: Any) -> tuple[datetime | None, dict[str, Any]]:
    if value is None:
        return None, {"quality": "MISSING"}
    text = str(value).strip()
    if not text:
        return None, {"quality": "MISSING"}
    # Check if timezone is specified via offset or Z suffix
    # A canonical UTC timestamp has Z suffix or +00:00
    if not (text.endswith("Z") or "+00:00" in text or "+05:30" in text or "T" in text):
        return None, {"quality": "MALFORMED"}
    try:
        # standard ISO format parsing
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None, {"quality": "LEGACY_TIMEZONE_UNKNOWN"}
        return dt.astimezone(timezone.utc), {"quality": "UTC_AWARE"}
    except ValueError:
        return None, {"quality": "MALFORMED"}


def _append_once(errors: list[str], code: str) -> None:
    if code not in errors:
        errors.append(code)


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _is_present(value: Any) -> bool:
    return value not in (None, "")


def _clean_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _same_identity(left: Mapping[str, Any], right: Mapping[str, Any], fields: Sequence[str]) -> bool:
    for field in fields:
        left_value = _clean_text(left.get(field))
        right_value = _clean_text(right.get(field))
        if left_value and right_value and left_value != right_value:
            return False
    return True


def _numeric_or_none(value: Any, invalid_code: str, errors: list[str], *, positive: bool = False) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        _append_once(errors, invalid_code)
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        _append_once(errors, invalid_code)
        return None
    if not math.isfinite(number):
        _append_once(errors, invalid_code)
        return None
    if positive and number <= 0:
        _append_once(errors, invalid_code)
        return None
    return number


def _journal_signature(journal: Mapping[str, Any]) -> str:
    relevant = {
        key: journal.get(key)
        for key in (
            "paper_trade_id",
            "setup_id",
            "canonical_setup_id",
            "status",
            "outcome_status",
            "exit_reason",
            "exit_price",
            "exit_time",
            "closed_at",
            "completed_at",
            "ambiguous",
            "partial_exits",
            "stop_exit",
        )
    }
    return _stable_json(relevant)


def _terminal_timestamp_source(row: Mapping[str, Any]) -> Any:
    return row.get("exit_time") or row.get("closed_at") or row.get("completed_at")


def _normalize_journals(
    trade_journal: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    trade: Mapping[str, Any],
    metadata: Mapping[str, Any],
    errors: list[str],
) -> Mapping[str, Any]:
    if trade_journal is None:
        return {}
    journals = list(trade_journal) if isinstance(trade_journal, list | tuple) else [trade_journal]
    valid: list[Mapping[str, Any]] = []
    seen_signatures: set[str] = set()
    paper_trade_id = _clean_text(trade.get("_id") or trade.get("paper_trade_id"))

    for journal in journals:
        if not isinstance(journal, Mapping):
            _append_once(errors, "LABEL_SOURCE_CONFLICT")
            continue
        journal_paper_trade_id = _clean_text(journal.get("paper_trade_id"))
        if paper_trade_id and journal_paper_trade_id and paper_trade_id != journal_paper_trade_id:
            _append_once(errors, "LABEL_SOURCE_CONFLICT")
            continue
        if not _same_identity(metadata, journal, ("setup_id", "canonical_setup_id", "identity_key")):
            _append_once(errors, "LABEL_SOURCE_CONFLICT")
            continue
        if not _same_identity(trade, journal, ("setup_id", "canonical_setup_id")):
            _append_once(errors, "LABEL_SOURCE_CONFLICT")
            continue
        signature = _journal_signature(journal)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        valid.append(journal)

    if not valid:
        return {}

    def sort_key(journal: Mapping[str, Any]) -> tuple[str, str, str]:
        timestamp = _clean_text(_terminal_timestamp_source(journal)) or ""
        status = _clean_text(journal.get("status") or journal.get("outcome_status") or journal.get("exit_reason")) or ""
        return (timestamp, status, _journal_signature(journal))

    ordered = sorted(valid, key=sort_key)
    terminal_signatures = {
        _stable_json({
            "status": _clean_text(j.get("status") or j.get("outcome_status") or j.get("exit_reason")),
            "exit_price": j.get("exit_price"),
            "timestamp": _terminal_timestamp_source(j),
            "partial_exits": j.get("partial_exits"),
            "stop_exit": j.get("stop_exit"),
        })
        for j in ordered
    }
    if len(terminal_signatures) > 1:
        _append_once(errors, "DUPLICATE_TERMINAL_EVIDENCE")
    return ordered[0]


def build_deterministic_label(
    canonical_row: Mapping[str, Any],
    paper_trade: Mapping[str, Any] | None = None,
    trade_journal: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    market_snapshots: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    errors = []
    warnings = []

    # 1. Authoritative Evidence Precedence
    trade = paper_trade or {}

    # Extract plan context / parameters
    metadata = canonical_row.get("metadata") or {}
    journal = _normalize_journals(trade_journal, trade, metadata, errors)

    # Check for trade ID linkage
    paper_trade_id = trade.get("_id") or trade.get("paper_trade_id")
    journal_paper_trade_id = journal.get("paper_trade_id")
    if paper_trade_id and journal_paper_trade_id and str(paper_trade_id) != str(journal_paper_trade_id):
        _append_once(errors, "LABEL_SOURCE_CONFLICT")

    # Check for contradictory duplicate evidence in statuses
    trade_status = str(trade.get("status") or trade.get("outcome_status") or "").upper()
    journal_status = str(journal.get("status") or journal.get("outcome_status") or journal.get("exit_reason") or "").upper()

    # Verify status consistency
    if trade_status and journal_status:
        trade_win = trade_status in TARGET_STATUSES or "WIN" in trade_status
        trade_loss = trade_status in LOSS_STATUSES or "LOSS" in trade_status
        journal_win = journal_status in TARGET_STATUSES or "WIN" in journal_status
        journal_loss = journal_status in LOSS_STATUSES or "LOSS" in journal_status
        if (trade_win and journal_loss) or (trade_loss and journal_win):
            _append_once(errors, "OUTCOME_STATUS_CONFLICT")

    # Determine status & terminality
    status = trade.get("status") or trade.get("outcome_status") or journal.get("status") or journal.get("outcome_status") or "WAITING_FOR_ENTRY"
    status = str(status).upper()

    is_entered = status not in {"WAITING_FOR_ENTRY", "ENTRY_TRIGGERED", "WAITING_FOR_CAPITAL", "PLANNED", "NOT_TRIGGERED"}

    # Entry validation
    entry_price = _numeric_or_none(
        trade.get("entry_price") if _is_present(trade.get("entry_price")) else metadata.get("entry_price"),
        "ENTRY_PRICE_INVALID",
        errors,
        positive=True,
    )
    initial_stop = _numeric_or_none(
        (
            trade.get("initial_stop_loss")
            if _is_present(trade.get("initial_stop_loss"))
            else trade.get("stop_loss")
            if _is_present(trade.get("stop_loss"))
            else metadata.get("final_stop_loss")
            if _is_present(metadata.get("final_stop_loss"))
            else metadata.get("stop_loss")
        ),
        "INITIAL_STOP_INVALID",
        errors,
        positive=True,
    )

    if is_entered:
        if entry_price is None and "ENTRY_PRICE_INVALID" not in errors:
            _append_once(errors, "ENTRY_PRICE_MISSING")
        if initial_stop is None and "INITIAL_STOP_INVALID" not in errors:
            _append_once(errors, "INITIAL_STOP_MISSING")

    initial_risk = None
    if entry_price is not None and initial_stop is not None:
        initial_risk = entry_price - initial_stop
        if is_entered and initial_risk <= 0:
            _append_once(errors, "INITIAL_RISK_INVALID")

    # Time validation
    feature_as_of = metadata.get("feature_as_of")
    entry_time = (
        trade.get("entry_triggered_at")
        or trade.get("entry_time")
        or metadata.get("entry_time")
    )

    if is_entered and not entry_time:
        _append_once(errors, "ENTRY_TIME_MISSING")

    # Parse and check timestamps
    feature_as_of_dt = None
    if feature_as_of:
        dt, info = parse_strict_utc(feature_as_of)
        if info["quality"] != "UTC_AWARE":
            _append_once(errors, "LABEL_TIMESTAMP_UNSAFE")
        else:
            feature_as_of_dt = dt
    else:
        _append_once(errors, "LABEL_TIMESTAMP_MISSING")

    entry_time_dt = None
    if entry_time:
        dt, info = parse_strict_utc(entry_time)
        if info["quality"] != "UTC_AWARE":
            if is_entered:
                _append_once(errors, "LABEL_TIMESTAMP_UNSAFE")
        else:
            entry_time_dt = dt

    if feature_as_of_dt and entry_time_dt and entry_time_dt < feature_as_of_dt:
        # Check order
        pass

    is_terminal = status in TERMINAL_STATUSES or trade.get("entry_triggered") is True and status == "CLOSED"

    # 2. Check for Ambiguity
    is_ambiguous = (
        status == "AMBIGUOUS" or
        journal.get("ambiguous") is True or
        trade.get("ambiguous") is True or
        journal_status == "AMBIGUOUS"
    )

    # 3. Partial exits and quantity conservation
    entered_qty = _numeric_or_none(
        trade.get("quantity") if _is_present(trade.get("quantity")) else trade.get("proposed_quantity"),
        "EXIT_QUANTITY_INVALID",
        errors,
    )
    if entered_qty is not None and entered_qty < 0:
        _append_once(errors, "EXIT_QUANTITY_INVALID")

    exited_qty_sum = 0.0
    partial_exits_valid = True
    partial_exits_list = []

    # Check partial_exit_1, partial_exit_2, partial_exit_3, stop_exit
    for field in ("partial_exit_1", "partial_exit_2", "partial_exit_3"):
        pe = trade.get(field) or journal.get("partial_exits", {}).get(field)
        if pe:
            if isinstance(pe, dict):
                px = _numeric_or_none(pe.get("exit_price"), "EXIT_PRICE_INVALID", errors)
                pq = _numeric_or_none(pe.get("quantity"), "EXIT_QUANTITY_INVALID", errors)
                pa = pe.get("exited_at")
                if px is None or pq is None or pq < 0:
                    partial_exits_valid = False
                    _append_once(errors, "PARTIAL_EXIT_EVIDENCE_INCOMPLETE")
                else:
                    exited_qty_sum += pq
                    partial_exits_list.append((px, pq, pa))
            else:
                partial_exits_valid = False
                _append_once(errors, "PARTIAL_EXIT_EVIDENCE_INCOMPLETE")

    stop_exit = trade.get("stop_exit") or journal.get("stop_exit")
    if stop_exit:
        if isinstance(stop_exit, dict):
            sx_price = _numeric_or_none(stop_exit.get("exit_price"), "EXIT_PRICE_INVALID", errors)
            sx_qty = _numeric_or_none(stop_exit.get("quantity"), "EXIT_QUANTITY_INVALID", errors)
            sx_at = stop_exit.get("exited_at")
            if sx_price is None or sx_qty is None or sx_qty < 0:
                partial_exits_valid = False
                _append_once(errors, "EXIT_PRICE_MISSING")
            else:
                exited_qty_sum += sx_qty
                partial_exits_list.append((sx_price, sx_qty, sx_at))
        else:
            partial_exits_valid = False
            _append_once(errors, "EXIT_PRICE_MISSING")

    # If terminal, check quantity conservation
    if is_terminal and entered_qty is not None:
        if abs(exited_qty_sum - entered_qty) > 1e-4:
            if exited_qty_sum > entered_qty + 1e-4:
                _append_once(errors, "QUANTITY_CONSERVATION_FAILED")
            elif is_terminal:
                # Terminal but didn't exit all quantity
                _append_once(errors, "QUANTITY_CONSERVATION_FAILED")

    # Event order and timestamp safety for exits
    exit_time_dt = None
    for px, pq, pa in partial_exits_list:
        if pa:
            dt, info = parse_strict_utc(pa)
            if info["quality"] != "UTC_AWARE":
                _append_once(errors, "LABEL_TIMESTAMP_UNSAFE")
            else:
                if entry_time_dt and dt < entry_time_dt:
                    _append_once(errors, "LABEL_BEFORE_ENTRY")
                if feature_as_of_dt and dt < feature_as_of_dt:
                    _append_once(errors, "LABEL_BEFORE_FEATURE_AS_OF")
                if exit_time_dt is None or dt > exit_time_dt:
                    exit_time_dt = dt

    explicit_terminal_time = (_terminal_timestamp_source(trade) or _terminal_timestamp_source(journal)) if is_terminal else None
    explicit_terminal_dt = None
    if explicit_terminal_time:
        dt, info = parse_strict_utc(explicit_terminal_time)
        if info["quality"] != "UTC_AWARE":
            _append_once(errors, "LABEL_TIMESTAMP_UNSAFE")
        else:
            explicit_terminal_dt = dt
            if entry_time_dt and dt < entry_time_dt:
                _append_once(errors, "LABEL_BEFORE_ENTRY")
            if feature_as_of_dt and dt < feature_as_of_dt:
                _append_once(errors, "LABEL_BEFORE_FEATURE_AS_OF")
    elif is_terminal and exit_time_dt is None:
        _append_once(errors, "LABEL_TIMESTAMP_MISSING")

    # R-multiple computation
    realized_r = None
    if entered_qty and initial_risk and partial_exits_valid and exited_qty_sum > 0:
        total_payout = sum(px * pq for px, pq, pa in partial_exits_list)
        weighted_exit = total_payout / exited_qty_sum
        realized_r = (weighted_exit - entry_price) / initial_risk

    # Classification
    outcome_class = OUTCOME_CLASS_INCOMPLETE
    label_state = LABEL_STATE_UNLABELED
    eligible_for_training = False

    if is_ambiguous:
        outcome_class = OUTCOME_CLASS_AMBIGUOUS
        label_state = LABEL_STATE_EXCLUDED
        _append_once(errors, "LABEL_EXCLUDED_AMBIGUOUS")
    elif not is_terminal:
        if status in {"WAITING_FOR_ENTRY", "ENTRY_TRIGGERED", "PLANNED", "NOT_TRIGGERED"}:
            outcome_class = OUTCOME_CLASS_NO_ENTRY
            label_state = LABEL_STATE_EXCLUDED
            _append_once(errors, "ENTRY_NOT_TRIGGERED")
        else:
            outcome_class = OUTCOME_CLASS_INCOMPLETE
            label_state = LABEL_STATE_UNLABELED
            _append_once(errors, "TRADE_NOT_TERMINAL")
    else:
        # Check if R could be computed
        if realized_r is not None:
            tolerance = 0.01
            if realized_r > tolerance:
                outcome_class = OUTCOME_CLASS_WIN
            elif realized_r < -tolerance:
                outcome_class = OUTCOME_CLASS_LOSS
            else:
                outcome_class = OUTCOME_CLASS_BREAKEVEN
        else:
            # Fallback outcome classification from status
            if status in TARGET_STATUSES:
                outcome_class = OUTCOME_CLASS_WIN
                warnings.append("FALLBACK_CLASSIFICATION_FROM_STATUS")
            elif status in LOSS_STATUSES:
                outcome_class = OUTCOME_CLASS_LOSS
                warnings.append("FALLBACK_CLASSIFICATION_FROM_STATUS")
            elif "BREAKEVEN" in status:
                outcome_class = OUTCOME_CLASS_BREAKEVEN
                warnings.append("FALLBACK_CLASSIFICATION_FROM_STATUS")
            else:
                outcome_class = OUTCOME_CLASS_INVALID
                _append_once(errors, "EXIT_EVIDENCE_MISSING")

        if outcome_class in {OUTCOME_CLASS_WIN, OUTCOME_CLASS_LOSS}:
            eligible_for_training = True
            label_state = LABEL_STATE_LABELED
        elif outcome_class == OUTCOME_CLASS_BREAKEVEN:
            eligible_for_training = True
            label_state = LABEL_STATE_LABELED
        else:
            eligible_for_training = False
            label_state = LABEL_STATE_EXCLUDED

    # Check validation errors
    if errors:
        # Any errors means it is excluded or incomplete
        eligible_for_training = False
        if "TRADE_NOT_TERMINAL" in errors or "ENTRY_NOT_TRIGGERED" in errors:
            label_state = LABEL_STATE_UNLABELED
        else:
            label_state = LABEL_STATE_EXCLUDED
            # If invalid or contradictory
            if outcome_class not in {OUTCOME_CLASS_AMBIGUOUS, OUTCOME_CLASS_NO_ENTRY}:
                outcome_class = OUTCOME_CLASS_INVALID

    # Deduplicate reason codes
    unique_errors = []
    for err in errors:
        if err not in unique_errors:
            unique_errors.append(err)

    # Compute label timestamp
    label_timestamp = None
    if exit_time_dt:
        label_timestamp = exit_time_dt.isoformat().replace("+00:00", "Z")
    elif explicit_terminal_dt:
        label_timestamp = explicit_terminal_dt.isoformat().replace("+00:00", "Z")

    return {
        "label_contract_version": AI_LABEL_CONTRACT_VERSION,
        "label_state": label_state,
        "outcome_class": outcome_class,
        "training_label": outcome_class if eligible_for_training else None,
        "label_timestamp": label_timestamp,
        "label_source": "paper_trade" if paper_trade else "trade_journal" if journal else "none",
        "terminal_reason": trade.get("exit_reason") or journal.get("exit_reason") or status,
        "realized_r_multiple": realized_r if (realized_r is not None and math.isfinite(realized_r)) else None,
        "evidence_summary": {
            "entered_quantity": entered_qty,
            "exited_quantity_sum": exited_qty_sum,
            "partial_exits_count": len(partial_exits_list),
            "entry_price": entry_price,
            "initial_stop": initial_stop,
            "is_terminal": is_terminal,
        },
        "validation_errors": unique_errors,
        "warnings": warnings,
        "eligible_for_training": eligible_for_training,
    }
