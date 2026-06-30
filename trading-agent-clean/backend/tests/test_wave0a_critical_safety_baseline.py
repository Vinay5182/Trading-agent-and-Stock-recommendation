import asyncio
import inspect
import threading
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import app
from routes import ai as ai_routes
from routes import dashboard, market, momentum, paper, scan, score, signals, swing, tv
from security.operator_intent import OPERATOR_INTENT_ERROR, OPERATOR_INTENT_HEADER, OPERATOR_INTENT_VALUE
from services import trade_journal as trade_journal_service
from services.tradingview_manager import TradingViewExecutionManager, TradingViewPreflightError


S12_UNTRUSTED_MUTATION = "S12 blocker RC1 unauthenticated mutation surface"
S12_READ_ROUTE_WRITES = "S12 blocker RC3 hidden/manual paper writes"
S12_TRADINGVIEW = "S12 blocker RC2 TradingView state mutation and CDP interference"
S12_MIGRATION_INDEX = "S12 blocker RC4 migration/apply and index safety gaps"
OPERATOR_HEADERS = {OPERATOR_INTENT_HEADER: OPERATOR_INTENT_VALUE}


class OperationStarted(AssertionError):
    pass


class WriteAttempt(AssertionError):
    pass


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.index = 0

    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, limit):
        self.rows = self.rows[:limit]
        return self

    def skip(self, skip):
        self.rows = self.rows[skip:]
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self.index]
        self.index += 1
        return dict(row)


class ReadOnlyCollection:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.write_calls = []
        self.find_calls = []

    def find(self, query=None, projection=None, *_args, **_kwargs):
        self.find_calls.append((query or {}, projection or {}))
        return FakeCursor(self.rows)

    async def find_one(self, query=None, *_args, **_kwargs):
        self.find_calls.append((query or {}, {}))
        return dict(self.rows[0]) if self.rows else None

    async def count_documents(self, query=None):
        self.find_calls.append((query or {}, {"count": True}))
        return len(self.rows)

    def aggregate(self, *_args, **_kwargs):
        return FakeCursor([])

    async def insert_one(self, *args, **kwargs):
        self.write_calls.append(("insert_one", args, kwargs))
        raise WriteAttempt("read route attempted insert_one")

    async def update_one(self, *args, **kwargs):
        self.write_calls.append(("update_one", args, kwargs))
        raise WriteAttempt("read route attempted update_one")

    async def update_many(self, *args, **kwargs):
        self.write_calls.append(("update_many", args, kwargs))
        raise WriteAttempt("read route attempted update_many")

    async def delete_one(self, *args, **kwargs):
        self.write_calls.append(("delete_one", args, kwargs))
        raise WriteAttempt("read route attempted delete_one")

    async def delete_many(self, *args, **kwargs):
        self.write_calls.append(("delete_many", args, kwargs))
        raise WriteAttempt("read route attempted delete_many")

    async def replace_one(self, *args, **kwargs):
        self.write_calls.append(("replace_one", args, kwargs))
        raise WriteAttempt("read route attempted replace_one")

    async def bulk_write(self, *args, **kwargs):
        self.write_calls.append(("bulk_write", args, kwargs))
        raise WriteAttempt("read route attempted bulk_write")

    async def create_index(self, *args, **kwargs):
        self.write_calls.append(("create_index", args, kwargs))
        raise WriteAttempt("read route attempted create_index")


class ExplodingDB:
    name = "wave0a_isolated_fake_db"

    def __getattr__(self, item):
        raise OperationStarted(f"database access reached before trusted operator intent: {item}")


def isolated_read_db():
    db = SimpleNamespace(
        name="wave0a_isolated_fake_db",
        paper_trades=ReadOnlyCollection(
            [
                {
                    "_id": "trade-1",
                    "paper_only": True,
                    "symbol": "TEST",
                    "status": "ACTIVE",
                    "outcome_status": "ACTIVE",
                    "entry_triggered": True,
                    "updated_at": "2026-01-01T00:00:00",
                }
            ]
        ),
        trade_journal=ReadOnlyCollection([]),
        ai_feature_snapshots=ReadOnlyCollection(
            [
                {
                    "_id": "snap-1",
                    "paper_only": True,
                    "symbol": "TEST",
                    "strategy_type": "swing",
                    "timeframe": "1D",
                    "snapshot_time": "2026-01-01T00:00:00",
                    "paper_trade_id": "trade-1",
                }
            ]
        ),
        paper_signals=ReadOnlyCollection([]),
        paper_update_runs=ReadOnlyCollection([]),
        paper_update_locks=ReadOnlyCollection([]),
        paper_market_snapshots=ReadOnlyCollection([]),
        scheduler_status=ReadOnlyCollection([]),
        system_errors=ReadOnlyCollection([]),
    )
    assert db.name != "trading_agent_clean"
    return db


def assert_no_writes(db):
    for value in vars(db).values():
        if isinstance(value, ReadOnlyCollection):
            assert value.write_calls == []


def assert_rejected_with_stable_error(response):
    assert response.status_code == 403
    assert response.json() == OPERATOR_INTENT_ERROR


