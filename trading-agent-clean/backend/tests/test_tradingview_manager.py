import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tv_client as tv_client_module
from routes import tv as tv_routes
from services.tradingview_manager import TradingViewExecutionManager
from tv_client import MAX_MANAGED_TABS, TradingViewClient, TradingViewTabNavigationError, TradingViewTabNotAttachedError, is_app_managed_tab
from tv_confirmation import enforce_tv_confirmation_safety


def setup_function(_function) -> None:
    tv_client_module.MANAGED_TAB_IDS.clear()
    tv_routes.tradingview_manager.detach_target()


def test_simultaneous_tradingview_requests_run_serially() -> None:
    async def run() -> None:
        manager = TradingViewExecutionManager()
        active = 0
        max_active = 0
        lock = asyncio.Lock()

        def blocking_operation(name: str) -> dict:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            time.sleep(0.03)
            active -= 1
            return {"name": name, "diagnostics": {"managed_tab_id": name, "managed_tab_count": 1, "devtools_ws_connected": True}}

        async def call(name: str) -> dict:
            async with lock:
                pass
            return await manager.run_sync(name, blocking_operation, name, timeout_seconds=1)

        results = await asyncio.gather(call("one"), call("two"), call("three"))
        status = manager.runtime_status()

        assert [row["name"] for row in results] == ["one", "two", "three"]
        assert max_active == 1
        assert status["max_cdp_concurrency"] == 1
        assert status["completed_operation_count"] == 3

    asyncio.run(run())


def test_tradingview_timeout_releases_manager_for_next_request() -> None:
    async def run() -> None:
        manager = TradingViewExecutionManager()

        try:
            await manager.run_sync("slow", time.sleep, 0.2, timeout_seconds=0.01)
        except TimeoutError:
            pass
        else:
            raise AssertionError("timeout was expected")

        result = await manager.run_sync(
            "fast",
            lambda: {"ok": True, "diagnostics": {"managed_tab_id": "tab-fast", "managed_tab_count": 1, "devtools_ws_connected": True}},
            timeout_seconds=1,
        )
        status = manager.runtime_status()

        assert result["ok"] is True
        assert status["worker_running"] is False
        assert status["active_operation"] is None
        assert status["timeout_count"] == 1
        assert status["current_tab_id"] == "tab-fast"

    asyncio.run(run())


def test_failed_tradingview_request_does_not_poison_next_request() -> None:
    async def run() -> None:
        manager = TradingViewExecutionManager()

        def fail() -> None:
            raise ConnectionError("TradingView Desktop restarted")

        try:
            await manager.run_sync("first", fail, timeout_seconds=1)
        except ConnectionError:
            pass
        else:
            raise AssertionError("connection failure was expected")

        result = await manager.run_sync(
            "second",
            lambda: {"ok": True, "diagnostics": {"managed_tab_id": "tab-reconnected", "managed_tab_count": 1, "devtools_ws_connected": True}},
            timeout_seconds=1,
        )
        status = manager.runtime_status()

        assert result["ok"] is True
        assert status["connected"] is True
        assert status["last_error"] is None
        assert status["current_tab_id"] == "tab-reconnected"

    asyncio.run(run())


def test_tab_navigation_failure_releases_manager_for_next_request() -> None:
    async def run() -> None:
        manager = TradingViewExecutionManager()

        def fail_navigation() -> None:
            raise TradingViewTabNavigationError("TAB_NAVIGATION_FAILED: blank target stayed blank")

        try:
            await manager.run_sync("navigation", fail_navigation, timeout_seconds=1)
        except TradingViewTabNavigationError:
            pass
        else:
            raise AssertionError("navigation failure was expected")

        result = await manager.run_sync(
            "next",
            lambda: {"ok": True, "diagnostics": {"managed_tab_id": "next-tab", "managed_tab_count": 1, "devtools_ws_connected": True}},
            timeout_seconds=1,
        )
        status = manager.runtime_status()

        assert result["ok"] is True
        assert status["worker_running"] is False
        assert status["active_operation"] is None
        assert status["current_tab_id"] == "next-tab"

    asyncio.run(run())


def test_blocking_cdp_work_does_not_block_unrelated_async_work() -> None:
    async def run() -> None:
        manager = TradingViewExecutionManager()
        started = time.monotonic()
        blocking_task = asyncio.create_task(manager.run_sync("blocking", time.sleep, 0.1, timeout_seconds=1))
        await asyncio.sleep(0.01)
        elapsed = time.monotonic() - started
        await blocking_task

        assert elapsed < 0.05

    asyncio.run(run())


