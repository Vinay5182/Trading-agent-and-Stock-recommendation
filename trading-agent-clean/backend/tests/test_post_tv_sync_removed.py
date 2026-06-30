import asyncio
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes import momentum, paper, swing
from security.operator_intent import OPERATOR_INTENT_HEADER, OPERATOR_INTENT_VALUE


def setup_function(_function) -> None:
    swing.tradingview_manager.detach_target()
    momentum.tradingview_manager.detach_target()


def sample_candidate(symbol: str = "TEST") -> dict:
    return {
        "exchange": "NSE",
        "symbol": symbol,
        "canonical_symbol": symbol,
        "tradingview_symbol": f"NSE:{symbol}",
        "current_price": 100.0,
        "selected_for_tv": True,
        "momentum_candidate": True,
        "momentum_status": "MOMENTUM_PRECHECK_PASSED",
        "momentum_score": 80,
        "index_name": "BROAD_MARKET_750",
    }


class FakeUpdateCollection:
    def __init__(self) -> None:
        self.calls = []

    async def update_one(self, identity, update, upsert=False):
        self.calls.append({"identity": identity, "update": update, "upsert": upsert})


class FakeSaveDb:
    def __init__(self) -> None:
        self.swing_tv_confirmations = FakeUpdateCollection()
        self.momentum_tv_confirmations = FakeUpdateCollection()


def test_swing_tv_completion_does_not_run_paper_sync(monkeypatch) -> None:
    saved_rows = []

    async def fake_resolve_tv_limit(index_name, requested_limit, offset=0):
        return 1, 1, None

    async def fake_load_candidates(index_name, limit, offset=0):
        return [sample_candidate("SWINGTEST")]

    async def fake_run_sync(*_args, **_kwargs):
        return {
            "tv_status": "CONFIRMED_SIGNAL",
            "reason": "confirmed",
            "requested_tradingview_symbol": "NSE:SWINGTEST",
        }

    async def fake_save(row):
        saved_rows.append(row)

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("TV completion must not trigger Trade Ready sync")

    monkeypatch.setattr(swing, "resolve_tv_limit", fake_resolve_tv_limit)
    monkeypatch.setattr(swing, "load_swing_tv_candidate_rows", fake_load_candidates)
    monkeypatch.setattr(swing.tradingview_manager, "run_sync", fake_run_sync)
    monkeypatch.setattr(swing, "save_confirmation_row", fake_save)
    monkeypatch.setattr(swing, "sync_trade_ready", fail_if_called, raising=False)

    result = asyncio.run(
        swing.run_swing_tv_confirmation(
            "BROAD_MARKET_750",
            1,
            ["1D"],
            save=True,
        )
    )

    assert result["processed"] == 1
    assert result["saved"] is True
    assert "paper_sync" not in result
    assert len(saved_rows) == 1


def test_momentum_tv_completion_does_not_run_paper_sync(monkeypatch) -> None:
    saved_rows = []

    async def fake_staleness(*_args, **_kwargs):
        return {
            "market_data_latest_updated_at": None,
            "scored_candidates_latest_updated_at": None,
            "is_score_stale": False,
        }

    async def fake_resolve_tv_limit(index_name, requested_limit, requested_offset=0):
        return 1, 1, None

    async def fake_load_candidates(index_name, limit, offset=0):
        return [sample_candidate("MOMTEST")]

    async def fake_run_sync(*_args, **_kwargs):
        return {
            "tv_status": "MOMENTUM_CONFIRMED",
            "reason": "confirmed",
            "requested_tradingview_symbol": "NSE:MOMTEST",
        }

    async def fake_save(row):
        saved_rows.append(row)

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("TV completion must not trigger Trade Ready sync")

    monkeypatch.setattr(momentum, "get_database", lambda: object())
    monkeypatch.setattr(momentum, "score_staleness_for_query", fake_staleness)
    monkeypatch.setattr(momentum, "resolve_tv_limit", fake_resolve_tv_limit)
    monkeypatch.setattr(momentum, "load_momentum_tv_candidate_rows", fake_load_candidates)
    monkeypatch.setattr(momentum.tradingview_manager, "run_sync", fake_run_sync)
    monkeypatch.setattr(momentum, "save_momentum_confirmation_row", fake_save)
    monkeypatch.setattr(momentum, "sync_trade_ready", fail_if_called, raising=False)

    result = asyncio.run(
        momentum.run_momentum_tv_confirmation(
            "BROAD_MARKET_750",
            1,
            ["1D"],
            save=True,
        )
    )

    assert result["processed"] == 1
    assert result["saved"] is True
    assert "paper_sync" not in result
    assert len(saved_rows) == 1