def test_untrusted_market_load_all_rejected_before_work(monkeypatch):
    monkeypatch.setattr(market, "get_database", lambda: ExplodingDB())
    monkeypatch.setattr(market, "fetch_load_all_nse_batch", lambda *_args, **_kwargs: (_ for _ in ()).throw(OperationStarted("NSE fetch reached")))
    response = TestClient(app, raise_server_exceptions=False).post("/api/market/load-all?dry_run=false")
    assert_rejected_with_stable_error(response)


def test_untrusted_market_cleanup_delete_rejected_before_work(monkeypatch):
    monkeypatch.setattr(market, "get_database", lambda: ExplodingDB())
    response = TestClient(app, raise_server_exceptions=False).post("/api/market/cleanup-invalid-symbols?dry_run=false")
    assert_rejected_with_stable_error(response)


def test_untrusted_score_run_rejected_before_work(monkeypatch):
    monkeypatch.setattr(score, "get_database", lambda: ExplodingDB())
    response = TestClient(app, raise_server_exceptions=False).post("/api/score/run?dry_run=false")
    assert_rejected_with_stable_error(response)


def test_untrusted_swing_save_true_rejected_before_tv_worker(monkeypatch):
    async def fail_worker(*_args, **_kwargs):
        raise OperationStarted("Swing TV worker reached")

    monkeypatch.setattr(swing, "run_swing_tv_confirmation", fail_worker)
    response = TestClient(app, raise_server_exceptions=False).post("/api/swing/tv-confirm?save=true&limit=1")
    assert_rejected_with_stable_error(response)


def test_untrusted_momentum_save_true_rejected_before_tv_worker(monkeypatch):
    async def fail_worker(*_args, **_kwargs):
        raise OperationStarted("Momentum TV worker reached")

    monkeypatch.setattr(momentum, "run_momentum_tv_confirmation", fail_worker)
    response = TestClient(app, raise_server_exceptions=False).post("/api/momentum/tv-confirm?save=true&limit=1")
    assert_rejected_with_stable_error(response)


@pytest.mark.parametrize(
    "path",
    [
        "/api/market/load-index?dry_run=false&limit=1",
        "/api/market/load-all-batches?dry_run=false&batch_size=1&max_batches=1",
    ],
)
def test_untrusted_market_batch_routes_reject_before_work(monkeypatch, path):
    monkeypatch.setattr(market, "get_database", lambda: ExplodingDB())
    response = TestClient(app, raise_server_exceptions=False).post(path)
    assert_rejected_with_stable_error(response)


def test_untrusted_scan_rejected_before_work(monkeypatch):
    monkeypatch.setattr(scan, "get_database", lambda: ExplodingDB())
    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/scan",
        json={"selected_index": "DEFAULT_UNIVERSE", "limit": 1, "force_refresh": False, "dry_run": False},
    )
    assert_rejected_with_stable_error(response)


@pytest.mark.parametrize(
    "path",
    [
        "/api/signals/build-tv-confirmed?save=true&limit=1",
        "/api/signals/build-momentum-tv-confirmed?save=true&limit=1",
    ],
)
def test_untrusted_signal_save_true_routes_reject_before_work(monkeypatch, path):
    monkeypatch.setattr(signals, "get_database", lambda: ExplodingDB())
    response = TestClient(app, raise_server_exceptions=False).post(path)
    assert_rejected_with_stable_error(response)


@pytest.mark.parametrize("path", ["/api/tv/test-symbol", "/api/tv/attach-tab?target_id=chart-1", "/api/tv/detach-tab"])
def test_untrusted_tv_mutating_routes_reject_before_manager_work(monkeypatch, path):
    class FailManager:
        async def run_sync(self, *_args, **_kwargs):
            raise OperationStarted("TradingView manager run_sync reached")

        def detach_target(self):
            raise OperationStarted("TradingView manager detach reached")

    monkeypatch.setattr(tv, "tradingview_manager", FailManager())
    response = TestClient(app, raise_server_exceptions=False).post(
        path,
        json={"symbol": "TEST", "timeframe": "1D", "fetch_candles": False},
    )
    assert_rejected_with_stable_error(response)


@pytest.mark.parametrize(
    ("path", "patches"),
    [
        ("/api/paper/build-plans?save=true&limit=1", ("paper_db",)),
        ("/api/paper/run-pipeline?dry_run=false&limit=1", ("paper_db",)),
        ("/api/paper/journal/sync", ("paper_sync_journal",)),
        ("/api/paper/sync-trade-ready?dry_run=false", ("paper_sync_trade_ready",)),
        ("/api/paper/auto-update-outcomes?dry_run=false", ("paper_auto_outcome",)),
        ("/api/paper/audit-waiting?apply=true", ("paper_audit_waiting",)),
        ("/api/ai/features/save?dry_run=false&limit=1", ("ai_db",)),
        ("/api/ai/features/attach-outcomes?dry_run=false&limit=1", ("ai_db",)),
    ],
)
def test_untrusted_mutating_routes_reject_before_work(monkeypatch, path, patches):
    if "paper_db" in patches:
        monkeypatch.setattr(paper, "get_database", lambda: ExplodingDB())
    if "ai_db" in patches:
        monkeypatch.setattr(ai_routes, "get_database", lambda: ExplodingDB())
    if "paper_sync_journal" in patches:
        async def fail_journal(*_args, **_kwargs):
            raise OperationStarted("journal sync reached")
        monkeypatch.setattr(paper, "sync_completed_trades_to_journal", fail_journal)
    if "paper_sync_trade_ready" in patches:
        async def fail_sync(*_args, **_kwargs):
            raise OperationStarted("manual sync-trade-ready reached")
        monkeypatch.setattr(paper, "sync_trade_ready", fail_sync)
    if "paper_auto_outcome" in patches:
        async def fail_auto(*_args, **_kwargs):
            raise OperationStarted("auto-update-outcomes reached")
        monkeypatch.setattr(paper, "run_automatic_outcome_update", fail_auto)
    if "paper_audit_waiting" in patches:
        async def fail_audit(*_args, **_kwargs):
            raise OperationStarted("audit-waiting apply reached")
        monkeypatch.setattr(paper, "audit_and_fix_waiting_trades", fail_audit)

    response = TestClient(app, raise_server_exceptions=False).post(path)
    assert_rejected_with_stable_error(response)