class FakeTabClient(TradingViewClient):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.tabs = [
            {"id": "managed-1", "url": "https://www.tradingview.com/chart/?trading_agent_managed=1", "title": "TradingView", "webSocketDebuggerUrl": "ws://managed-1"},
            {"id": "managed-2", "url": "https://www.tradingview.com/chart/?trading_agent_managed=1", "title": "TradingView", "webSocketDebuggerUrl": "ws://managed-2"},
            {"id": "user-tab", "url": "https://www.tradingview.com/chart/?symbol=NSE%3AUSER", "title": "TradingView", "webSocketDebuggerUrl": "ws://user"},
        ]
        self.closed = []
        self.activated = []

    def list_tabs(self) -> list[dict]:
        self.diagnostics["tabs_count"] = len(self.tabs)
        self.diagnostics["managed_tab_count"] = sum(1 for tab in self.tabs if is_app_managed_tab(tab))
        return [tab.copy() for tab in self.tabs]

    def _request_text(self, path: str) -> str:
        if path.startswith("/json/close/"):
            tab_id = path.rsplit("/", 1)[-1]
            self.closed.append(tab_id)
            self.tabs = [tab for tab in self.tabs if tab.get("id") != tab_id]
        if path.startswith("/json/activate/"):
            self.activated.append(path.rsplit("/", 1)[-1])
        return "{}"


def test_managed_tab_count_stays_bounded_and_user_tabs_are_preserved() -> None:
    client = FakeTabClient()

    tab = client.open_or_reuse_chart_tab()
    client.list_tabs()

    assert tab["id"] == "managed-1"
    assert client.closed == ["managed-2"]
    assert any(existing["id"] == "user-tab" for existing in client.tabs)
    assert client.diagnostics["managed_tab_count"] == MAX_MANAGED_TABS


def test_id_owned_managed_tab_cleanup_survives_stripped_marker() -> None:
    client = FakeTabClient()
    tv_client_module.MANAGED_TAB_IDS.update({"managed-1", "managed-2"})
    for tab in client.tabs:
        if tab["id"].startswith("managed-"):
            tab["url"] = "https://in.tradingview.com/chart/ThW59K6v/"

    tab = client.open_or_reuse_chart_tab()
    client.list_tabs()

    assert tab["id"] == "managed-1"
    assert client.closed == ["managed-2"]
    assert "managed-2" not in tv_client_module.MANAGED_TAB_IDS
    assert any(existing["id"] == "user-tab" for existing in client.tabs)
    assert client.diagnostics["managed_tab_count"] == MAX_MANAGED_TABS


class FakeNavigationWebSocket:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.queue = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def send(self, payload: str) -> None:
        message = json.loads(payload)
        self.state.setdefault("sent", []).append(message)
        method = message.get("method")
        call_id = message.get("id")
        if method in {"Page.enable", "Runtime.enable"}:
            self.queue.append({"id": call_id, "result": {}})
        elif method == "Page.navigate":
            if self.state.get("navigate_error"):
                self.queue.append({"id": call_id, "error": {"message": self.state["navigate_error"]}})
            else:
                update_tab = self.state.get("update_tab")
                if update_tab:
                    update_tab()
                self.queue.append({"id": call_id, "result": {"frameId": "frame-1"}})
                if self.state.get("load_event", True):
                    self.queue.append({"method": "Page.loadEventFired", "params": {}})
        elif method == "Runtime.evaluate":
            readiness = self.state.get(
                "readiness",
                {
                    "href": "https://in.tradingview.com/chart/ThW59K6v/",
                    "title": "TradingView",
                    "readyState": "complete",
                    "tradingviewApiAvailable": True,
                    "activeChartAvailable": True,
                },
            )
            self.queue.append({"id": call_id, "result": {"result": {"value": readiness}}})

    def recv(self, timeout: float = 5) -> str:
        if not self.queue:
            raise TimeoutError("no queued CDP message")
        return json.dumps(self.queue.pop(0))


def http_error(path: str = "/json/new") -> HTTPError:
    return HTTPError(path, 405, "Method Not Allowed", hdrs=None, fp=None)


class FakeAttachableTargetClient(TradingViewClient):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.tabs = [
            {"id": "chart-1", "type": "page", "url": "https://in.tradingview.com/chart/ThW59K6v/", "title": "TradingView Chart", "webSocketDebuggerUrl": "ws://chart-1"},
            {"id": "blank", "type": "page", "url": "", "title": "", "webSocketDebuggerUrl": "ws://blank"},
            {"id": "news", "type": "page", "url": "https://example.com/", "title": "Example", "webSocketDebuggerUrl": "ws://news"},
            {"id": "iframe", "type": "iframe", "url": "https://in.tradingview.com/chart/iframe", "title": "TradingView", "webSocketDebuggerUrl": "ws://iframe"},
        ]
        self.closed = []

    def _request_json(self, path: str, method: str = "GET"):
        if path == "/json/version":
            return {"webSocketDebuggerUrl": "ws://browser"}
        if path.startswith("/json/new?"):
            raise http_error(path)
        return self.list_tabs()

    def _request_text(self, path: str) -> str:
        if path.startswith("/json/close/"):
            tab_id = path.rsplit("/", 1)[-1]
            self.closed.append(tab_id)
            self.tabs = [tab for tab in self.tabs if tab.get("id") != tab_id]
        return "{}"

    def list_tabs(self) -> list[dict]:
        self.diagnostics["tabs_count"] = len(self.tabs)
        self.diagnostics["managed_tab_count"] = sum(1 for tab in self.tabs if is_app_managed_tab(tab))
        return [tab.copy() for tab in self.tabs]