def test_swing_save_false_exception_does_not_record_system_error(monkeypatch) -> None:
    recorded_errors = []

    async def fake_resolve_tv_limit(index_name, requested_limit, offset=0):
        return 1, 1, None

    async def fake_load_candidates(index_name, limit, offset=0):
        return [sample_candidate("SWINGERR")]

    async def fake_run_sync(*_args, **_kwargs):
        if _args and _args[0] == "tv.validate_preflight":
            return None
        raise TimeoutError("manager timeout")

    async def fake_record_system_error(*_args, **kwargs):
        recorded_errors.append(kwargs)

    monkeypatch.setattr(swing, "resolve_tv_limit", fake_resolve_tv_limit)
    monkeypatch.setattr(swing, "load_swing_tv_candidate_rows", fake_load_candidates)
    monkeypatch.setattr(swing.tradingview_manager, "run_sync", fake_run_sync)
    monkeypatch.setattr(swing, "record_system_error", fake_record_system_error)

    result = asyncio.run(
        swing.run_swing_tv_confirmation(
            "BROAD_MARKET_750",
            1,
            ["1D"],
            save=False,
        )
    )

    assert result["processed"] == 1
    assert result["saved"] is False
    assert result["rows"][0]["reason"] == "TV_CONFIRMATION_EXCEPTION"
    assert recorded_errors == []


def test_momentum_save_false_exception_does_not_record_system_error(monkeypatch) -> None:
    recorded_errors = []

    async def fake_staleness(*_args, **_kwargs):
        return {
            "market_data_latest_updated_at": None,
            "scored_candidates_latest_updated_at": None,
            "is_score_stale": False,
        }

    async def fake_resolve_tv_limit(index_name, requested_limit, requested_offset=0):
        return 1, 1, None

    async def fake_load_candidates(index_name, limit, offset=0):
        return [sample_candidate("MOMERR")]

    async def fake_run_sync(*_args, **_kwargs):
        if _args and _args[0] == "tv.validate_preflight":
            return None
        raise TimeoutError("manager timeout")

    async def fake_record_system_error(*_args, **kwargs):
        recorded_errors.append(kwargs)

    monkeypatch.setattr(momentum, "get_database", lambda: object())
    monkeypatch.setattr(momentum, "score_staleness_for_query", fake_staleness)
    monkeypatch.setattr(momentum, "resolve_tv_limit", fake_resolve_tv_limit)
    monkeypatch.setattr(momentum, "load_momentum_tv_candidate_rows", fake_load_candidates)
    monkeypatch.setattr(momentum.tradingview_manager, "run_sync", fake_run_sync)
    monkeypatch.setattr(momentum, "record_system_error", fake_record_system_error)

    result = asyncio.run(
        momentum.run_momentum_tv_confirmation(
            "BROAD_MARKET_750",
            1,
            ["1D"],
            save=False,
        )
    )

    assert result["processed"] == 1
    assert result["saved"] is False
    assert result["rows"][0]["reason"] == "TV_CONFIRMATION_EXCEPTION"
    assert recorded_errors == []


def test_swing_confirmation_passes_attached_target_to_worker(monkeypatch) -> None:
    captured = {}

    async def fake_resolve_tv_limit(index_name, requested_limit, offset=0):
        return 1, 1, None

    async def fake_load_candidates(index_name, limit, offset=0):
        return [sample_candidate("SWINGATTACH")]

    async def fake_run_sync(*args, **_kwargs):
        if args and args[0] == "tv.validate_preflight":
            return None
        captured["args"] = args
        return {
            "tv_status": "TECHNICAL_FAILED",
            "reason": "TEST_DONE",
            "requested_tradingview_symbol": "NSE:SWINGATTACH",
            "managed_tab_id": args[5],
        }

    monkeypatch.setattr(swing, "resolve_tv_limit", fake_resolve_tv_limit)
    monkeypatch.setattr(swing, "load_swing_tv_candidate_rows", fake_load_candidates)
    monkeypatch.setattr(swing.tradingview_manager, "run_sync", fake_run_sync)
    swing.tradingview_manager.attach_target({"target_id": "attached-chart", "title": "TradingView", "url": "https://in.tradingview.com/chart/ThW59K6v/", "websocket_debugger_url": "ws://attached-chart"})
    try:
        result = asyncio.run(swing.run_swing_tv_confirmation("BROAD_MARKET_750", 1, ["1D"], save=False))
    finally:
        swing.tradingview_manager.detach_target()

    assert result["processed"] == 1
    assert captured["args"][5] == "attached-chart"
    assert result["rows"][0]["managed_tab_id"] == "attached-chart"