@pytest.mark.parametrize("header_value", ["", "operator-write-v1 ", "OPERATOR-WRITE-V1", "operator-write-v2"])
def test_incorrect_operator_intent_header_rejected_exactly(monkeypatch, header_value):
    monkeypatch.setattr(score, "get_database", lambda: ExplodingDB())
    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/score/run?dry_run=false",
        headers={OPERATOR_INTENT_HEADER: header_value},
    )
    assert_rejected_with_stable_error(response)


def test_valid_operator_intent_header_reaches_route_service(monkeypatch):
    calls = {}

    async def fake_sync(_db, limit):
        calls["limit"] = limit
        return {"synced_count": 0}

    monkeypatch.setattr(paper, "get_database", lambda: SimpleNamespace(name="wave0a_fake_db"))
    monkeypatch.setattr(paper, "sync_completed_trades_to_journal", fake_sync)
    response = TestClient(app).post("/api/paper/journal/sync?limit=7", headers=OPERATOR_HEADERS)
    assert response.status_code == 200
    assert calls == {"limit": 7}
    assert response.json()["paper_only"] is True


def test_market_dry_run_still_available_without_operator_intent(monkeypatch):
    monkeypatch.setattr(market, "get_database", lambda: ExplodingDB())
    response = TestClient(app).post("/api/market/load-all?dry_run=true")
    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["mongo_writes"] is False


def test_swing_save_false_still_available_without_operator_intent(monkeypatch):
    calls = {}

    async def fake_worker(index_name, limit, timeframes, save, *_args, **_kwargs):
        calls["save"] = save
        return {"index_name": index_name, "limit": limit, "timeframes": timeframes, "saved": save, "rows": []}

    monkeypatch.setattr(swing, "run_swing_tv_confirmation", fake_worker)
    response = TestClient(app).post("/api/swing/tv-confirm?save=false&limit=1")
    assert response.status_code == 200
    assert calls == {"save": False}
    assert response.json()["saved"] is False


def test_internal_service_entrypoints_do_not_require_http_operator_header():
    service_entrypoints = [
        swing.run_swing_tv_confirmation,
        momentum.run_momentum_tv_confirmation,
        paper.run_paper_trade_update,
        paper.audit_and_fix_waiting_trades,
    ]
    for entrypoint in service_entrypoints:
        assert "operator_intent" not in inspect.signature(entrypoint).parameters


def test_paper_trade_read_endpoints_do_not_write(monkeypatch):
    db = isolated_read_db()
    monkeypatch.setattr(paper, "get_database", lambda: db)
    client = TestClient(app)

    for path in ("/api/paper/open", "/api/paper/history", "/api/paper/summary", "/api/paper/active", "/api/paper/trades"):
        response = client.get(path)
        assert response.status_code == 200

    assert_no_writes(db)


def test_dashboard_trade_analytics_does_not_sync_or_write(monkeypatch):
    db = isolated_read_db()
    monkeypatch.setattr(dashboard, "get_database", lambda: db)
    response = TestClient(app).get("/api/dashboard/trade-analytics")
    assert response.status_code == 200
    assert_no_writes(db)


def test_ai_read_endpoints_do_not_write(monkeypatch):
    db = isolated_read_db()
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    client = TestClient(app)

    for path in (
        "/api/ai/features/summary",
        "/api/ai/features/snapshots",
        "/api/ai/features/collection-status",
        "/api/ai/features/outcome-preview",
    ):
        response = client.get(path)
        assert response.status_code == 200

    assert_no_writes(db)


def test_paper_journal_get_default_does_not_implicitly_sync(monkeypatch):
    db = isolated_read_db()
    monkeypatch.setattr(paper, "get_database", lambda: db)
    calls = {"sync": 0}

    async def fail_sync(*_args, **_kwargs):
        calls["sync"] += 1
        raise WriteAttempt("journal GET attempted implicit sync")

    monkeypatch.setattr(paper, "sync_completed_trades_to_journal", fail_sync)
    response = TestClient(app, raise_server_exceptions=False).get("/api/paper/journal")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 0
    assert payload["journal"] == []
    assert payload.get("sync_result") is None
    assert calls["sync"] == 0
    assert_no_writes(db)