def test_attachable_targets_list_only_ready_tradingview_chart_pages(monkeypatch) -> None:
    state = {"sent": []}

    def fake_ws_connect(url: str, open_timeout: int = 5):
        state["ws_url"] = url
        return FakeNavigationWebSocket(state)

    monkeypatch.setattr(tv_client_module, "ws_connect", fake_ws_connect)
    client = FakeAttachableTargetClient()

    targets = client.list_attachable_chart_targets()

    assert [target["target_id"] for target in targets] == ["chart-1"]
    assert targets[0]["ready"] is True
    assert targets[0]["websocket_debugger_url"] == "ws://chart-1"


def test_attachable_targets_exclude_chart_when_active_chart_is_not_ready(monkeypatch) -> None:
    state = {
        "sent": [],
        "readiness": {"href": "https://in.tradingview.com/chart/ThW59K6v/", "title": "TradingView", "readyState": "complete", "tradingviewApiAvailable": True, "activeChartAvailable": False},
    }

    def fake_ws_connect(url: str, open_timeout: int = 5):
        return FakeNavigationWebSocket(state)

    monkeypatch.setattr(tv_client_module, "ws_connect", fake_ws_connect)
    client = FakeAttachableTargetClient()

    assert client.list_attachable_chart_targets() == []


def test_attached_tab_is_reused_without_being_registered_for_cleanup() -> None:
    client = FakeAttachableTargetClient(attached_target_id="chart-1", require_attached_tab=True)

    tab = client.open_or_reuse_chart_tab()

    assert tab["id"] == "chart-1"
    assert client.diagnostics["managed_tab_id"] == "chart-1"
    assert client.close_managed_tab(tab) is False
    assert client.closed == []


def test_open_symbol_allows_attached_user_chart_target(monkeypatch) -> None:
    state = {"sent": []}

    def fake_ws_connect(url: str, open_timeout: int = 5):
        return FakeNavigationWebSocket(state)

    monkeypatch.setattr(tv_client_module, "ws_connect", fake_ws_connect)
    client = FakeAttachableTargetClient(attached_target_id="chart-1", require_attached_tab=True)

    tab = client.open_symbol("NSE:RELIANCE")

    assert tab["id"] == "chart-1"
    assert client.diagnostics["managed_tab_id"] == "chart-1"
    assert client.closed == []
    assert any(message.get("method") == "Page.navigate" for message in state["sent"])


def test_manager_stores_and_detaches_attached_target_details() -> None:
    manager = TradingViewExecutionManager()

    attached = manager.attach_target({"target_id": "chart-1", "title": "TradingView", "url": "https://in.tradingview.com/chart/ThW59K6v/", "websocket_debugger_url": "ws://chart-1", "ready": True})
    status = manager.runtime_status()

    assert attached["target_id"] == "chart-1"
    assert status["attached_target_id"] == "chart-1"
    assert status["attached_target"]["title"] == "TradingView"

    manager.detach_target()

    assert manager.runtime_status()["attached_target_id"] is None


def test_attached_target_disappearing_returns_disconnected() -> None:
    client = FakeAttachableTargetClient(attached_target_id="missing", require_attached_tab=True)

    assert client.load_symbol_strict("NSE:TATACAP") is False
    assert client.diagnostics["symbol_error_stage"] == "TV_TAB_DISCONNECTED"
    assert client.diagnostics["managed_tab_id"] == "missing"


def test_confirmation_without_attachment_fails_fast() -> None:
    client = FakeAttachableTargetClient(require_attached_tab=True)

    assert client.load_symbol_strict("NSE:TATACAP") is False
    assert client.diagnostics["symbol_error_stage"] == "TV_TAB_NOT_ATTACHED"


def test_invalid_attach_target_is_rejected(monkeypatch) -> None:
    state = {"sent": []}

    def fake_ws_connect(url: str, open_timeout: int = 5):
        return FakeNavigationWebSocket(state)

    monkeypatch.setattr(tv_client_module, "ws_connect", fake_ws_connect)
    client = FakeAttachableTargetClient()

    assert client.validate_attachable_target("blank") is None


