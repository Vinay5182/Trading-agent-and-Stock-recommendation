import asyncio
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes import dashboard


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = [row.copy() for row in rows]
        self.index = 0

    def sort(self, field: str, direction: int = 1):
        reverse = direction < 0
        self.rows = sorted(self.rows, key=lambda row: str(row.get(field) or ""), reverse=reverse)
        return self

    def limit(self, count: int):
        self.rows = self.rows[:count]
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self.index]
        self.index += 1
        return row


class FakeCollection:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def find(self, query: dict | None = None, *_args, **_kwargs):
        query = query or {}
        return FakeCursor(
            [
                row
                for row in self.rows
                if all(row.get(key) == expected for key, expected in query.items())
            ]
        )


def test_dashboard_uses_journal_realized_pnl_and_open_margin_formulas(monkeypatch) -> None:
    open_trade = {
        "symbol": "OPEN",
        "paper_only": True,
        "status": "ACTIVE",
        "outcome_status": "ACTIVE",
        "entry_price": 100.0,
        "quantity": 100,
        "quantity_remaining": 100,
        "latest_close": 108.0,
        "paper_pnl": 800.0,
        "updated_at": "2026-06-16T12:00:00",
    }
    partial_trade = {
        "symbol": "PART",
        "paper_only": True,
        "status": "T1_PARTIAL",
        "outcome_status": "T1_PARTIAL",
        "entry_price": 200.0,
        "quantity": 50,
        "quantity_remaining": 20,
        "remaining_unrealized_pnl": 400.0,
        "updated_at": "2026-06-16T13:00:00",
    }
    waiting_trade = {
        "symbol": "WAIT",
        "paper_only": True,
        "status": "WAITING_FOR_ENTRY",
        "outcome_status": "WAITING_FOR_ENTRY",
        "entry_triggered": False,
        "entry_price": 75.0,
        "quantity": 40,
        "updated_at": "2026-06-16T09:00:00",
    }
    completed_trade = {
        "symbol": "DONE",
        "paper_only": True,
        "status": "COMPLETED",
        "outcome_status": "T3_HIT",
        "entry_price": 110.0,
        "quantity": 20,
        "quantity_remaining": 0,
        "updated_at": "2026-06-17T09:00:00",
    }
    stopped_trade = {
        "symbol": "STOP",
        "paper_only": True,
        "status": "SL_HIT",
        "outcome_status": "SL_HIT",
        "entry_price": 90.0,
        "quantity": 25,
        "quantity_remaining": 0,
        "updated_at": "2026-06-18T09:00:00",
    }
    ambiguous_trade = {
        "symbol": "AMBOPEN",
        "paper_only": True,
        "status": "AMBIGUOUS",
        "outcome_status": "AMBIGUOUS",
        "entry_price": 140.0,
        "quantity": 12,
        "updated_at": "2026-06-19T09:00:00",
    }
    journal_win = {
        "symbol": "WIN",
        "strategy_type": "Swing",
        "exit_reason": "T3_HIT",
        "profit_percent": 31.0,
        "RR": 4.0,
        "total_trade_pnl": 310.0,
        "exit_date": "2026-06-16T10:00:00",
    }
    journal_loss = {
        "symbol": "LOSS",
        "strategy_type": "Momentum",
        "exit_reason": "SL_HIT",
        "SL_HIT": True,
        "profit_percent": -10.0,
        "RR": -1.0,
        "total_trade_pnl": -100.0,
        "exit_date": "2026-06-17T10:00:00",
    }
    journal_ambiguous = {
        "symbol": "AMB",
        "strategy_type": "Swing",
        "exit_reason": "AMBIGUOUS",
        "ambiguous": True,
        "profit_percent": 99.0,
        "total_trade_pnl": 999.0,
        "exit_date": "2026-06-18T10:00:00",
    }
    fake_db = type(
        "FakeDb",
        (),
        {
            "paper_trades": FakeCollection([open_trade, partial_trade, waiting_trade, completed_trade, stopped_trade, ambiguous_trade]),
            "trade_journal": FakeCollection([journal_win, journal_loss, journal_ambiguous]),
        },
    )()
    monkeypatch.setattr(dashboard, "get_database", lambda: fake_db)

    result = asyncio.run(dashboard.get_paper_equity())

    assert result["starting_virtual_balance"] == 250000.0
    assert result["starting_virtual_capital"] == 250000.0
    assert result["starting_virtual_capital"] == result["starting_virtual_balance"]
    assert result["realized_pnl"] == 1209.0
    assert result["current_virtual_balance"] == 251209.0
    assert result["effective_exposure"] == 14000.0
    assert result["open_margin_used"] == 10000.0
    assert result["available_margin"] == 241209.0
    assert result["max_buying_power"] == 628022.5
    assert result["available_buying_power"] == 603022.5
    assert result["broker_funded"] == 4000.0
    assert result["buying_power_usage_percent"] == 3.98
    assert result["unrealized_pnl"] == 1200.0
    assert result["total_pnl"] == 2409.0
    assert result["virtual_return_percent"] == 0.96
    assert result["equity_curve"][-1]["value"] == 251209.0
    assert result["current_virtual_balance"] == result["starting_virtual_balance"] + result["realized_pnl"]
    assert result["total_pnl"] == result["realized_pnl"] + result["unrealized_pnl"]
    assert result["virtual_return_percent"] == round(result["total_pnl"] / result["starting_virtual_balance"] * 100, 2)
    assert result["max_buying_power"] == result["current_virtual_balance"] * result["leverage"]
    assert result["available_margin"] == result["current_virtual_balance"] - result["open_margin_used"]
    assert result["available_buying_power"] == result["available_margin"] * result["leverage"]
    assert result["broker_funded"] == result["effective_exposure"] - result["open_margin_used"]
    assert result["completed_trades"] == 1
    assert result["sl_hit_count"] == 1
    assert result["ambiguous_count"] == 1
    assert result["status_counts"] == {
        "waiting": 1,
        "active": 1,
        "partial": 1,
        "completed": 1,
        "sl_hit": 1,
        "ambiguous": 1,
    }
    assert result["win_rate_percent"] == 50.0
    assert result["profit_factor"] == 3.1
    assert result["average_rr"] == 1.5
    assert result["monthly_pnl_rows"] == [{"month": "2026-06", "pnl": 1209.0}]
    open_row = next(row for row in result["open_position_exposure"] if row["symbol"] == "OPEN")
    assert open_row["effective_exposure"] == 10000.0
    assert open_row["actual_trade_capital"] == 5000.0
    partial_row = next(row for row in result["open_position_exposure"] if row["symbol"] == "PART")
    assert partial_row["effective_exposure"] == 4000.0
    assert partial_row["actual_trade_capital"] == 5000.0