def test_paper_analytics_get_default_does_not_implicitly_sync(monkeypatch):
    db = isolated_read_db()
    monkeypatch.setattr(paper, "get_database", lambda: db)
    calls = {"sync": 0}

    async def fail_sync(*_args, **_kwargs):
        calls["sync"] += 1
        raise WriteAttempt("analytics GET attempted implicit sync")

    monkeypatch.setattr(paper, "sync_completed_trades_to_journal", fail_sync)
    monkeypatch.setattr(trade_journal_service, "sync_completed_trades_to_journal", fail_sync)
    response = TestClient(app, raise_server_exceptions=False).get("/api/paper/analytics")
    assert response.status_code == 200
    payload = response.json()
    assert payload["journal_count"] == 0
    assert payload["journal"] == []
    assert payload["sync_result"] is None
    assert payload["analytics"]["total_trades"] == 0
    assert calls["sync"] == 0
    assert_no_writes(db)


@pytest.mark.parametrize("path", ["/api/paper/journal?sync_missing=false", "/api/paper/analytics?sync_missing=false"])
def test_paper_read_routes_sync_missing_false_remains_read_only(monkeypatch, path):
    db = isolated_read_db()
    monkeypatch.setattr(paper, "get_database", lambda: db)

    async def fail_sync(*_args, **_kwargs):
        raise WriteAttempt("sync_missing=false attempted journal sync")

    monkeypatch.setattr(paper, "sync_completed_trades_to_journal", fail_sync)
    monkeypatch.setattr(trade_journal_service, "sync_completed_trades_to_journal", fail_sync)
    response = TestClient(app, raise_server_exceptions=False).get(path)
    assert response.status_code == 200
    assert_no_writes(db)


@pytest.mark.parametrize("path", ["/api/paper/journal?sync_missing=true", "/api/paper/analytics?sync_missing=true"])
def test_paper_read_routes_sync_missing_true_rejected_without_writes(monkeypatch, path):
    db = isolated_read_db()
    monkeypatch.setattr(paper, "get_database", lambda: db)

    async def fail_sync(*_args, **_kwargs):
        raise WriteAttempt("sync_missing=true attempted journal sync")

    monkeypatch.setattr(paper, "sync_completed_trades_to_journal", fail_sync)
    monkeypatch.setattr(trade_journal_service, "sync_completed_trades_to_journal", fail_sync)
    response = TestClient(app, raise_server_exceptions=False).get(path)
    assert response.status_code == 400
    assert response.json() == paper.READ_ROUTE_WRITE_NOT_ALLOWED_ERROR
    assert_no_writes(db)


def test_paper_journal_get_reads_existing_rows_with_limit_without_intent(monkeypatch):
    db = isolated_read_db()
    db.trade_journal = ReadOnlyCollection(
        [
            {"paper_trade_id": "journal-1", "symbol": "WIN", "exit_date": "2026-01-02T00:00:00"},
            {"paper_trade_id": "journal-2", "symbol": "LOSS", "exit_date": "2026-01-01T00:00:00"},
        ]
    )
    monkeypatch.setattr(paper, "get_database", lambda: db)
    response = TestClient(app).get("/api/paper/journal?limit=1")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["journal"][0]["symbol"] == "WIN"
    assert payload["sync_result"] is None
    assert_no_writes(db)


def test_paper_analytics_uses_existing_journal_records_and_excludes_ambiguous(monkeypatch):
    db = isolated_read_db()
    db.trade_journal = ReadOnlyCollection(
        [
            {
                "paper_trade_id": "win",
                "symbol": "WIN",
                "strategy_type": "Swing",
                "grade": "A+",
                "trap_status": "CLEAN",
                "profit_percent": 30.0,
                "total_trade_pnl": 300.0,
                "RR": 4.0,
                "exit_date": "2026-06-05T15:30:00",
            },
            {
                "paper_trade_id": "loss",
                "symbol": "LOSS",
                "strategy_type": "Momentum",
                "grade": "B",
                "trap_status": "DANGER",
                "profit_percent": -10.0,
                "total_trade_pnl": -100.0,
                "RR": -1.0,
                "exit_date": "2026-06-20T15:30:00",
            },
            {
                "paper_trade_id": "amb",
                "symbol": "AMB",
                "strategy_type": "Swing",
                "profit_percent": 999.0,
                "total_trade_pnl": 9999.0,
                "exit_reason": "AMBIGUOUS",
                "exit_date": "2026-06-21T15:30:00",
                "ambiguous": True,
            },
        ]
    )
    monkeypatch.setattr(paper, "get_database", lambda: db)
    response = TestClient(app).get("/api/paper/analytics")
    assert response.status_code == 200
    payload = response.json()
    analytics = payload["analytics"]
    assert payload["journal_count"] == 3
    assert payload["sync_result"] is None
    assert analytics["total_trades"] == 2
    assert analytics["ambiguous_count"] == 1
    assert analytics["win_rate"] == 50.0
    assert analytics["profit_factor"] == 3.0
    assert analytics["monthly_pnl"] == {"2026-06": 200.0}
    assert_no_writes(db)