def test_momentum_confirmation_passes_attached_target_to_worker(monkeypatch) -> None:
    captured = {}

    async def fake_staleness(*_args, **_kwargs):
        return {
            "market_data_latest_updated_at": None,
            "scored_candidates_latest_updated_at": None,
            "is_score_stale": False,
        }

    async def fake_resolve_tv_limit(index_name, requested_limit, requested_offset=0):
        return 1, 1, None

    async def fake_load_candidates(index_name, limit, offset=0):
        return [sample_candidate("MOMATTACH")]

    async def fake_run_sync(*args, **_kwargs):
        if args and args[0] == "tv.validate_preflight":
            return None
        captured["args"] = args
        return {
            "tv_status": "TECHNICAL_FAILED",
            "reason": "TEST_DONE",
            "requested_tradingview_symbol": "NSE:MOMATTACH",
            "managed_tab_id": args[5],
        }

    monkeypatch.setattr(momentum, "get_database", lambda: object())
    monkeypatch.setattr(momentum, "score_staleness_for_query", fake_staleness)
    monkeypatch.setattr(momentum, "resolve_tv_limit", fake_resolve_tv_limit)
    monkeypatch.setattr(momentum, "load_momentum_tv_candidate_rows", fake_load_candidates)
    monkeypatch.setattr(momentum.tradingview_manager, "run_sync", fake_run_sync)
    momentum.tradingview_manager.attach_target({"target_id": "attached-chart", "title": "TradingView", "url": "https://in.tradingview.com/chart/ThW59K6v/", "websocket_debugger_url": "ws://attached-chart"})
    try:
        result = asyncio.run(momentum.run_momentum_tv_confirmation("BROAD_MARKET_750", 1, ["1D"], save=False, force_use_stale_scores=False))
    finally:
        momentum.tradingview_manager.detach_target()

    assert result["processed"] == 1
    assert captured["args"][5] == "attached-chart"
    assert result["rows"][0]["managed_tab_id"] == "attached-chart"


def test_swing_save_preserves_failure_history_and_dedupes_confirmations(monkeypatch) -> None:
    fake_db = FakeSaveDb()
    monkeypatch.setattr(swing, "get_database", lambda: fake_db)

    base_row = {
        "symbol": "KEEPFAIL",
        "tradingview_symbol": "NSE:KEEPFAIL",
        "index_name": "BROAD_MARKET_750",
        "timeframes_hash": "hash",
    }

    asyncio.run(swing.save_confirmation_row({**base_row, "tv_status": "TECHNICAL_FAILED", "reason": "SYMBOL_MISMATCH"}))
    asyncio.run(swing.save_confirmation_row({**base_row, "tv_status": "CONFIRMED_SIGNAL", "reason": "OK"}))

    failure_identity = fake_db.swing_tv_confirmations.calls[0]["identity"]
    success_identity = fake_db.swing_tv_confirmations.calls[1]["identity"]

    assert failure_identity["tv_status"] == "TECHNICAL_FAILED"
    assert failure_identity["failure_run_id"]
    assert success_identity["tv_status"] == {"$ne": "TECHNICAL_FAILED"}


def test_sync_trade_ready_endpoint_returns_json_cors_on_service_error(monkeypatch) -> None:
    recorded_errors = []

    async def fail_sync(*_args, **_kwargs):
        raise RuntimeError("sync exploded")

    async def fake_record_system_error(*_args, **kwargs):
        recorded_errors.append(kwargs)

    monkeypatch.setattr(paper, "sync_trade_ready", fail_sync)
    monkeypatch.setattr(paper, "get_database", lambda: object())
    monkeypatch.setattr(paper, "record_system_error", fake_record_system_error)

    test_app = FastAPI()
    test_app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    test_app.include_router(paper.router, prefix="/api/paper")

    with TestClient(test_app) as client:
        response = client.post(
            "/api/paper/sync-trade-ready",
            headers={"Origin": "http://127.0.0.1:5173", OPERATOR_INTENT_HEADER: OPERATOR_INTENT_VALUE},
        )

    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert response.json() == {
        "ok": False,
        "error": "SYNC_TRADE_READY_FAILED",
        "message": "Trade Ready paper sync failed. Automatic scheduler will retry.",
        "paper_only": True,
        "live_trading": False,
        "broker_orders": False,
    }
    assert recorded_errors[0]["component"] == "paper_sync"
    assert recorded_errors[0]["operation"] == "manual_sync_trade_ready_endpoint"