def test_dashboard_empty_data_returns_zero_metrics(monkeypatch) -> None:
    fake_db = type(
        "FakeDb",
        (),
        {
            "paper_trades": FakeCollection([]),
            "trade_journal": FakeCollection([]),
        },
    )()
    monkeypatch.setattr(dashboard, "get_database", lambda: fake_db)

    result = asyncio.run(dashboard.get_paper_equity())

    assert result["starting_virtual_balance"] == 250000.0
    assert result["starting_virtual_capital"] == 250000.0
    assert result["starting_virtual_capital"] == result["starting_virtual_balance"]
    assert result["realized_pnl"] == 0.0
    assert result["current_virtual_balance"] == 250000.0
    assert result["open_margin_used"] == 0.0
    assert result["available_margin"] == 250000.0
    assert result["max_buying_power"] == 625000.0
    assert result["available_buying_power"] == 625000.0
    assert result["effective_exposure"] == 0.0
    assert result["broker_funded"] == 0.0
    assert result["buying_power_usage_percent"] == 0.0
    assert result["realized_pnl"] == result["total_pnl"]
    assert result["virtual_return_percent"] == 0.0
    assert result["win_rate_percent"] == 0.0
    assert result["profit_factor"] == 0.0
    assert result["average_rr"] == 0.0
    assert result["status_counts"] == {
        "waiting": 0,
        "active": 0,
        "partial": 0,
        "completed": 0,
        "sl_hit": 0,
        "ambiguous": 0,
    }


def test_dashboard_includes_old_ambiguous_realized_pnl_without_win_rate(monkeypatch) -> None:
    journal_win = {
        "symbol": "WIN",
        "strategy_type": "Swing",
        "exit_reason": "T3_HIT",
        "profit_percent": 10.0,
        "total_trade_pnl": 100.0,
        "exit_date": "2026-06-16T10:00:00",
    }
    old_ambiguous_profit = {
        "symbol": "OLDAMBPROFIT",
        "strategy_type": "Swing",
        "exit_reason": "AMBIGUOUS",
        "status": "AMBIGUOUS",
        "outcome_status": "AMBIGUOUS",
        "ambiguous": True,
        "total_trade_pnl": 500.0,
        "profit_percent": 500.0,
        "exit_date": "2024-01-05T10:00:00",
    }
    old_ambiguous_loss = {
        "symbol": "OLDAMBLOSS",
        "strategy_type": "Momentum",
        "exit_reason": "AMBIGUOUS",
        "status": "AMBIGUOUS",
        "outcome_status": "AMBIGUOUS",
        "ambiguous": True,
        "total_trade_pnl": -300.0,
        "profit_percent": -300.0,
        "exit_date": "2024-01-06T10:00:00",
    }
    fake_db = type(
        "FakeDb",
        (),
        {
            "paper_trades": FakeCollection(
                [
                    {"symbol": "WAIT", "paper_only": True, "status": "WAITING_FOR_ENTRY", "paper_pnl": 999.0},
                    {"symbol": "ACTIVE", "paper_only": True, "status": "ACTIVE", "paper_pnl": 777.0},
                    {"symbol": "AMB", "paper_only": True, "status": "AMBIGUOUS", "outcome_status": "AMBIGUOUS"},
                ]
            ),
            "trade_journal": FakeCollection([journal_win, old_ambiguous_profit, old_ambiguous_loss]),
        },
    )()
    monkeypatch.setattr(dashboard, "get_database", lambda: fake_db)

    result = asyncio.run(dashboard.get_paper_equity())

    assert result["realized_pnl"] == 300.0
    assert result["current_virtual_balance"] == 250300.0
    assert result["monthly_pnl_rows"] == [
        {"month": "2024-01", "pnl": 200.0},
        {"month": "2026-06", "pnl": 100.0},
    ]
    assert result["ambiguous_count"] == 1
    assert result["win_rate_percent"] == 100.0
    assert result["profit_factor"] == 100.0


def test_dashboard_has_no_stale_virtual_balance_defaults() -> None:
    source = Path(dashboard.__file__).read_text()
    stale_balances = (
        dashboard.MINIMUM_TRADE_CAPITAL * 20,
        dashboard.MINIMUM_TRADE_CAPITAL * 10,
    )

    assert "STARTING_VIRTUAL_BALANCE = 250000.0" in source
    for stale_balance in stale_balances:
        assert f"STARTING_VIRTUAL_BALANCE = {stale_balance:.1f}" not in source