def test_post_paper_journal_sync_without_intent_rejects_before_service(monkeypatch):
    calls = {"sync": 0}

    async def fail_sync(*_args, **_kwargs):
        calls["sync"] += 1
        raise OperationStarted("journal sync reached without operator intent")

    monkeypatch.setattr(paper, "sync_completed_trades_to_journal", fail_sync)
    response = TestClient(app, raise_server_exceptions=False).post("/api/paper/journal/sync")
    assert_rejected_with_stable_error(response)
    assert calls["sync"] == 0


def test_attachable_tabs_is_discovery_only_with_no_preference_mutation(monkeypatch, tmp_path):
    preference_path = tmp_path / "tv-pref.json"
    manager = TradingViewExecutionManager(preference_path=preference_path)
    calls = {"attach": 0, "detach": 0}

    class FakeClient:
        diagnostics = {"fake": True}

        def connect_to_debug_port(self):
            return True

        def list_attachable_chart_targets(self):
            return [{"target_id": "chart-1", "title": "TradingView", "url": "https://in.tradingview.com/chart/x", "ready": True}]

    def fail_attach(*_args, **_kwargs):
        calls["attach"] += 1
        raise OperationStarted("attach_target reached during discovery")

    def fail_detach(*_args, **_kwargs):
        calls["detach"] += 1
        raise OperationStarted("detach_target reached during discovery")

    monkeypatch.setattr(manager, "get_client", lambda require_attached_tab=False: FakeClient())
    monkeypatch.setattr(manager, "attach_target", fail_attach)
    monkeypatch.setattr(manager, "detach_target", fail_detach)
    monkeypatch.setattr(tv, "tradingview_manager", manager)

    response = TestClient(app, raise_server_exceptions=False).get("/api/tv/attachable-tabs")
    assert response.status_code == 200
    body = response.json()
    assert calls["attach"] == 0
    assert calls["detach"] == 0
    assert body["count"] == 1
    assert body["auto_attached"] is False
    assert body["attached_target_id"] is None
    assert body["manual_attachment_required"] is True
    assert not preference_path.exists()
    assert manager.attached_target_snapshot() is None


@pytest.mark.parametrize("targets", [
    [],
    [
        {"target_id": "chart-1", "title": "TradingView 1", "url": "https://in.tradingview.com/chart/1", "ready": True},
        {"target_id": "chart-2", "title": "TradingView 2", "url": "https://in.tradingview.com/chart/2", "ready": True},
    ],
])
def test_attachable_tabs_observes_zero_and_multiple_targets_without_selection(monkeypatch, tmp_path, targets):
    preference_path = tmp_path / "tv-pref.json"
    manager = TradingViewExecutionManager(preference_path=preference_path)
    calls = {"attach": 0, "detach": 0}

    class FakeClient:
        diagnostics = {"fake": True}

        def connect_to_debug_port(self):
            return True

        def list_attachable_chart_targets(self):
            return list(targets)

    monkeypatch.setattr(manager, "get_client", lambda require_attached_tab=False: FakeClient())
    monkeypatch.setattr(manager, "attach_target", lambda *_args, **_kwargs: (_ for _ in ()).throw(OperationStarted("attach reached")))
    monkeypatch.setattr(manager, "detach_target", lambda *_args, **_kwargs: (_ for _ in ()).throw(OperationStarted("detach reached")))
    monkeypatch.setattr(tv, "tradingview_manager", manager)

    response = TestClient(app, raise_server_exceptions=False).get("/api/tv/attachable-tabs")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == len(targets)
    assert body["auto_attached"] is False
    assert body["attached_target_id"] is None
    assert body["manual_attachment_required"] is True
    assert calls["attach"] == 0
    assert calls["detach"] == 0
    assert not preference_path.exists()
    assert manager.attached_target_snapshot() is None


def test_attachable_tabs_preserves_stale_attachment_and_repeated_results(monkeypatch, tmp_path):
    preference_path = tmp_path / "tv-pref.json"
    manager = TradingViewExecutionManager(preference_path=preference_path)
    manager.attach_target({
        "target_id": "stale-chart",
        "title": "TradingView stale",
        "url": "https://in.tradingview.com/chart/stale",
        "ready": True,
    })
    preference_before = preference_path.read_bytes()
    calls = {"attach": 0, "detach": 0}

    class FakeClient:
        diagnostics = {"fake": True}

        def connect_to_debug_port(self):
            return True

        def list_attachable_chart_targets(self):
            return [{"target_id": "chart-1", "title": "TradingView", "url": "https://in.tradingview.com/chart/x", "ready": True}]

    def fail_attach(*_args, **_kwargs):
        calls["attach"] += 1
        raise OperationStarted("attach_target reached during discovery")

    def fail_detach(*_args, **_kwargs):
        calls["detach"] += 1
        raise OperationStarted("detach_target reached during discovery")

    monkeypatch.setattr(manager, "get_client", lambda require_attached_tab=False: FakeClient())
    monkeypatch.setattr(manager, "attach_target", fail_attach)
    monkeypatch.setattr(manager, "detach_target", fail_detach)
    monkeypatch.setattr(tv, "tradingview_manager", manager)
    client = TestClient(app, raise_server_exceptions=False)

    first = client.get("/api/tv/attachable-tabs")
    second = client.get("/api/tv/attachable-tabs")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    body = first.json()
    assert calls["attach"] == 0
    assert calls["detach"] == 0
    assert body["attached_target_id"] == "stale-chart"
    assert body["attached_target_stale"] is True
    assert body["manual_attachment_required"] is True
    assert manager.attached_target_snapshot()["target_id"] == "stale-chart"
    assert preference_path.read_bytes() == preference_before


