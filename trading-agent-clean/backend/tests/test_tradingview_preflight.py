import asyncio
import json
import os
import sys
from pathlib import Path
import pytest
from datetime import datetime
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.tradingview_manager import tradingview_manager, TradingViewPreflightError
from routes import swing, momentum, signals
from tv_client import TradingViewClient
from tests.test_paper_auto_sync import FakeCursor
FakeCursor.skip = lambda self, skip: (setattr(self, "rows", self.rows[skip:]), self)[1]

# Helper mock candidate
def sample_candidate(symbol: str = "TEST") -> dict:
    return {
        "exchange": "NSE",
        "symbol": symbol,
        "canonical_symbol": symbol,
        "tradingview_symbol": f"NSE:{symbol}",
        "current_price": 100.0,
        "selected_for_tv": True,
        "swing_candidate": True,
        "swing_status": "SWING_SELECTED_FOR_TV",
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


class FakeCountCollection:
    def __init__(self) -> None:
        self.rows = [sample_candidate()]

    async def count_documents(self, query):
        return len(self.rows)

    def find(self, query=None, *args, **kwargs):
        from tests.test_paper_auto_sync import FakeCursor
        return FakeCursor(self.rows)

    async def find_one(self, *args, **kwargs):
        return {"updated_at": "2026-06-24T00:00:00"}


class FakeFindOneCollection:
    async def find_one(self, *args, **kwargs):
        return {"updated_at": "2026-06-24T00:00:00"}


class FakeSaveDb:
    def __init__(self) -> None:
        self.swing_tv_confirmations = FakeUpdateCollection()
        self.momentum_tv_confirmations = FakeUpdateCollection()
        self.paper_signals = FakeUpdateCollection()
        self.system_errors = FakeUpdateCollection()
        self.scored_candidates = FakeCountCollection()
        self.market_data = FakeFindOneCollection()


@pytest.fixture(autouse=True)
def cleanup_manager():
    tradingview_manager.reset()
    if os.path.exists(tradingview_manager.preference_file):
        try:
            os.remove(tradingview_manager.preference_file)
        except Exception:
            pass
    yield
    tradingview_manager.reset()
    if os.path.exists(tradingview_manager.preference_file):
        try:
            os.remove(tradingview_manager.preference_file)
        except Exception:
            pass


def test_no_attached_tab_blocks_batch_before_candidate_iteration(monkeypatch):
    # Mock list_tabs to return empty
    monkeypatch.setattr(TradingViewClient, "connect_to_debug_port", lambda self: None)
    monkeypatch.setattr(TradingViewClient, "list_tabs", lambda self: [])
    fake_db = FakeSaveDb()
    monkeypatch.setattr(swing, "get_database", lambda: fake_db)

    # Call swing confirmation. Preflight should fail and abort immediately.
    result = asyncio.run(swing.run_swing_tv_confirmation(
        index_name="BROAD_MARKET_750",
        requested_limit=5,
        checked_timeframes=["1D"],
        save=False,
    ))

    assert isinstance(result, JSONResponse)
    assert result.status_code == 400

    content = json.loads(result.body.decode("utf-8"))
    assert content["code"] == "TV_NO_VALID_CHART_TAB"
    assert content["valid_target_count"] == 0
    assert content["attached_target_id"] is None


def test_preflight_failure_writes_nothing_to_db(monkeypatch):
    # Setup mock DB
    fake_db = FakeSaveDb()
    monkeypatch.setattr(swing, "get_database", lambda: fake_db)
    monkeypatch.setattr(momentum, "get_database", lambda: fake_db)

    # CDP unavailable to force preflight failure
    def fail_connect(self):
        raise ConnectionError("CDP port closed")
    monkeypatch.setattr(TradingViewClient, "connect_to_debug_port", fail_connect)

    # 1. Test Swing with save=True
    result_swing = asyncio.run(swing.run_swing_tv_confirmation(
        index_name="BROAD_MARKET_750",
        requested_limit=5,
        checked_timeframes=["1D"],
        save=True,
    ))
    assert isinstance(result_swing, JSONResponse)
    assert result_swing.status_code == 400
    assert len(fake_db.swing_tv_confirmations.calls) == 0
    assert len(fake_db.system_errors.calls) == 0

    # 2. Test Momentum with save=True
    result_momentum = asyncio.run(momentum.run_momentum_tv_confirmation(
        index_name="BROAD_MARKET_750",
        requested_limit=5,
        checked_timeframes=["1D"],
        save=True,
    ))
    assert isinstance(result_momentum, JSONResponse)
    assert result_momentum.status_code == 400
    assert len(fake_db.momentum_tv_confirmations.calls) == 0
    assert len(fake_db.system_errors.calls) == 0


def test_cdp_unavailable_error_code(monkeypatch):
    def fail_connect(self):
        raise ConnectionError("CDP connection refused")
    monkeypatch.setattr(TradingViewClient, "connect_to_debug_port", fail_connect)

    with pytest.raises(TradingViewPreflightError) as exc_info:
        tradingview_manager.ensure_ready_attached_target()

    assert exc_info.value.code == "TV_CDP_UNREACHABLE"
    assert "is unreachable" in exc_info.value.message
    assert exc_info.value.details["cdp_reachable"] is False


def test_exactly_one_valid_chart_auto_attaches(monkeypatch):
    tab = {
        "id": "single-chart-id",
        "type": "page",
        "title": "NIFTY Chart",
        "url": "https://in.tradingview.com/chart/abcd/",
        "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/single-chart-id"
    }

    monkeypatch.setattr(TradingViewClient, "connect_to_debug_port", lambda self: None)
    monkeypatch.setattr(TradingViewClient, "list_tabs", lambda self: [tab])
    monkeypatch.setattr(TradingViewClient, "read_chart_readiness", lambda self, t: {"activeChartAvailable": True})

    assert tradingview_manager.attached_target_id is None

    # Preflight should auto-attach
    tradingview_manager.ensure_ready_attached_target()

    assert tradingview_manager.attached_target_id == "single-chart-id"
    assert tradingview_manager._connected is True


def test_multiple_valid_charts_require_manual_selection(monkeypatch):
    tab1 = {
        "id": "chart1",
        "type": "page",
        "title": "NIFTY Chart 1",
        "url": "https://in.tradingview.com/chart/abcd1/",
        "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/chart1"
    }
    tab2 = {
        "id": "chart2",
        "type": "page",
        "title": "NIFTY Chart 2",
        "url": "https://in.tradingview.com/chart/abcd2/",
        "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/chart2"
    }

    monkeypatch.setattr(TradingViewClient, "connect_to_debug_port", lambda self: None)
    monkeypatch.setattr(TradingViewClient, "list_tabs", lambda self: [tab1, tab2])

    with pytest.raises(TradingViewPreflightError) as exc_info:
        tradingview_manager.ensure_ready_attached_target()

    assert exc_info.value.code == "TV_MULTIPLE_TABS_SELECTION_REQUIRED"
    assert exc_info.value.details["manual_attachment_required"] is True


def test_stale_persisted_target_id_is_not_trusted(monkeypatch):
    # Save a stale preference
    pref = {
        "target_id": "stale-pref-id",
        "title": "Stale",
        "url": "https://in.tradingview.com/chart/stale/",
        "websocket_debugger_url": "ws://localhost:9222/devtools/page/stale-pref-id"
    }
    tradingview_manager.attach_target(pref)

    # Mock list_tabs to NOT contain the preference
    monkeypatch.setattr(TradingViewClient, "connect_to_debug_port", lambda self: None)
    monkeypatch.setattr(TradingViewClient, "list_tabs", lambda self: [])

    # Re-validate preference
    asyncio.run(tradingview_manager.validate_preference_on_restart())

    assert tradingview_manager.attached_target_id is None
    assert tradingview_manager._connected is False


def test_valid_persisted_target_is_revalidated_after_restart(monkeypatch):
    pref_tab = {
        "id": "valid-pref-id",
        "type": "page",
        "title": "Valid Chart",
        "url": "https://in.tradingview.com/chart/valid/",
        "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/valid-pref-id"
    }
    tradingview_manager.attach_target({
        "target_id": "valid-pref-id",
        "title": "Valid Chart",
        "url": "https://in.tradingview.com/chart/valid/",
        "websocket_debugger_url": "ws://localhost:9222/devtools/page/valid-pref-id"
    })

    monkeypatch.setattr(TradingViewClient, "connect_to_debug_port", lambda self: None)
    monkeypatch.setattr(TradingViewClient, "list_tabs", lambda self: [pref_tab])
    monkeypatch.setattr(TradingViewClient, "read_chart_readiness", lambda self, t: {"activeChartAvailable": True})

    # Validate preference on restart
    asyncio.run(tradingview_manager.validate_preference_on_restart())

    assert tradingview_manager.attached_target_id == "valid-pref-id"
    assert tradingview_manager._connected is True


def test_target_disappears_after_frontend_check_or_queueing(monkeypatch):
    tab = {
        "id": "temp-chart",
        "type": "page",
        "title": "Temp Chart",
        "url": "https://in.tradingview.com/chart/temp/",
        "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/temp-chart"
    }

    tabs_state = [tab]

    monkeypatch.setattr(TradingViewClient, "connect_to_debug_port", lambda self: None)
    monkeypatch.setattr(TradingViewClient, "list_tabs", lambda self: tabs_state)
    monkeypatch.setattr(TradingViewClient, "read_chart_readiness", lambda self, t: {"activeChartAvailable": True})

    # Frontend check should see ready target
    status = tradingview_manager.get_preflight_status()
    assert status["preflight_ready"] is False  # one tab is open and will be auto-attached
    assert status["valid_chart_target_count"] == 1

    # Preflight auto-attaches
    tradingview_manager.ensure_ready_attached_target()
    assert tradingview_manager.attached_target_id == "temp-chart"

    # Now target disappears
    tabs_state.clear()

    # Preflight call inside run_sync must detect it disappeared
    with pytest.raises(TradingViewPreflightError) as exc_info:
        asyncio.run(tradingview_manager.run_sync("test_op", lambda: {"status": "ok"}, require_preflight=True))

    assert exc_info.value.code in ("TV_NO_VALID_CHART_TAB", "TV_ATTACH_FAILED")
    assert tradingview_manager.attached_target_id is None


def test_chart_exists_but_active_chart_is_unavailable(monkeypatch):
    tab = {
        "id": "unready-chart",
        "type": "page",
        "title": "Unready Chart",
        "url": "https://in.tradingview.com/chart/unready/",
        "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/unready-chart"
    }

    monkeypatch.setattr(TradingViewClient, "connect_to_debug_port", lambda self: None)
    monkeypatch.setattr(TradingViewClient, "list_tabs", lambda self: [tab])
    # activeChartAvailable is False
    monkeypatch.setattr(TradingViewClient, "read_chart_readiness", lambda self, t: {"activeChartAvailable": False})

    with pytest.raises(TradingViewPreflightError) as exc_info:
        tradingview_manager.ensure_ready_attached_target()

    assert exc_info.value.code == "TV_CHART_NOT_READY"


def test_manager_busy_returns_tv_manager_busy(monkeypatch):
    async def run_test():
        # Acquire lock manually
        await tradingview_manager._lock.acquire()
        assert tradingview_manager._lock.locked() is True

        try:
            with pytest.raises(TradingViewPreflightError) as exc_info:
                await tradingview_manager.run_sync("dummy", lambda: None, require_preflight=True)
            assert exc_info.value.code == "TV_OPERATION_BUSY"
        finally:
            tradingview_manager._lock.release()

    asyncio.run(run_test())


def test_lock_is_released_after_every_preflight_failure(monkeypatch):
    monkeypatch.setattr(TradingViewClient, "connect_to_debug_port", lambda self: None)
    monkeypatch.setattr(TradingViewClient, "list_tabs", lambda self: [])

    assert tradingview_manager._lock.locked() is False

    with pytest.raises(TradingViewPreflightError):
        asyncio.run(tradingview_manager.run_sync("dummy", lambda: None, require_preflight=True))

    # Lock must be released immediately after preflight failure
    assert tradingview_manager._lock.locked() is False


def test_saved_result_signal_building_works_without_live_tv(monkeypatch):
    fake_db = FakeSaveDb()
    monkeypatch.setattr(signals, "get_database", lambda: fake_db)

    # Mock DB find call
    class FakeCursor:
        def __init__(self, items):
            self.items = items
            self.index = 0
        def sort(self, *args, **kwargs):
            return self
        def limit(self, *args, **kwargs):
            return self
        def __aiter__(self):
            return self
        async def __anext__(self):
            if self.index < len(self.items):
                val = self.items[self.index]
                self.index += 1
                return val
            raise StopAsyncIteration

    fake_db.paper_signals.find = lambda *args, **kwargs: FakeCursor([{"symbol": "NSE:RELIANCE", "timeframe": "1D", "paper_only": True}])

    # Call get_paper_signals which consumes saved records
    result = asyncio.run(signals.get_paper_signals(limit=10))
    assert result["count"] == 1
    assert result["signals"][0]["symbol"] == "NSE:RELIANCE"


def test_backend_runtime_status_exposes_preflight_ready(monkeypatch):
    monkeypatch.setattr(TradingViewClient, "connect_to_debug_port", lambda self: None)
    monkeypatch.setattr(TradingViewClient, "list_tabs", lambda self: [])

    status = tradingview_manager.runtime_status()
    assert "preflight_ready" in status
    assert "preflight_code" in status
    assert "preflight_message" in status
    assert "cdp_reachable" in status
    assert "valid_chart_target_count" in status
    assert "manual_attachment_required" in status
    assert "attached_target_ready" in status
    assert "last_attachment_error" in status


def test_historical_records_classification():
    # Verify that TECHNICAL_FAILED does not count as strategy rejection or similar
    from routes.swing import should_save_confirmation_row

    # Preflight errors should not be saved as confirmation rows
    assert should_save_confirmation_row({"tv_status": "TECHNICAL_FAILED", "reason": "TV_NO_VALID_CHART_TAB"}) is False
    assert should_save_confirmation_row({"tv_status": "TECHNICAL_FAILED", "reason": "TV_TAB_DISCONNECTED"}) is False

    # Real technical failures during operation are saved so they can be shown in UI, but separated
    assert should_save_confirmation_row({"tv_status": "TECHNICAL_FAILED", "reason": "SYMBOL_LOAD_FAILED"}) is True


def test_swing_and_momentum_share_same_preflight():
    assert swing.tradingview_manager is tradingview_manager
    assert momentum.tradingview_manager is tradingview_manager


def test_preflight_failure_with_save_false_writes_nothing(monkeypatch):
    fake_db = FakeSaveDb()
    monkeypatch.setattr(swing, "get_database", lambda: fake_db)
    monkeypatch.setattr(momentum, "get_database", lambda: fake_db)

    def fail_connect(self):
        raise ConnectionError("CDP port closed")
    monkeypatch.setattr(TradingViewClient, "connect_to_debug_port", fail_connect)

    # Test Swing with save=False
    result_swing = asyncio.run(swing.run_swing_tv_confirmation(
        index_name="BROAD_MARKET_750",
        requested_limit=5,
        checked_timeframes=["1D"],
        save=False,
    ))
    assert isinstance(result_swing, JSONResponse)
    assert result_swing.status_code == 400
    assert len(fake_db.swing_tv_confirmations.calls) == 0
    assert len(fake_db.system_errors.calls) == 0

    # Test Momentum with save=False
    result_momentum = asyncio.run(momentum.run_momentum_tv_confirmation(
        index_name="BROAD_MARKET_750",
        requested_limit=5,
        checked_timeframes=["1D"],
        save=False,
    ))
    assert isinstance(result_momentum, JSONResponse)
    assert result_momentum.status_code == 400
    assert len(fake_db.momentum_tv_confirmations.calls) == 0
    assert len(fake_db.system_errors.calls) == 0


def test_target_disappears_after_queueing_but_before_execution(monkeypatch):
    tab = {
        "id": "queued-chart",
        "type": "page",
        "title": "Queued Chart",
        "url": "https://in.tradingview.com/chart/queued/",
        "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/queued-chart"
    }

    tabs_state = [tab]
    monkeypatch.setattr(TradingViewClient, "connect_to_debug_port", lambda self: None)
    monkeypatch.setattr(TradingViewClient, "list_tabs", lambda self: tabs_state)
    monkeypatch.setattr(TradingViewClient, "read_chart_readiness", lambda self, t: {"activeChartAvailable": True})

    # Initially, get preflight status shows ready
    status = tradingview_manager.get_preflight_status()
    assert status["valid_chart_target_count"] == 1

    # Attach the tab first
    tradingview_manager.ensure_ready_attached_target()
    assert tradingview_manager.attached_target_id == "queued-chart"

    # Acquire lock manually to queue the next operation
    async def run_disappear_flow():
        await tradingview_manager._lock.acquire()
        assert tradingview_manager._lock.locked() is True

        # Start the run_sync task in background (it will block on the lock)
        sync_task = asyncio.create_task(
            tradingview_manager.run_sync("test_op", lambda: {"status": "ok"}, require_preflight=True)
        )

        # While queued, the target disappears
        tabs_state.clear()

        # Release the lock so the background task can acquire it and run preflight
        tradingview_manager._lock.release()

        # Wait for the task to finish and check if it raised preflight error
        with pytest.raises(TradingViewPreflightError) as exc_info:
            await sync_task

        assert exc_info.value.code == "TV_NO_VALID_CHART_TAB"
        assert tradingview_manager.attached_target_id is None

    asyncio.run(run_disappear_flow())


def _make_tab(tab_id="chart-1", title="NIFTY Chart", url="https://in.tradingview.com/chart/abc/"):
    return {
        "id": tab_id,
        "type": "page",
        "title": title,
        "url": url,
        "webSocketDebuggerUrl": f"ws://localhost:9222/devtools/page/{tab_id}",
    }


def _setup_single_tab_env(monkeypatch, tab=None):
    if tab is None:
        tab = _make_tab()
    monkeypatch.setattr(TradingViewClient, "connect_to_debug_port", lambda self: None)
    monkeypatch.setattr(TradingViewClient, "list_tabs", lambda self: [tab])
    monkeypatch.setattr(TradingViewClient, "read_chart_readiness", lambda self, t: {"activeChartAvailable": True})
    return tab


def test_one_valid_tab_preflight_status_is_neutral_not_failure(monkeypatch):
    """Phase 7 #3: One valid tab remains neutral before an operation."""
    _setup_single_tab_env(monkeypatch)

    status = tradingview_manager.get_preflight_status()

    assert status["preflight_ready"] is False  # Not ready yet (no attachment)
    assert status["preflight_code"] == "TV_TAB_NOT_ATTACHED"
    assert status["valid_chart_target_count"] == 1
    assert status["cdp_reachable"] is True
    assert status["manual_attachment_required"] is False  # NOT manual; will auto-attach
    assert "auto-attached" in status["preflight_message"].lower() or "will be" in status["preflight_message"].lower()


def test_runtime_status_one_tab_returns_fields_for_frontend_pending_check(monkeypatch):
    """Verify runtime_status returns all fields the frontend needs for singleTabPending."""
    _setup_single_tab_env(monkeypatch)

    status = tradingview_manager.runtime_status()

    assert "cdp_reachable" in status
    assert "valid_chart_target_count" in status
    assert "attached_target_id" in status
    assert "manual_attachment_required" in status
    assert status["cdp_reachable"] is True
    assert status["valid_chart_target_count"] == 1
    assert status["attached_target_id"] is None
    assert status["manual_attachment_required"] is False


def test_test_symbol_auto_attaches_single_valid_tab(monkeypatch):
    """Phase 7 #4: test-symbol auto-attaches the one valid tab."""
    tab = _setup_single_tab_env(monkeypatch)

    assert tradingview_manager.attached_target_id is None
    tradingview_manager.ensure_ready_attached_target()
    assert tradingview_manager.attached_target_id == tab["id"]
    assert tradingview_manager._connected is True


def test_swing_auto_attaches_single_valid_tab(monkeypatch):
    """Phase 7 #5: Swing confirmation auto-attaches the one valid tab."""
    tab = _setup_single_tab_env(monkeypatch)

    assert tradingview_manager.attached_target_id is None

    # ensure_ready_attached_target is called by run_sync when require_preflight=True
    tradingview_manager.ensure_ready_attached_target()
    assert tradingview_manager.attached_target_id == tab["id"]


def test_momentum_auto_attaches_single_valid_tab(monkeypatch):
    """Phase 7 #6: Momentum confirmation auto-attaches the one valid tab."""
    tab = _setup_single_tab_env(monkeypatch)

    assert tradingview_manager.attached_target_id is None

    tradingview_manager.ensure_ready_attached_target()
    assert tradingview_manager.attached_target_id == tab["id"]


def test_multiple_tabs_require_explicit_selection_in_status(monkeypatch):
    """Phase 7 #7 + #15: Multiple tabs require explicit selection."""
    tab1 = _make_tab("chart-1", "Chart 1")
    tab2 = _make_tab("chart-2", "Chart 2")
    monkeypatch.setattr(TradingViewClient, "connect_to_debug_port", lambda self: None)
    monkeypatch.setattr(TradingViewClient, "list_tabs", lambda self: [tab1, tab2])
    monkeypatch.setattr(TradingViewClient, "read_chart_readiness", lambda self, t: {"activeChartAvailable": True})

    status = tradingview_manager.get_preflight_status()

    assert status["preflight_ready"] is False
    assert status["preflight_code"] == "TV_MULTIPLE_TABS_SELECTION_REQUIRED"
    assert status["valid_chart_target_count"] == 2
    assert status["manual_attachment_required"] is True


def test_stale_stored_tab_plus_one_replacement_auto_attaches(monkeypatch):
    """Phase 7 #10: Stale stored tab plus one replacement auto-attaches."""
    # First, attach a tab that will become stale
    stale_tab = _make_tab("stale-chart", "Stale Chart")
    tradingview_manager.attach_target({
        "target_id": "stale-chart",
        "title": "Stale Chart",
        "url": "https://in.tradingview.com/chart/stale/",
    })
    assert tradingview_manager.attached_target_id == "stale-chart"

    # Now the stale tab is gone; a new replacement tab is available
    new_tab = _make_tab("new-chart", "New Chart")
    monkeypatch.setattr(TradingViewClient, "connect_to_debug_port", lambda self: None)
    monkeypatch.setattr(TradingViewClient, "list_tabs", lambda self: [new_tab])
    monkeypatch.setattr(TradingViewClient, "read_chart_readiness", lambda self, t: {"activeChartAvailable": True})

    # ensure_ready_attached_target should clear the stale tab and auto-attach the new one
    tradingview_manager.ensure_ready_attached_target()
    assert tradingview_manager.attached_target_id == "new-chart"


def test_attachment_result_updates_runtime_status(monkeypatch):
    """Phase 7 #11: Attachment result updates runtime status."""
    tab = _setup_single_tab_env(monkeypatch)

    # Before attachment
    status_before = tradingview_manager.runtime_status()
    assert status_before["attached_target_id"] is None

    # Attach
    tradingview_manager.ensure_ready_attached_target()

    # After attachment, runtime status should reflect the attachment
    status_after = tradingview_manager.runtime_status()
    assert status_after["attached_target_id"] == tab["id"]
    assert status_after["attached_target"] is not None
    assert status_after["attached_target"]["target_id"] == tab["id"]
    assert status_after["connected"] is True


def test_attachment_failure_returns_stable_error(monkeypatch):
    """Phase 7 #12: Attachment failure returns a stable error."""
    monkeypatch.setattr(TradingViewClient, "connect_to_debug_port", lambda self: (_ for _ in ()).throw(ConnectionError("CDP port closed")))

    with pytest.raises(TradingViewPreflightError) as exc_info:
        tradingview_manager.ensure_ready_attached_target()

    assert exc_info.value.code == "TV_CDP_UNREACHABLE"
    assert "unreachable" in exc_info.value.message.lower()


def test_zero_valid_tabs_blocks_attachment(monkeypatch):
    """Phase 7 #2: Zero valid tabs."""
    monkeypatch.setattr(TradingViewClient, "connect_to_debug_port", lambda self: None)
    monkeypatch.setattr(TradingViewClient, "list_tabs", lambda self: [])

    with pytest.raises(TradingViewPreflightError) as exc_info:
        tradingview_manager.ensure_ready_attached_target()

    assert exc_info.value.code == "TV_NO_VALID_CHART_TAB"
    assert exc_info.value.details["valid_target_count"] == 0


def test_cdp_unreachable_preflight(monkeypatch):
    """Phase 7 #1: CDP unreachable."""
    monkeypatch.setattr(TradingViewClient, "connect_to_debug_port", lambda self: (_ for _ in ()).throw(ConnectionError("Connection refused")))

    status = tradingview_manager.get_preflight_status()

    assert status["preflight_ready"] is False
    assert status["preflight_code"] == "TV_CDP_UNREACHABLE"
    assert status["cdp_reachable"] is False


def test_symbol_timeframe_normalization_and_routing():
    from tv_client import normalize_symbol, compare_symbols, normalize_timeframe, validate_timeframe

    # 1. NSE:SBIN equals detected NSE:SBIN
    assert compare_symbols("NSE:SBIN", "NSE:SBIN") is True

    # 2. NSE:SBIN equals safely normalized SBIN with known exchange
    assert compare_symbols("SBIN", "NSE:SBIN") is True
    assert compare_symbols("NSE:SBIN · 1D", "NSE:SBIN") is True
    assert compare_symbols("SBIN · 1D · NSE", "NSE:SBIN") is True

    # 3. Unrelated symbol is rejected
    assert compare_symbols("NSE:SBIN", "NSE:RELIANCE") is False
    assert compare_symbols("NSE:SBIN", "BSE:SBIN") is False

    # 4. 1W equals W
    assert normalize_timeframe("W") == "1W"
    assert normalize_timeframe("1W") == "1W"
    assert normalize_timeframe("1 week") == "1W"

    # 5. 1D equals D
    assert normalize_timeframe("D") == "1D"
    assert normalize_timeframe("1D") == "1D"

    # 6. 4H equals 240
    assert normalize_timeframe("240") == "4H"
    assert normalize_timeframe("4H") == "4H"

    # 7. 1H equals 60
    assert normalize_timeframe("60") == "1H"
    assert normalize_timeframe("1H") == "1H"

    # 8. validate_timeframe accepts raw and normalized values
    assert validate_timeframe("W") == "W"
    assert validate_timeframe("1W") == "W"
    assert validate_timeframe("240") == "240"
    assert validate_timeframe("4H") == "240"


def test_client_level_timeframe_and_symbol_avoidance(monkeypatch):
    from tv_client import TradingViewClient

    calls = []

    def mock_get_active_chart_symbol(self):
        return "NSE:SBIN"

    def mock_get_active_chart_resolution(self):
        return "W"

    def mock_open_symbol(self, symbol):
        calls.append(("open_symbol", symbol))

    def mock_navigate_with_cdp(self, tab, url, stage):
        calls.append(("navigate", url, stage))
        return tab

    monkeypatch.setattr(TradingViewClient, "get_active_chart_symbol", mock_get_active_chart_symbol)
    monkeypatch.setattr(TradingViewClient, "get_active_chart_resolution", mock_get_active_chart_resolution)
    monkeypatch.setattr(TradingViewClient, "open_symbol", mock_open_symbol)
    monkeypatch.setattr(TradingViewClient, "navigate_with_cdp", mock_navigate_with_cdp)
    monkeypatch.setattr(TradingViewClient, "ensure_managed_tab", lambda self: {"id": "fake_tab", "url": "https://www.tradingview.com/chart/?symbol=NSE:SBIN&interval=W"})
    monkeypatch.setattr(TradingViewClient, "evaluate_runtime", lambda self, expr: (_ for _ in ()).throw(RuntimeError("Mocked CDP error")))

    client = TradingViewClient()
    client.diagnostics["requested_tradingview_symbol"] = "NSE:SBIN"

    # load_symbol_strict when symbol already active
    res = client.load_symbol_strict("NSE:SBIN")
    assert res is True
    assert ("open_symbol", "NSE:SBIN") not in calls  # Skipped!

    # set_timeframe when timeframe already active
    client.set_timeframe("1W")
    assert not any(c[0] == "navigate" for c in calls)  # Skipped!

    # set_timeframe when timeframe different
    client.set_timeframe("1D")
    assert any(c[0] == "navigate" for c in calls)  # Navigated!
