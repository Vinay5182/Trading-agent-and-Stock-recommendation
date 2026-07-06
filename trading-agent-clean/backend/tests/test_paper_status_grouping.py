import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes.paper import (
    TARGET_COMPLETED_STATUSES,
    is_ambiguous_paper_trade,
    is_canceled_or_expired_trade,
    is_completed_target_trade,
    is_sl_hit_trade,
    paper_api_row,
    ui_status_for_trade,
)


def trade(status: str, **overrides) -> dict:
    return {
        "symbol": "TEST",
        "status": status,
        "outcome_status": status,
        "paper_only": True,
        "quantity": 10,
        "original_quantity": 10,
        "paper_pnl": 999,
        **overrides,
    }


def test_expired_is_not_target_completed() -> None:
    row = trade("EXPIRED")

    assert "EXPIRED" not in TARGET_COMPLETED_STATUSES
    assert is_completed_target_trade(row) is False
    assert is_canceled_or_expired_trade(row) is True
    assert ui_status_for_trade(row) == "Expired / Not Triggered"


def test_not_triggered_is_not_target_completed() -> None:
    row = trade("NOT_TRIGGERED", entry_triggered=False)

    assert "NOT_TRIGGERED" not in TARGET_COMPLETED_STATUSES
    assert is_completed_target_trade(row) is False
    assert is_canceled_or_expired_trade(row) is True
    assert ui_status_for_trade(row) == "Expired / Not Triggered"


def test_ambiguous_is_not_target_completed() -> None:
    row = trade("AMBIGUOUS")

    assert "AMBIGUOUS" not in TARGET_COMPLETED_STATUSES
    assert is_completed_target_trade(row) is False
    assert is_ambiguous_paper_trade(row) is True


def test_stopped_and_sl_statuses_are_not_target_completed() -> None:
    for status in ("STOPPED", "STOP_HIT", "SL_HIT", "STOPPED_AFTER_T1", "LOST_SL"):
        row = trade(status)
        assert status not in TARGET_COMPLETED_STATUSES
        assert is_completed_target_trade(row) is False
        assert is_sl_hit_trade(row) is True


def test_target_2_hit_is_target_completed() -> None:
    row = trade("TARGET_2_HIT")

    assert is_completed_target_trade(row) is True
    assert ui_status_for_trade(row) == "Completed"


def test_completed_rows_remain_target_completed_contract() -> None:
    row = trade("COMPLETED")

    assert is_completed_target_trade(row) is True
    assert ui_status_for_trade(row) == "Completed"


def test_expired_not_triggered_rows_do_not_show_realized_profit_or_quantity() -> None:
    row = paper_api_row(trade("EXPIRED", exit_price=123))

    assert row["paper_pnl"] == 0.0
    assert row["pnl"] == 0.0
    assert row["pnl_display"] is None
    assert row["pnl_note"] == "Expired / not triggered; no realized P&L"
    assert row["bought_quantity"] == 0
    assert row["open_quantity"] == 0
    assert row["quantity_integrity_warning"] is None