def test_attachable_tabs_busy_response_does_not_probe_or_mutate(monkeypatch):
    calls = {"probe": 0}

    class BusyManager:
        async def run_read_only_inspection(self, *_args, **_kwargs):
            raise TradingViewPreflightError(
                "TV_MANAGER_BUSY",
                "TradingView Execution Manager is busy with another operation",
                {"code": "TV_MANAGER_BUSY", "message": "busy"},
            )

        def attached_target_snapshot(self):
            return {"target_id": "chart-1", "title": "TradingView", "url": "https://in.tradingview.com/chart/x"}

        def operation_status_snapshot(self):
            return {"worker_running": True, "queue_length": 1, "active_operation": "tv.confirm"}

        def get_client(self, *_args, **_kwargs):
            calls["probe"] += 1
            raise OperationStarted("client probe reached while manager busy")

    monkeypatch.setattr(tv, "tradingview_manager", BusyManager())
    response = TestClient(app, raise_server_exceptions=False).get("/api/tv/attachable-tabs")

    assert response.status_code == 200
    body = response.json()
    assert calls["probe"] == 0
    assert body["manager_busy"] is True
    assert body["target_listing_skipped"] is True
    assert body["attached_target_id"] == "chart-1"


def test_detach_uses_serialized_manager_ownership(monkeypatch):
    calls = {"run_sync": 0, "serialized_detach": 0, "direct_detach": 0}

    class FakeManager:
        async def run_sync(self, *_args, **_kwargs):
            calls["run_sync"] += 1
            return {"attached": False, "attached_target": None, "attached_target_id": None, "detached": True}

        async def detach_target_serialized(self):
            calls["serialized_detach"] += 1
            return await self.run_sync("tv.detach_tab", lambda: None)

        def detach_target(self):
            calls["direct_detach"] += 1
            return {"detached": True}

    monkeypatch.setattr(tv, "tradingview_manager", FakeManager())
    response = TestClient(app).post("/api/tv/detach-tab", headers=OPERATOR_HEADERS)
    assert response.status_code == 200
    assert calls["serialized_detach"] == 1
    assert calls["run_sync"] == 1
    assert calls["direct_detach"] == 0


def test_detach_target_serialized_is_idempotent_and_clears_temp_preference(monkeypatch, tmp_path):
    preference_path = tmp_path / "tv-pref.json"
    manager = TradingViewExecutionManager(preference_path=preference_path)
    manager.attach_target({"target_id": "chart-1", "title": "TradingView", "url": "https://in.tradingview.com/chart/x", "ready": True})
    observed = {}
    original_detach = manager._detach_target_unlocked

    def wrapped_detach():
        status = manager.operation_status_snapshot()
        observed["locked"] = status["worker_running"]
        observed["active_operation"] = status["active_operation"]
        original_detach()

    monkeypatch.setattr(manager, "_detach_target_unlocked", wrapped_detach)

    result = asyncio.run(manager.detach_target_serialized())
    idempotent_result = asyncio.run(manager.detach_target_serialized())

    assert observed == {"locked": True, "active_operation": "tv.detach_tab"}
    assert result["attached"] is False
    assert result["detached"] is True
    assert idempotent_result["attached"] is False
    assert idempotent_result["detached"] is False
    assert manager.attached_target_snapshot() is None
    assert not preference_path.exists()


def test_detach_target_serialized_failure_preserves_attachment_and_preference(monkeypatch, tmp_path):
    preference_path = tmp_path / "tv-pref.json"
    manager = TradingViewExecutionManager(preference_path=preference_path)
    manager.attach_target({"target_id": "chart-1", "title": "TradingView", "url": "https://in.tradingview.com/chart/x", "ready": True})
    preference_before = preference_path.read_bytes()

    def fail_detach():
        raise RuntimeError("detach failed before mutation")

    monkeypatch.setattr(manager, "_detach_target_unlocked", fail_detach)

    with pytest.raises(RuntimeError, match="detach failed"):
        asyncio.run(manager.detach_target_serialized())

    assert manager.attached_target_snapshot()["target_id"] == "chart-1"
    assert preference_path.read_bytes() == preference_before