def test_attach_tab_endpoint_helper_stores_validated_target(monkeypatch) -> None:
    state = {"sent": []}

    def fake_ws_connect(url: str, open_timeout: int = 5):
        return FakeNavigationWebSocket(state)

    monkeypatch.setattr(tv_client_module, "ws_connect", fake_ws_connect)
    monkeypatch.setattr(tv_routes, "TradingViewClient", FakeAttachableTargetClient)
    tv_routes.tradingview_manager.detach_target()
    try:
        result = tv_routes._attach_tab_sync("chart-1")
        status = tv_routes.tradingview_manager.runtime_status()
    finally:
        tv_routes.tradingview_manager.detach_target()

    assert result["attached"] is True
    assert result["attached_target_id"] == "chart-1"
    assert status["attached_target_id"] == "chart-1"


def test_list_attachable_tabs_auto_attaches_single_valid_target(monkeypatch) -> None:
    state = {"sent": []}

    def fake_ws_connect(url: str, open_timeout: int = 5):
        return FakeNavigationWebSocket(state)

    monkeypatch.setattr(tv_client_module, "ws_connect", fake_ws_connect)
    monkeypatch.setattr(tv_routes, "TradingViewClient", FakeAttachableTargetClient)

    result = tv_routes._list_attachable_tabs_sync()

    assert result["count"] == 1
    assert result["auto_attached"] is True
    assert result["attached_target_id"] == "chart-1"


def test_attach_tab_endpoint_helper_rejects_invalid_target(monkeypatch) -> None:
    state = {"sent": []}

    def fake_ws_connect(url: str, open_timeout: int = 5):
        return FakeNavigationWebSocket(state)

    monkeypatch.setattr(tv_client_module, "ws_connect", fake_ws_connect)
    monkeypatch.setattr(tv_routes, "TradingViewClient", FakeAttachableTargetClient)

    try:
        tv_routes._attach_tab_sync("blank")
    except ValueError as exc:
        assert "INVALID_TV_TARGET" in str(exc)
    else:
        raise AssertionError("invalid target should be rejected")


def test_json_new_failure_requires_attachment_without_browser_target_fallback(monkeypatch) -> None:
    state = {"ws_connected": False}

    def fake_ws_connect(*_args, **_kwargs):
        state["ws_connected"] = True
        raise AssertionError("browser WebSocket should not be used for target creation")

    monkeypatch.setattr(tv_client_module, "ws_connect", fake_ws_connect)

    class FakeUnsupportedCreateClient(TradingViewClient):
        def list_tabs(self) -> list[dict]:
            self.diagnostics["tabs_count"] = 0
            self.diagnostics["managed_tab_count"] = 0
            return []

        def _request_json(self, path: str, method: str = "GET"):
            if path.startswith("/json/new?"):
                raise http_error(path)
            return []

    client = FakeUnsupportedCreateClient()

    try:
        client.open_or_reuse_chart_tab()
    except TradingViewTabNotAttachedError as exc:
        assert str(exc) == "TV_TAB_NOT_ATTACHED"
    else:
        raise AssertionError("unsupported tab creation should require an attached tab")
    assert state["ws_connected"] is False
    assert client.diagnostics["reason"] == "TV_TAB_NOT_ATTACHED"
    assert client.diagnostics["error_stage"] == "managed_tab_creation_unsupported"


def test_confirmation_safety_downgrades_symbol_mismatch() -> None:
    row = {
        "tv_status": "CONFIRMED_SIGNAL",
        "tv_confirmed": True,
        "requested_tradingview_symbol": "NSE:WANTED",
        "active_symbol_after_stabilize": "NSE:OTHER",
        "symbol_match": False,
        "candles_by_timeframe": {"1D": 100},
        "timeframe_debug": {"1D": {"resolution_match": True, "resolution_after": "D", "candles_count": 100}},
    }

    guarded = enforce_tv_confirmation_safety(row, "swing", ["1D"])

    assert guarded["tv_status"] == "TECHNICAL_FAILED"
    assert guarded["reason"] == "SYMBOL_MISMATCH"
    assert guarded["requested_symbol"] == "NSE:WANTED"
    assert guarded["loaded_symbol"] == "NSE:OTHER"


def test_confirmation_safety_downgrades_rows_without_candles() -> None:
    row = {
        "tv_status": "MOMENTUM_CONFIRMED",
        "tv_confirmed": True,
        "requested_tradingview_symbol": "NSE:READY",
        "active_symbol_after_stabilize": "NSE:READY",
        "symbol_match": True,
        "candles_by_timeframe": {"1D": 0},
        "timeframe_debug": {"1D": {"resolution_match": True, "resolution_after": "D", "candles_count": 0}},
    }

    guarded = enforce_tv_confirmation_safety(row, "momentum", ["1D"])

    assert guarded["tv_status"] == "TECHNICAL_FAILED"
    assert guarded["reason"] == "NO_CANDLES"