def test_timed_out_worker_cannot_mutate_after_later_operation_starts(tmp_path):
    manager = TradingViewExecutionManager(preference_path=tmp_path / "tv-pref.json")
    manager._cached_preflight = {
        "preflight_ready": True,
        "preflight_code": "OK",
        "preflight_message": "fake ready",
        "cdp_reachable": True,
        "valid_chart_target_count": 1,
        "manual_attachment_required": False,
        "attached_target_ready": True,
        "last_attachment_error": None,
    }
    started = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    inspection_started = threading.Event()

    def late_attachment_worker():
        started.set()
        release.wait(2)
        manager.attach_target({
            "target_id": "stale-late-chart",
            "title": "Stale late TradingView",
            "url": "https://in.tradingview.com/chart/stale",
            "ready": True,
        })
        return {"ok": True, "diagnostics": {"managed_tab_id": "stale-late-chart", "managed_tab_count": 1}}

    def second_worker():
        second_started.set()
        return {"ok": True}

    def inspection_worker():
        inspection_started.set()
        return {"targets": []}

    async def run():
        with pytest.raises(TimeoutError):
            await manager.run_sync("tv.timeout_worker", late_attachment_worker, timeout_seconds=0.05, retries=1)

        assert started.is_set()
        status = manager.runtime_status()
        assert status["worker_running"] is True
        assert status["recovering_from_timeout"] is True
        assert status["preflight_code"] == "TV_MANAGER_RECOVERING"
        assert status["quarantined_operation"] == "tv.timeout_worker"
        assert status["manager_available"] is False

        with pytest.raises(TradingViewPreflightError) as second_exc:
            await manager.run_sync("tv.second_worker", second_worker, timeout_seconds=1)
        assert second_exc.value.code == "TV_MANAGER_RECOVERING"
        assert not second_started.is_set()

        with pytest.raises(TradingViewPreflightError) as inspection_exc:
            await manager.run_read_only_inspection("tv.discovery", inspection_worker, timeout_seconds=1)
        assert inspection_exc.value.code == "TV_MANAGER_RECOVERING"
        assert not inspection_started.is_set()

        with pytest.raises(TradingViewPreflightError) as detach_exc:
            await manager.detach_target_serialized()
        assert detach_exc.value.code == "TV_MANAGER_RECOVERING"

        release.set()
        for _ in range(100):
            if manager.operation_status_snapshot()["manager_available"]:
                break
            await asyncio.sleep(0.01)

        recovered = manager.operation_status_snapshot()
        assert recovered["worker_running"] is False
        assert recovered["recovering_from_timeout"] is False
        assert recovered["manager_available"] is True
        assert manager.attached_target_snapshot() is None
        assert not (tmp_path / "tv-pref.json").exists()

        result = await manager.run_sync(
            "tv.later_worker",
            lambda: {"ok": True, "diagnostics": {"managed_tab_id": "later-chart", "managed_tab_count": 1, "devtools_ws_connected": True}},
            timeout_seconds=1,
        )
        assert result["ok"] is True
        assert manager._current_tab_id == "later-chart"

    asyncio.run(run())


def test_unmanaged_tradingview_helpers_refuse_when_manager_busy(monkeypatch, tmp_path):
    import tv_client
    from scripts import export_tradingview_candles as export_script

    with pytest.raises(RuntimeError, match="TV_MANAGER_OWNERSHIP_REQUIRED"):
        tv_client.open_symbol("NSE:RELIANCE")

    monkeypatch.setattr(tv_client.TradingViewClient, "open_symbol", lambda self, symbol: {"symbol": symbol})
    with tv_client.allow_unmanaged_tradingview_client_for_tests():
        assert tv_client.open_symbol("NSE:RELIANCE") == {"symbol": "NSE:RELIANCE"}

    constructed = {"count": 0}

    class ExplodingClient:
        def __init__(self, *_args, **_kwargs):
            constructed["count"] += 1
            raise OperationStarted("export constructed client before manager ownership")

    class RecoveringManager:
        async def run_sync(self, *_args, **_kwargs):
            raise TradingViewPreflightError(
                "TV_MANAGER_RECOVERING",
                "previous worker still finishing",
                {"code": "TV_MANAGER_RECOVERING"},
            )

    monkeypatch.setattr(export_script, "TradingViewClient", ExplodingClient)
    monkeypatch.setattr(export_script, "tradingview_manager", RecoveringManager())
    monkeypatch.setattr(
        export_script,
        "parse_args",
        lambda: SimpleNamespace(
            port=9222,
            output_dir=tmp_path,
            only=["TEST_1H"],
            navigation_timeout=1,
            history_attempts=1,
            history_wait_seconds=0,
        ),
    )
    monkeypatch.setattr(export_script, "EXPORT_JOBS", [export_script.ExportJob("TEST", "NSE:TEST", "1H", days=1)])

    assert export_script.main() == 1
    assert constructed["count"] == 0


def test_manager_owned_export_succeeds_with_fake_client(monkeypatch, tmp_path):
    from scripts import export_tradingview_candles as export_script

    ownership = {"active": False, "constructed": 0, "run_sync": 0}

    class FakeManager:
        async def run_sync(self, _operation_name, func, *args, **kwargs):
            ownership["run_sync"] += 1
            assert kwargs["retries"] == 0
            ownership["active"] = True
            try:
                return func(*args)
            finally:
                ownership["active"] = False

    class FakeClient:
        def __init__(self, port):
            assert ownership["active"] is True
            ownership["constructed"] += 1
            self.port = port

        def connect_to_debug_port(self):
            return True

        def open_or_reuse_chart_tab(self):
            return {"id": "fake-chart", "url": "https://in.tradingview.com/chart/fake", "title": "TradingView"}

        def navigate_with_cdp(self, tab, _url, _stage):
            return tab

        def get_active_chart_symbol(self):
            return "NSE:TEST"

        def get_active_chart_resolution(self):
            return "60"

        def symbol_matches(self, active_symbol, requested_symbol):
            return active_symbol == requested_symbol

        def evaluate_runtime(self, expression):
            if "requestMoreData" in expression:
                return {"end_of_data": True}
            return {"visible_range": {"from": 1, "to": 2}}

        def extract_candles_from_active_chart(self, *_args, **_kwargs):
            now = export_script.datetime.now(export_script.UTC).timestamp()
            return [
                {"time": now - 1800, "open": 100, "high": 105, "low": 99, "close": 104, "volume": 1000},
                {"time": now - 600, "open": 104, "high": 106, "low": 103, "close": 105, "volume": 1200},
            ]

    monkeypatch.setattr(export_script, "TradingViewClient", FakeClient)
    monkeypatch.setattr(export_script, "tradingview_manager", FakeManager())
    monkeypatch.setattr(export_script, "EXPORT_JOBS", [export_script.ExportJob("TEST", "NSE:TEST", "1H", days=1)])
    monkeypatch.setattr(
        export_script,
        "parse_args",
        lambda: SimpleNamespace(
            port=9222,
            output_dir=tmp_path,
            only=["TEST_1H"],
            navigation_timeout=1,
            history_attempts=1,
            history_wait_seconds=0,
        ),
    )

    assert export_script.main() == 0
    assert ownership == {"active": False, "constructed": 1, "run_sync": 1}
    output_path = tmp_path / "TEST_1H.csv"
    assert output_path.exists()
    assert "symbol,tradingview_symbol,timeframe,timestamp,open,high,low,close,volume,source,exported_at" in output_path.read_text(encoding="utf-8")


def test_phase1_apply_requires_maintenance_and_backup_contract():
    from cli import phase1_stabilize
    from services.migration_safety import MigrationSafetyError

    args = phase1_stabilize.parse_args(["--apply"])
    isolated_db = SimpleNamespace(name="wave0d1_isolated_fake_db")

    with pytest.raises(MigrationSafetyError) as exc:
        asyncio.run(phase1_stabilize.run(args, db_override=isolated_db))

    assert exc.value.code == "MIGRATION_MAINTENANCE_REQUIRED"
    parsed = phase1_stabilize.parse_args([
        "--apply",
        "--maintenance-approved",
        "--plan-file",
        "plan.json",
        "--confirm-plan-sha256",
        "abc",
        "--backup-manifest",
        "backup.json",
    ])
    assert parsed.maintenance_approved is True
    assert parsed.plan_file == "plan.json"
    assert parsed.confirm_plan_sha256 == "abc"
    assert parsed.backup_manifest == "backup.json"


def test_capital_backfill_preview_contract_exposes_field_changes_and_stale_predicates():
    from cli import capital_backfill

    assert hasattr(capital_backfill, "build_field_level_preview")
    assert hasattr(capital_backfill, "stale_row_update_filter")
    db = SimpleNamespace(
        name="wave0d1_isolated_fake_db",
        paper_trades=ReadOnlyCollection(
            [
                {
                    "_id": "trade-1",
                    "paper_only": True,
                    "symbol": "TEST",
                    "status": "ACTIVE",
                    "outcome_status": "ACTIVE",
                    "entry_price": 100.0,
                    "stop_loss": 90.0,
                    "quantity": 100,
                    "quantity_remaining": 100,
                    "updated_at": "2026-06-01T00:00:00Z",
                }
            ]
        ),
    )
    preview = asyncio.run(capital_backfill.build_field_level_preview(db))

    assert preview["apply"] is False
    assert preview["zero_writes_performed"] is True
    assert preview["document_count"] == 1
    assert preview["operation_count"] == 1
    fields = {change["field"] for change in preview["proposed_field_changes"]}
    assert {"capital_model_version", "initial_margin_reserved", "margin_remaining"} <= fields
    operation = preview["plan"]["operations"][0]
    cas_filter = capital_backfill.stale_row_update_filter(operation)
    assert set(cas_filter) != {"_id"}
    assert cas_filter["status"] == "ACTIVE"


def test_critical_unique_indexes_are_centralized_at_startup():
    from services import mongo_indexes

    required = {
        "market_data",
        "pipeline_run_locks",
        "pipeline_run_status",
        "market_load_state",
        "scored_candidates",
        "paper_trades",
        "paper_signals",
        "paper_update_locks",
        "trade_journal",
        "ai_feature_snapshots",
    }
    assert required <= set(getattr(mongo_indexes, "CENTRALIZED_INDEX_COLLECTIONS"))
    assert callable(getattr(mongo_indexes, "get_critical_index_specs"))
    assert callable(getattr(mongo_indexes, "ensure_active_indexes"))


def test_tv_confirmation_unique_identity_policy_is_status_aware_not_blanket_base_identity():
    from services import mongo_indexes

    policy = getattr(mongo_indexes, "TV_CONFIRMATION_UNIQUE_POLICY", None)
    assert policy is not None
    assert policy["non_technical_filter"]
    assert "failure_run_id" in policy["technical_identity_fields"]
    assert policy["requires_setup_id"] is False
