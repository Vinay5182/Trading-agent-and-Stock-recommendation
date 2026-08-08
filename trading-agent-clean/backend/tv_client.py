import contextlib
import contextvars
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen

from websockets.sync.client import connect as ws_connect

from config import settings


logger = logging.getLogger("uvicorn.error")
SUPPORTED_TIMEFRAMES = {"1W": "W", "1D": "D", "4H": "240", "1H": "60"}
MANAGED_TAB_MARKER = "trading_agent_managed=1"
MAX_MANAGED_TABS = 1
MANAGED_TAB_IDS: set[str] = set()
CHART_NAVIGATION_TIMEOUT_SECONDS = 20.0
CDP_RECV_POLL_SECONDS = 0.5
_MANAGER_OPERATION_CONTEXT = contextvars.ContextVar("tradingview_manager_operation", default=None)
_ALLOW_UNMANAGED_TEST_CONTEXT = contextvars.ContextVar("allow_unmanaged_tradingview_test_context", default=False)


class TradingViewTabNavigationError(RuntimeError):
    pass


class TradingViewTabNotAttachedError(RuntimeError):
    pass


class TradingViewTabDisconnectedError(RuntimeError):
    def __init__(self, target_id: str | None = None, message: str | None = None) -> None:
        self.target_id = target_id
        super().__init__(message or "TV_TAB_DISCONNECTED")


@contextlib.contextmanager
def manager_operation_context(operation_name: str, generation: int):
    token = _MANAGER_OPERATION_CONTEXT.set({"operation_name": operation_name, "generation": generation})
    try:
        yield
    finally:
        _MANAGER_OPERATION_CONTEXT.reset(token)


@contextlib.contextmanager
def allow_unmanaged_tradingview_client_for_tests():
    token = _ALLOW_UNMANAGED_TEST_CONTEXT.set(True)
    try:
        yield
    finally:
        _ALLOW_UNMANAGED_TEST_CONTEXT.reset(token)


def manager_operation_active() -> bool:
    return _MANAGER_OPERATION_CONTEXT.get() is not None


def require_manager_available(action: str = "TradingView helper") -> None:
    if manager_operation_active() or _ALLOW_UNMANAGED_TEST_CONTEXT.get():
        return
    raise RuntimeError(
        "TV_MANAGER_OWNERSHIP_REQUIRED: "
        f"{action} must run inside TradingViewExecutionManager.run_sync or an explicit test context."
    )


@dataclass
class TradingViewClient:
    port: int = settings.TRADINGVIEW_DEBUG_PORT
    attached_target_id: str | None = None
    require_attached_tab: bool = False

    def __post_init__(self) -> None:
        self.deadline_monotonic = None
        self.diagnostics = {
            "devtools_version_ok": False,
            "tabs_count": 0,
            "chart_tab_found": False,
            "json_new_method_used": None,
            "open_url": None,
            "http_error_stage": None,
            "navigation_note": None,
            "navigation_method_used": None,
            "navigation_stage": None,
            "navigation_load_event_fired": False,
            "navigation_location_href": None,
            "navigation_ready_state": None,
            "navigation_target_url": None,
            "navigation_target_title": None,
            "navigation_active_chart_available": False,
            "navigation_error_message": None,
            "devtools_ws_connected": False,
            "requested_symbol": None,
            "current_tab_url": None,
            "current_tab_title": None,
            "symbol_verification_method": None,
            "symbol_navigation_attempted": False,
            "requested_tradingview_symbol": None,
            "previous_active_symbol": None,
            "active_symbol_before_set": None,
            "active_symbol_after_set": None,
            "active_symbol_after_stabilize": None,
            "loaded_symbol": None,
            "symbol_match": None,
            "symbol_stable_check_passed": None,
            "symbol_retry_count": None,
            "symbol_wait_seconds_used": None,
            "stale_previous_symbol_warning": None,
            "symbol_error_stage": None,
            "symbol_error_message": None,
            "reason": None,
            "error_stage": None,
            "candle_extraction_method": None,
            "candle_extraction_message": None,
            "tradingview_api_available": False,
            "active_chart_available": False,
            "chart_methods": [],
            "series_methods": [],
            "data_methods": [],
            "bars_type": None,
            "bars_count_raw": 0,
            "candles_count": 0,
            "candle_sample_keys": [],
            "candle_extraction_error": None,
            "runtime_evaluate_error": None,
            "requested_timeframe": None,
            "target_resolution": None,
            "resolution_before": None,
            "resolution_after": None,
            "resolution_match": None,
            "wait_seconds_used": None,
            "retry_count": None,
            "cdp_reconnect_count": 0,
            "cdp_last_exception": None,
            "timeout_location": None,
            "managed_tab_id": None,
            "managed_tab_count": 0,
            "attached_target_id": self.attached_target_id,
            "attachment_required": self.require_attached_tab,
        }
        self.owned_tab_ids: set[str] = set()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def set_deadline(self, timeout_seconds: int) -> None:
        self.deadline_monotonic = time.monotonic() + timeout_seconds

    def check_deadline(self, stage: str) -> None:
        if self.deadline_monotonic is not None and time.monotonic() >= self.deadline_monotonic:
            self.diagnostics["timeout_location"] = stage
            raise TimeoutError(f"TradingView symbol timeout at {stage}")

    def sleep(self, seconds: float, stage: str) -> None:
        self.check_deadline(stage)
        if self.deadline_monotonic is None:
            time.sleep(seconds)
        else:
            remaining = self.deadline_monotonic - time.monotonic()
            if remaining <= 0:
                self.check_deadline(stage)
            time.sleep(min(seconds, remaining))
        self.check_deadline(stage)

    def _request_json(self, path: str, method: str = "GET") -> Any:
        self.check_deadline(f"http:{path}")
        request = Request(f"{self.base_url}{path}", method=method)
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def _request_text(self, path: str) -> str:
        self.check_deadline(f"http:{path}")
        request = Request(f"{self.base_url}{path}", method="GET")
        with urlopen(request, timeout=5) as response:
            return response.read().decode("utf-8")

    def connect_to_debug_port(self) -> bool:
        self._request_json("/json/version")
        self.diagnostics["devtools_version_ok"] = True
        logger.info("TradingView CDP connected port=%d", self.port)
        return True

    def list_tabs(self) -> list[dict]:
        tabs = self._request_json("/json/list")
        tabs = tabs if isinstance(tabs, list) else []
        self.diagnostics["tabs_count"] = len(tabs)
        self.diagnostics["chart_tab_found"] = any(is_tradingview_tab(tab) for tab in tabs)
        self.diagnostics["managed_tab_count"] = sum(1 for tab in tabs if is_app_managed_tab(tab))
        return tabs

    def list_attachable_chart_targets(self) -> list[dict]:
        attachable = []
        for tab in self.list_tabs():
            if not is_real_tradingview_chart_target(tab):
                continue
            readiness = self.read_chart_readiness(tab)
            if not readiness.get("activeChartAvailable"):
                continue
            attachable.append(attachable_target_payload(tab, readiness))
        return attachable

    def read_chart_readiness(self, tab: dict) -> dict:
        try:
            value = self.evaluate_runtime_on_tab(tab, chart_readiness_expression())
            return value if isinstance(value, dict) else {}
        except Exception as exc:
            self.diagnostics["runtime_evaluate_error"] = str(exc)
            return {
                "href": tab.get("url"),
                "title": tab.get("title"),
                "readyState": None,
                "tradingviewApiAvailable": False,
                "activeChartAvailable": False,
                "error": str(exc),
            }

    def validate_attachable_target(self, target_id: str) -> dict | None:
        for target in self.list_attachable_chart_targets():
            if target.get("target_id") == target_id:
                return target
        return None

    def open_or_reuse_chart_tab(self) -> dict:
        if self.require_attached_tab or self.attached_target_id:
            return self.get_attached_chart_tab()
        tabs = self.list_tabs()
        managed_tabs = [tab for tab in tabs if is_app_managed_tab(tab)]
        if len(managed_tabs) > MAX_MANAGED_TABS:
            for stale_tab in managed_tabs[MAX_MANAGED_TABS:]:
                self.close_managed_tab(stale_tab)
        if managed_tabs:
            tab = managed_tabs[0]
            tab_id = tab.get("id")
            if tab_id:
                self._request_text(f"/json/activate/{tab_id}")
                self.diagnostics["managed_tab_id"] = tab_id
            return tab
        tab = self.create_managed_tab()
        tab_id = tab.get("id")
        if tab_id:
            self.diagnostics["managed_tab_id"] = tab_id
        return tab

    def get_attached_chart_tab(self) -> dict:
        if not self.attached_target_id:
            self.diagnostics["reason"] = "TV_TAB_NOT_ATTACHED"
            self.diagnostics["error_stage"] = "attached_tab_missing"
            raise TradingViewTabNotAttachedError("TV_TAB_NOT_ATTACHED")
        tab = self.find_tab_by_id(self.attached_target_id, attempts=1)
        if not tab or not tab.get("webSocketDebuggerUrl"):
            self.diagnostics["reason"] = "TV_TAB_DISCONNECTED"
            self.diagnostics["error_stage"] = "attached_tab_disconnected"
            self.diagnostics["managed_tab_id"] = self.attached_target_id
            raise TradingViewTabDisconnectedError(self.attached_target_id)
        if not is_real_tradingview_chart_target(tab):
            self.diagnostics["reason"] = "TV_TAB_DISCONNECTED"
            self.diagnostics["error_stage"] = "attached_tab_not_chart"
            self.diagnostics["managed_tab_id"] = self.attached_target_id
            raise TradingViewTabDisconnectedError(self.attached_target_id, "TV_TAB_DISCONNECTED: attached target is not a TradingView chart")
        self.diagnostics["managed_tab_id"] = self.attached_target_id
        self.diagnostics["current_tab_url"] = tab.get("url")
        self.diagnostics["current_tab_title"] = tab.get("title")
        return tab

    def create_managed_tab(self) -> dict:
        url = managed_tradingview_url()
        chart_url = quote(url, safe="")
        self.diagnostics["open_url"] = url
        self.diagnostics["json_new_method_used"] = "PUT"
        try:
            tab = self._request_json(f"/json/new?{chart_url}", method="PUT")
            self.register_owned_tab(tab)
            return tab
        except HTTPError as exc:
            self.diagnostics["http_error_stage"] = f"json_new_put_failed_http_{exc.code}"
            logger.warning("TradingView /json/new PUT failed status=%s; attach an existing chart tab instead", exc.code)
            self.diagnostics["reason"] = "TV_TAB_NOT_ATTACHED"
            self.diagnostics["error_stage"] = "managed_tab_creation_unsupported"
            raise TradingViewTabNotAttachedError("TV_TAB_NOT_ATTACHED") from exc

    def register_owned_tab(self, tab: dict | None) -> None:
        tab_id = tab.get("id") if isinstance(tab, dict) else None
        if tab_id:
            self.owned_tab_ids.add(str(tab_id))
            self.diagnostics["managed_tab_id"] = str(tab_id)
        register_managed_tab(tab)

    def wait_for_created_tab_by_id(self, tab_id: str, attempts: int = 10, require_tradingview: bool = False) -> dict | None:
        for _ in range(attempts):
            tab = self.find_tab_by_id(tab_id, attempts=1)
            if tab and tab.get("webSocketDebuggerUrl") and (not require_tradingview or is_tradingview_tab(tab)):
                return tab
            self.sleep(0.5, "wait_for_managed_tab_by_id")
        return None

    def close_managed_tab(self, tab: dict) -> bool:
        if not is_app_managed_tab(tab):
            return False
        tab_id = tab.get("id")
        if not tab_id:
            return False
        try:
            self._request_text(f"/json/close/{tab_id}")
            unregister_managed_tab_id(tab_id)
            return True
        except Exception as exc:
            logger.warning("TradingView managed tab close failed tab_id=%s error=%s", tab_id, exc)
            return False

    def managed_tab_is_valid(self, tab: dict | None) -> bool:
        if not tab or not is_app_managed_tab(tab):
            return False
        tab_id = tab.get("id")
        if not tab_id:
            return False
        return any(candidate.get("id") == tab_id for candidate in self.list_tabs())

    def ensure_managed_tab(self) -> dict:
        tab = self.open_or_reuse_chart_tab()
        if self.require_attached_tab or self.attached_target_id:
            return tab
        if not self.managed_tab_is_valid(tab):
            raise RuntimeError("TradingView managed tab is missing or invalid")
        return tab

    def open_legacy_or_reuse_chart_tab(self) -> dict:
        for tab in self.list_tabs():
            if is_tradingview_tab(tab):
                tab_id = tab.get("id")
                if tab_id:
                    self._request_text(f"/json/activate/{tab_id}")
                return tab
        chart_url = quote("https://www.tradingview.com/chart/", safe="")
        self.diagnostics["json_new_method_used"] = "PUT"
        self.diagnostics["open_url"] = "https://www.tradingview.com/chart/"
        try:
            return self._request_json(f"/json/new?{chart_url}", method="PUT")
        except HTTPError as exc:
            self.diagnostics["http_error_stage"] = "open_chart_tab_failed_reused_existing_tab"
            self.diagnostics["navigation_note"] = f"/json/new returned HTTP {exc.code}; reused existing tab when available"
            tabs = self.list_tabs()
            return tabs[0] if tabs else {}

    def open_symbol(self, tradingview_symbol: str) -> dict:
        validate_symbol(tradingview_symbol)
        tab = self.ensure_managed_tab()
        interval = SUPPORTED_TIMEFRAMES["1D"]
        self.diagnostics["requested_symbol"] = tradingview_symbol
        self.diagnostics["symbol_navigation_attempted"] = True

        # Try to use TradingViewApi first!
        try:
            res = self.evaluate_runtime(
                f"""
                (() => {{
                  const api = window.TradingViewApi;
                  const chart = api && typeof api.activeChart === 'function' ? api.activeChart() : null;
                  if (chart && typeof chart.setSymbol === 'function') {{
                    chart.setSymbol({json.dumps(tradingview_symbol)});
                    return true;
                  }}
                  return false;
                }})()
                """
            )
            if res is True:
                self.diagnostics["symbol_navigation_method"] = "api"
                return tab
        except Exception as e:
            logger.warning("Failed to set symbol via TradingViewApi: %s", e)

        url = tradingview_url(tradingview_symbol, interval)
        self.diagnostics["json_new_method_used"] = "GET"
        self.diagnostics["open_url"] = url
        self.diagnostics["symbol_navigation_method"] = "cdp_navigate"
        return self.navigate_with_cdp(tab, url, "open_symbol")

    def set_timeframe(self, timeframe: str) -> dict:
        interval = validate_timeframe(timeframe)
        tab = self.ensure_managed_tab()

        # Check if already active and matches
        current_res = self.get_active_chart_resolution()
        current_sym = self.get_active_chart_symbol()
        requested_sym = self.diagnostics.get("requested_tradingview_symbol")

        if (
            current_sym
            and requested_sym
            and self.symbol_matches(current_sym, requested_sym)
            and normalize_timeframe(current_res) == normalize_timeframe(timeframe)
        ):
            self.diagnostics["resolution_after"] = normalize_timeframe(current_res)
            self.diagnostics["resolution_match"] = True
            return tab

        self.diagnostics["requested_timeframe"] = timeframe
        self.diagnostics["target_resolution"] = interval
        self.diagnostics["resolution_before"] = normalize_timeframe(current_res)

        # Try to use TradingViewApi first!
        try:
            res = self.evaluate_runtime(
                f"""
                (() => {{
                  const api = window.TradingViewApi;
                  const chart = api && typeof api.activeChart === 'function' ? api.activeChart() : null;
                  if (chart && typeof chart.setResolution === 'function') {{
                    chart.setResolution({json.dumps(interval)});
                    return true;
                  }}
                  return false;
                }})()
                """
            )
            if res is True:
                self.diagnostics["resolution_navigation_method"] = "api"
                self.refresh_resolution_status(timeframe)
                return tab
        except Exception as e:
            logger.warning("Failed to set timeframe via TradingViewApi: %s", e)

        current_symbol = requested_sym or extract_symbol_from_url(tab.get("url", "")) or "NSE:RELIANCE"
        url = tradingview_url(current_symbol, interval)
        self.diagnostics["json_new_method_used"] = "GET"
        self.diagnostics["open_url"] = url
        self.diagnostics["resolution_navigation_method"] = "cdp_navigate"
        opened = self.navigate_with_cdp(tab, url, "set_timeframe")
        self.diagnostics["resolution_after"] = extract_interval_from_url(opened.get("url", ""))
        self.diagnostics["resolution_match"] = (
            normalize_timeframe(self.diagnostics["resolution_after"]) == normalize_timeframe(timeframe)
        )
        return opened

    def refresh_resolution_status(self, timeframe: str) -> None:
        requested_norm = normalize_timeframe(timeframe)
        self.diagnostics["target_resolution"] = requested_norm
        tab = self.open_or_reuse_chart_tab()
        chart_resolution = self.get_active_chart_resolution()
        active_norm = normalize_timeframe(chart_resolution or extract_interval_from_url(tab.get("url", "")))
        self.diagnostics["resolution_after"] = active_norm
        self.diagnostics["resolution_match"] = (active_norm == requested_norm)

    def get_active_chart_resolution(self) -> str | None:
        try:
            return self.evaluate_runtime(
                """
                (() => {
                  const chart = window.TradingViewApi && window.TradingViewApi.activeChart && window.TradingViewApi.activeChart();
                  if (!chart || typeof chart.resolution !== 'function') return null;
                  const value = chart.resolution();
                  return value == null ? null : String(value);
                })()
                """
            )
        except Exception as exc:
            self.diagnostics["runtime_evaluate_error"] = str(exc)
            return None

    def get_active_chart_symbol(self) -> str | None:
        try:
            value = self.evaluate_runtime(
                """
                (() => {
                  const api = window.TradingViewApi;
                  const chart = api && typeof api.activeChart === 'function' ? api.activeChart() : null;
                  const read = (value) => {
                    if (!value) return null;
                    if (typeof value === 'string') return value;
                    if (typeof value === 'object') return value.symbol || value.full_name || value.ticker || value.pro_name || null;
                    return null;
                  };
                  const candidates = [];
                  try { if (chart && typeof chart.symbol === 'function') candidates.push(read(chart.symbol())); } catch (error) {}
                  try { if (chart && typeof chart.symbolExt === 'function') candidates.push(read(chart.symbolExt())); } catch (error) {}
                  try { if (chart && typeof chart.mainSeries === 'function') candidates.push(read(chart.mainSeries().symbol && chart.mainSeries().symbol())); } catch (error) {}
                  try { candidates.push(new URL(location.href).searchParams.get('symbol')); } catch (error) {}
                  return candidates.find((item) => typeof item === 'string' && item.trim().length) || null;
                })()
                """
            )
            return normalize_symbol(value)
        except Exception as exc:
            self.diagnostics["runtime_evaluate_error"] = str(exc)
            return None
    def symbol_matches(self, active_symbol: str | None, requested_symbol: str) -> bool:
        return compare_symbols(active_symbol, requested_symbol)

    def load_symbol_strict(self, tradingview_symbol: str) -> bool:
        validate_symbol(tradingview_symbol)
        requested = normalize_symbol(tradingview_symbol)
        self.diagnostics["requested_tradingview_symbol"] = requested

        try:
            previous = self.get_active_chart_symbol()
        except (TradingViewTabNavigationError, TradingViewTabNotAttachedError, TradingViewTabDisconnectedError) as exc:
            self.diagnostics["symbol_error_stage"] = tv_tab_error_stage(exc)
            self.diagnostics["symbol_error_message"] = str(exc)
            self.diagnostics["symbol_match"] = False
            self.diagnostics["symbol_stable_check_passed"] = False
            self.diagnostics["symbol_retry_count"] = 0
            self.diagnostics["symbol_wait_seconds_used"] = 0
            return False

        if previous and self.symbol_matches(previous, requested):
            self.diagnostics["active_symbol_before_set"] = previous
            self.diagnostics["active_symbol_after_set"] = previous
            self.diagnostics["active_symbol_after_stabilize"] = previous
            self.diagnostics["loaded_symbol"] = previous
            self.diagnostics["symbol_match"] = True
            self.diagnostics["symbol_stable_check_passed"] = True
            self.diagnostics["symbol_retry_count"] = 0
            self.diagnostics["symbol_wait_seconds_used"] = 0
            self.diagnostics["symbol_error_stage"] = None
            self.diagnostics["symbol_error_message"] = None
            return True

        previous_is_different = bool(previous and not self.symbol_matches(previous, requested))
        self.diagnostics["previous_active_symbol"] = previous
        self.diagnostics["active_symbol_before_set"] = previous
        self.diagnostics["stale_previous_symbol_warning"] = False
        wait_used = 0

        max_attempts = settings.TRADINGVIEW_SYMBOL_RETRIES
        for attempt in range(1, max_attempts + 1):
            try:
                self.check_deadline(f"symbol_load_attempt_{attempt}")
                self.open_symbol(tradingview_symbol)

                deadline = time.time() + settings.TRADINGVIEW_SYMBOL_WAIT_SECONDS
                active_after_set = None
                verified_changed = False
                while time.time() <= deadline:
                    self.check_deadline("symbol_wait")
                    active_after_set = self.get_active_chart_symbol()
                    self.diagnostics["active_symbol_after_set"] = active_after_set
                    if self.symbol_matches(active_after_set, requested) and not (previous_is_different and active_after_set == previous):
                        verified_changed = True
                        break
                    self.sleep(0.2, "symbol_wait")
                    wait_used += 0.2

                if not verified_changed:
                    self.diagnostics["symbol_error_stage"] = "TV_SYMBOL_VERIFY_TIMEOUT"
                    self.diagnostics["symbol_error_message"] = f"Symbol loaded but verify timed out: requested={requested}, active={active_after_set}"
                    if attempt < max_attempts:
                        self.sleep(1.0, "symbol_retry_wait")
                        wait_used += 1.0
                        continue
                    return False

                self.sleep(settings.TRADINGVIEW_SYMBOL_STABILIZE_SECONDS, "symbol_stabilize")
                wait_used += settings.TRADINGVIEW_SYMBOL_STABILIZE_SECONDS
                active_after_stabilize = self.get_active_chart_symbol()
                self.diagnostics["active_symbol_after_stabilize"] = active_after_stabilize
                self.diagnostics["loaded_symbol"] = active_after_stabilize

                symbol_match = self.symbol_matches(active_after_set, requested) and self.symbol_matches(active_after_stabilize, requested)
                stable = active_after_set == active_after_stabilize and symbol_match and not (previous_is_different and active_after_stabilize == previous)

                self.diagnostics["symbol_match"] = symbol_match
                self.diagnostics["symbol_stable_check_passed"] = stable
                self.diagnostics["symbol_retry_count"] = attempt - 1
                self.diagnostics["symbol_wait_seconds_used"] = wait_used
                self.diagnostics["stale_previous_symbol_warning"] = (previous_is_different and active_after_stabilize == previous) or not stable

                if stable:
                    self.diagnostics["symbol_error_stage"] = None
                    self.diagnostics["symbol_error_message"] = None
                    return True

                if active_after_stabilize and not self.symbol_matches(active_after_stabilize, requested):
                    self.diagnostics["symbol_error_stage"] = "TV_SYMBOL_VERIFY_TIMEOUT"
                    self.diagnostics["symbol_error_message"] = f"requested_symbol={requested}, loaded_symbol={active_after_stabilize}"
                elif previous_is_different and active_after_stabilize == previous:
                    self.diagnostics["symbol_error_stage"] = "TV_SYMBOL_VERIFY_TIMEOUT"
                    self.diagnostics["symbol_error_message"] = f"requested_symbol={requested}, stale_previous_symbol={previous}"
                else:
                    self.diagnostics["symbol_error_stage"] = "TV_SYMBOL_SET_TIMEOUT"
                    self.diagnostics["symbol_error_message"] = f"requested_symbol={requested}, loaded_symbol={active_after_stabilize}"

            except (TradingViewTabNavigationError, TradingViewTabNotAttachedError, TradingViewTabDisconnectedError) as exc:
                self.diagnostics["symbol_error_stage"] = tv_tab_error_stage(exc)
                self.diagnostics["symbol_error_message"] = str(exc)
                self.diagnostics["symbol_match"] = False
                self.diagnostics["symbol_stable_check_passed"] = False
                self.diagnostics["symbol_retry_count"] = attempt - 1
                self.diagnostics["symbol_wait_seconds_used"] = wait_used
                return False
            except Exception as exc:
                self.diagnostics["symbol_error_stage"] = "TV_SYMBOL_SET_TIMEOUT"
                self.diagnostics["symbol_error_message"] = str(exc)

            if attempt < max_attempts:
                self.sleep(1.0, "symbol_retry_wait")
                wait_used += 1.0

        return False

    def wait_for_resolution(self, timeframe: str, timeout_seconds: int = 10) -> bool:
        requested_norm = normalize_timeframe(timeframe)
        deadline = time.time() + timeout_seconds
        while time.time() <= deadline:
            self.check_deadline(f"resolution_wait:{timeframe}")
            self.refresh_resolution_status(timeframe)
            active_norm = normalize_timeframe(self.diagnostics.get("resolution_after"))
            if active_norm == requested_norm:
                self.diagnostics["resolution_match"] = True
                return True
            self.sleep(0.2, f"resolution_wait:{timeframe}")
        self.refresh_resolution_status(timeframe)
        active_norm = normalize_timeframe(self.diagnostics.get("resolution_after"))
        self.diagnostics["resolution_match"] = (active_norm == requested_norm)
        return bool(self.diagnostics["resolution_match"])

    def navigate_with_cdp(self, tab: dict, url: str, stage: str) -> dict:
        last_error = None
        for attempt in range(2):
            current_tab = tab if attempt == 0 else self.open_or_reuse_chart_tab()
            try:
                self.check_deadline(f"cdp_navigate:{stage}")
                return self.navigate_owned_target_to_tradingview(current_tab, url, stage)
            except TradingViewTabNavigationError:
                raise
            except Exception as exc:
                last_error = exc
            self.diagnostics["cdp_last_exception"] = str(last_error)
            logger.warning("TradingView CDP navigation failed stage=%s attempt=%d error=%s", stage, attempt + 1, last_error)
            if attempt == 0:
                self.diagnostics["cdp_reconnect_count"] += 1
        self.diagnostics["navigation_method_used"] = "reused_tab_only"
        self.diagnostics["error_stage"] = f"{stage}:cdp_page_navigate_failed"
        self.diagnostics["reason"] = str(last_error)
        return tab

    def navigate_owned_target_to_tradingview(self, tab: dict, url: str, stage: str) -> dict:
        tab_id = tab.get("id")
        ws_url = tab.get("webSocketDebuggerUrl")
        if not tab_id:
            raise TradingViewTabNavigationError("TAB_NAVIGATION_FAILED: managed tab is missing an id")
        if not ws_url:
            raise TradingViewTabNavigationError("TAB_NAVIGATION_FAILED: missing WebSocket debugger URL for managed tab")

        self.diagnostics["navigation_stage"] = stage
        self.diagnostics["navigation_method_used"] = "cdp_page_navigate"
        self.diagnostics["navigation_load_event_fired"] = False
        self.diagnostics["navigation_error_message"] = None
        deadline = time.monotonic() + CHART_NAVIGATION_TIMEOUT_SECONDS
        next_eval_at = 0.0
        eval_call_id = None
        next_call_id = 1

        def send_command(websocket, method: str, params: dict | None = None) -> int:
            nonlocal next_call_id
            call_id = next_call_id
            next_call_id += 1
            websocket.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
            return call_id

        readiness = {}
        with ws_connect(ws_url, open_timeout=5) as websocket:
            self.diagnostics["devtools_ws_connected"] = True
            logger.info("TradingView CDP websocket connected method=Page.navigate stage=%s", stage)
            send_command(websocket, "Page.enable")
            send_command(websocket, "Runtime.enable")
            navigate_call_id = send_command(websocket, "Page.navigate", {"url": url})
            while time.monotonic() <= deadline:
                self.check_deadline(f"cdp_navigate_wait:{stage}")
                now = time.monotonic()
                if eval_call_id is None and now >= next_eval_at:
                    eval_call_id = send_command(
                        websocket,
                        "Runtime.evaluate",
                        {
                            "expression": chart_readiness_expression(),
                            "returnByValue": True,
                            "awaitPromise": True,
                        },
                    )
                    next_eval_at = now + 1
                try:
                    message = json.loads(websocket.recv(timeout=CDP_RECV_POLL_SECONDS))
                except TimeoutError:
                    message = {}
                if message.get("method") == "Page.loadEventFired":
                    self.diagnostics["navigation_load_event_fired"] = True
                if message.get("id") == navigate_call_id and "error" in message:
                    error_message = message["error"].get("message", "Page.navigate failed")
                    self.diagnostics["navigation_error_message"] = error_message
                    raise TradingViewTabNavigationError(f"TAB_NAVIGATION_FAILED: {error_message}")
                if eval_call_id is not None and message.get("id") == eval_call_id:
                    if "error" in message:
                        self.diagnostics["runtime_evaluate_error"] = message["error"].get("message", "Runtime.evaluate failed")
                    else:
                        readiness = message.get("result", {}).get("result", {}).get("value") or {}
                        self.refresh_navigation_diagnostics(readiness, tab_id)
                        if self.chart_navigation_ready(readiness, tab_id):
                            return self.find_tab_by_id(str(tab_id), attempts=1) or tab
                    eval_call_id = None
                self.refresh_navigation_diagnostics(readiness, tab_id)
                if self.chart_navigation_ready(readiness, tab_id):
                    return self.find_tab_by_id(str(tab_id), attempts=1) or tab

        self.diagnostics["navigation_error_message"] = "TradingView chart did not become ready"
        raise TradingViewTabNavigationError("TAB_NAVIGATION_FAILED: TradingView chart did not become ready")

    def refresh_navigation_diagnostics(self, readiness: dict, tab_id: str) -> dict | None:
        tab = self.find_tab_by_id(str(tab_id), attempts=1)
        target_url = tab.get("url") if tab else None
        target_title = tab.get("title") if tab else None
        self.diagnostics["navigation_location_href"] = readiness.get("href") or self.diagnostics.get("navigation_location_href")
        self.diagnostics["navigation_ready_state"] = readiness.get("readyState") or self.diagnostics.get("navigation_ready_state")
        self.diagnostics["navigation_target_url"] = target_url
        self.diagnostics["navigation_target_title"] = target_title
        self.diagnostics["navigation_active_chart_available"] = bool(readiness.get("activeChartAvailable"))
        return tab

    def chart_navigation_ready(self, readiness: dict, tab_id: str) -> bool:
        tab = self.find_tab_by_id(str(tab_id), attempts=1)
        href = readiness.get("href")
        ready_state = readiness.get("readyState")
        target_url = tab.get("url") if tab else None
        target_title = tab.get("title") if tab else None
        url_or_title_is_tv = is_tradingview_location(href, readiness.get("title")) or is_tradingview_location(target_url, target_title)
        no_longer_blank = not is_blank_url(href) or not is_blank_url(target_url)
        return bool(no_longer_blank and url_or_title_is_tv and ready_state in {"interactive", "complete"} and readiness.get("activeChartAvailable"))

    def cdp_call(self, tab: dict, method: str, params: dict | None = None, call_id: int = 1) -> dict:
        last_error = None
        for attempt in range(2):
            self.check_deadline(f"cdp:{method}")
            current_tab = tab if attempt == 0 else self.open_or_reuse_chart_tab()
            ws_url = current_tab.get("webSocketDebuggerUrl")
            if not ws_url:
                last_error = RuntimeError("Missing webSocketDebuggerUrl for TradingView tab")
            else:
                try:
                    with ws_connect(ws_url, open_timeout=5) as websocket:
                        self.diagnostics["devtools_ws_connected"] = True
                        logger.info("TradingView CDP websocket connected method=%s attempt=%d", method, attempt + 1)
                        websocket.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
                        while True:
                            self.check_deadline(f"cdp_recv:{method}")
                            message = json.loads(websocket.recv(timeout=15))
                            if message.get("id") == call_id:
                                return message
                except Exception as exc:
                    last_error = exc
            self.diagnostics["cdp_last_exception"] = str(last_error)
            logger.warning("TradingView CDP call failed method=%s attempt=%d error=%s", method, attempt + 1, last_error)
            if attempt == 0:
                self.diagnostics["cdp_reconnect_count"] += 1
        raise last_error

    def cdp_call_on_tab(self, tab: dict, method: str, params: dict | None = None, call_id: int = 1) -> dict:
        ws_url = tab.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("Missing webSocketDebuggerUrl for TradingView tab")
        self.check_deadline(f"cdp:{method}")
        with ws_connect(ws_url, open_timeout=5) as websocket:
            self.diagnostics["devtools_ws_connected"] = True
            websocket.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
            while True:
                self.check_deadline(f"cdp_recv:{method}")
                message = json.loads(websocket.recv(timeout=15))
                if message.get("id") == call_id:
                    return message

    def evaluate_runtime_on_tab(self, tab: dict, expression: str) -> Any:
        response = self.cdp_call_on_tab(
            tab,
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if "exceptionDetails" in response:
            details = response["exceptionDetails"].get("text", "Runtime.evaluate failed")
            self.diagnostics["runtime_evaluate_error"] = details
            raise RuntimeError(details)
        return response.get("result", {}).get("result", {}).get("value")

    def evaluate_runtime(self, expression: str) -> Any:
        tab = self.open_or_reuse_chart_tab()
        response = self.cdp_call(
            tab,
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if "exceptionDetails" in response:
            details = response["exceptionDetails"].get("text", "Runtime.evaluate failed")
            self.diagnostics["runtime_evaluate_error"] = details
            raise RuntimeError(details)
        return response.get("result", {}).get("result", {}).get("value")

    def extract_candles_from_active_chart(
        self,
        initial_wait_seconds: int = 2,
        retry_wait_seconds: int = 2,
        max_attempts: int = 10,
    ) -> list[dict]:
        self.diagnostics["candle_extraction_method"] = "TradingViewApi.activeChart.getSeries.series.data.bars"
        expression = """
        (() => {
          const isNumber = (value) => typeof value === 'number' && Number.isFinite(value);
          const methodNames = (obj) => {
            if (!obj) return [];
            const names = new Set();
            let cur = obj;
            for (let i = 0; cur && i < 4; i++, cur = Object.getPrototypeOf(cur)) {
              try { Object.getOwnPropertyNames(cur).forEach((name) => names.add(name)); } catch (error) {}
            }
            return Array.from(names).filter((name) => typeof obj[name] === 'function').slice(0, 80);
          };
          const normalize = (item) => {
            if (!item || typeof item !== 'object') return null;
            const row = Array.isArray(item.value) ? item.value : item;
            const time = item.time ?? item.timestamp ?? row[0];
            const open = item.open ?? item.o ?? row[1];
            const high = item.high ?? item.h ?? row[2];
            const low = item.low ?? item.l ?? row[3];
            const close = item.close ?? item.c ?? row[4];
            const volume = item.volume ?? item.v ?? row[5] ?? null;
            if ([open, high, low, close].every(isNumber)) {
              return {time, open, high, low, close, volume};
            }
            return null;
          };
          const out = {
            tradingview_api_available: !!window.TradingViewApi,
            active_chart_available: false,
            chart_methods: [],
            series_methods: [],
            data_methods: [],
            bars_type: null,
            bars_count_raw: 0,
            candles: [],
            candle_sample_keys: [],
            candle_extraction_error: null,
            title: document.title,
            url: location.href
          };
          try {
            const api = window.TradingViewApi;
            const chart = api && typeof api.activeChart === 'function' ? api.activeChart() : null;
            out.active_chart_available = !!chart;
            out.chart_methods = methodNames(chart);
            const getSeriesResult = chart && typeof chart.getSeries === 'function' ? chart.getSeries() : null;
            out.series_methods = methodNames(getSeriesResult);
            const series = getSeriesResult && typeof getSeriesResult.series === 'function' ? getSeriesResult.series() : null;
            const data = series && typeof series.data === 'function' ? series.data() : null;
            out.data_methods = methodNames(data);
            let bars = data && typeof data.bars === 'function' ? data.bars() : null;
            out.bars_type = bars === null ? null : Object.prototype.toString.call(bars);
            if (bars && Array.isArray(bars._items)) bars = bars._items;
            else if (bars && typeof bars.values === 'function') bars = Array.from(bars.values());
            else if (bars && !Array.isArray(bars) && typeof bars === 'object') bars = Object.values(bars);
            if (Array.isArray(bars)) {
              out.bars_count_raw = bars.length;
              out.candles = bars.map(normalize).filter(Boolean);
              out.candle_sample_keys = bars[0] && typeof bars[0] === 'object' ? Object.keys(bars[0]).slice(0, 30) : [];
            }
          } catch (error) {
            out.candle_extraction_error = String(error && error.stack ? error.stack : error);
          }
          return out;
        })()
        """
        result = {}
        candles = []
        attempts = 0
        for _ in range(max_attempts):
            wait_seconds = initial_wait_seconds if attempts == 0 else retry_wait_seconds
            if wait_seconds:
                self.sleep(wait_seconds, "candle_extract_wait")
            attempts += 1
            result = self.evaluate_runtime(expression) or {}
            candles = result.get("candles") or []
            if candles:
                break

        # Validate candles in Python
        validated_candles = []
        seen_timestamps = set()
        for c in candles:
            if not isinstance(c, dict):
                continue
            time_val = c.get("time")
            if time_val is None:
                continue
            try:
                time_val = float(time_val)
            except (ValueError, TypeError):
                continue

            open_val = c.get("open")
            high_val = c.get("high")
            low_val = c.get("low")
            close_val = c.get("close")
            volume_val = c.get("volume")

            try:
                open_val = float(open_val)
                high_val = float(high_val)
                low_val = float(low_val)
                close_val = float(close_val)
                if volume_val is not None:
                    volume_val = float(volume_val)
            except (ValueError, TypeError):
                continue

            if high_val < low_val:
                continue

            # Handle future timestamp safely
            if time_val > time.time() + 86400:
                continue

            if time_val in seen_timestamps:
                continue
            seen_timestamps.add(time_val)

            validated_candles.append({
                "time": int(time_val),
                "open": open_val,
                "high": high_val,
                "low": low_val,
                "close": close_val,
                "volume": volume_val
            })

        validated_candles.sort(key=lambda x: x["time"])
        candles = validated_candles[:2000]

        for key in (
            "tradingview_api_available",
            "active_chart_available",
            "chart_methods",
            "series_methods",
            "data_methods",
            "bars_type",
            "bars_count_raw",
            "candle_sample_keys",
            "candle_extraction_error",
        ):
            self.diagnostics[key] = result.get(key)
        self.diagnostics["candles_count"] = len(candles)
        self.diagnostics["candle_extraction_message"] = (
            f"Extracted {len(candles)} candles" if candles else "CANDLES_NOT_FOUND: TradingViewApi bars returned no OHLCV candles"
        )
        self.diagnostics["wait_seconds_used"] = initial_wait_seconds + max(attempts - 1, 0) * retry_wait_seconds
        self.diagnostics["retry_count"] = max(attempts - 1, 0)
        return candles

    def fetch_candles(self, timeframe: str, min_candles: int = 50) -> list[dict]:
        self.set_timeframe(timeframe)
        return self.extract_candles_from_active_chart()

    def find_tab_by_url(self, url: str) -> dict | None:
        for tab in self.list_tabs():
            if tab.get("url") == url:
                return tab
        return None

    def find_tab_by_id(self, tab_id: str, attempts: int = 5) -> dict | None:
        for _ in range(attempts):
            for tab in self.list_tabs():
                if tab.get("id") == tab_id:
                    return tab
            self.sleep(0.5, "find_tab_by_id")
        return None

    def verify_symbol_loaded(self, tradingview_symbol: str) -> bool:
        expected = quote(tradingview_symbol, safe="")
        plain_expected = tradingview_symbol
        for tab in self.list_tabs():
            url = tab.get("url", "")
            title = tab.get("title", "")
            self.diagnostics["current_tab_url"] = url
            self.diagnostics["current_tab_title"] = title
            if "tradingview.com" in url and (expected in url or plain_expected in url or plain_expected in title):
                self.diagnostics["symbol_verification_method"] = "url_or_title"
                self.diagnostics["reason"] = None
                return True
        self.diagnostics["symbol_verification_method"] = "url_or_title"
        self.diagnostics["reason"] = "verification_not_implemented"
        return False

    def fetch_visible_chart_status(self) -> dict:
        tab = self.open_or_reuse_chart_tab()
        return {
            "tab_id": tab.get("id"),
            "title": tab.get("title"),
            "url": tab.get("url"),
        }


def validate_symbol(tradingview_symbol: str) -> None:
    if not (tradingview_symbol.startswith("NSE:") or tradingview_symbol.startswith("BSE:")):
        raise ValueError("TradingView symbol must start with NSE: or BSE:")
    if len(tradingview_symbol.split(":", 1)[1].strip()) == 0:
        raise ValueError("TradingView symbol is missing the symbol value")


def is_tradingview_tab(tab: dict) -> bool:
    url = tab.get("url", "").lower()
    title = tab.get("title", "").lower()
    return "tradingview.com/chart" in url or "tradingview" in url or "tradingview" in title


def is_tradingview_location(url: object, title: object = None) -> bool:
    text = f"{url or ''} {title or ''}".lower()
    return "tradingview.com/chart" in text or "tradingview" in text


def is_real_tradingview_chart_target(tab: dict) -> bool:
    if tab.get("type") != "page":
        return False
    url = str(tab.get("url", "")).strip().lower()
    if is_blank_url(url):
        return False
    return "tradingview.com/chart" in url


def is_blank_url(url: object) -> bool:
    value = str(url or "").strip().lower()
    return value == "" or value == "about:blank"


def chart_readiness_expression() -> str:
    return """
    (() => {
      const api = window.TradingViewApi;
      const chart = api && typeof api.activeChart === 'function' ? api.activeChart() : null;
      return {
        href: String(window.location && window.location.href || ''),
        title: String(document && document.title || ''),
        readyState: String(document && document.readyState || ''),
        tradingviewApiAvailable: !!api,
        activeChartAvailable: !!chart
      };
    })()
    """


def attachable_target_payload(tab: dict, readiness: dict) -> dict:
    return {
        "target_id": tab.get("id"),
        "title": tab.get("title"),
        "url": tab.get("url"),
        "websocket_debugger_url": tab.get("webSocketDebuggerUrl"),
        "ready": bool(readiness.get("activeChartAvailable")),
        "chart_readiness": readiness,
    }


def tv_tab_error_stage(exc: Exception) -> str:
    if isinstance(exc, TradingViewTabNotAttachedError):
        return "TV_TAB_NOT_ATTACHED"
    if isinstance(exc, TradingViewTabDisconnectedError):
        return "TV_TAB_DISCONNECTED"
    return "TAB_NAVIGATION_FAILED"


def register_managed_tab(tab: dict | None) -> None:
    tab_id = tab.get("id") if isinstance(tab, dict) else None
    if tab_id:
        MANAGED_TAB_IDS.add(str(tab_id))


def unregister_managed_tab_id(tab_id: object) -> None:
    if tab_id:
        MANAGED_TAB_IDS.discard(str(tab_id))


def is_app_managed_tab(tab: dict) -> bool:
    tab_id = tab.get("id")
    return MANAGED_TAB_MARKER in str(tab.get("url", "")) or (bool(tab_id) and str(tab_id) in MANAGED_TAB_IDS)


def validate_timeframe(timeframe: str) -> str:
    norm = normalize_timeframe(timeframe)
    if norm not in SUPPORTED_TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe: {timeframe}. Use 1W, 1D, 4H, or 1H")
    return SUPPORTED_TIMEFRAMES[norm]


def tradingview_url(tradingview_symbol: str, interval: str) -> str:
    return f"https://www.tradingview.com/chart/?symbol={quote(tradingview_symbol, safe='')}&interval={interval}&{MANAGED_TAB_MARKER}"


def managed_tradingview_url() -> str:
    return f"https://www.tradingview.com/chart/?{MANAGED_TAB_MARKER}"


def extract_symbol_from_url(url: str) -> str | None:
    try:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        if "symbol" in params and params["symbol"]:
            return params["symbol"][0]
        return None
    except Exception:
        return None


def normalize_symbol(value: object) -> str | None:
    if value is None:
        return None
    val = str(value).strip()
    if not val:
        return None
    try:
        val = unquote(val)
    except Exception:
        pass
    val = val.replace("%3A", ":").replace("%3a", ":")
    parts = [p.strip() for p in val.split("·") if p.strip()]
    if not parts:
        return None
    main_token = parts[0]
    main_token = main_token.split(" ")[0].strip()

    exchange = None
    symbol = main_token
    if ":" in main_token:
        ex_part, sym_part = main_token.split(":", 1)
        if ex_part.strip().upper() in {"NSE", "BSE"}:
            exchange = ex_part.strip().upper()
            symbol = sym_part.strip().upper()

    if not exchange:
        for p in parts[1:]:
            p_upper = p.upper().strip()
            if p_upper in {"NSE", "BSE"}:
                exchange = p_upper
                break

    from utils.symbol_utils import normalize_symbol as base_normalize
    symbol = base_normalize(exchange or "NSE", symbol)

    if exchange:
        return f"{exchange}:{symbol}"
    return symbol


def compare_symbols(active: str | None, requested: str | None) -> bool:
    if not active or not requested:
        return False
    norm_active = normalize_symbol(active)
    norm_requested = normalize_symbol(requested)
    if not norm_active or not norm_requested:
        return False
    if ":" in norm_active and ":" in norm_requested:
        return norm_active == norm_requested
    active_sym = norm_active.split(":", 1)[-1]
    requested_sym = norm_requested.split(":", 1)[-1]
    return active_sym == requested_sym


def normalize_timeframe(tf: str | None) -> str | None:
    if not tf:
        return None
    val = str(tf).strip().upper()
    if val.endswith("S"):
        val = val[:-1]
    mapping = {
        "1W": "1W",
        "W": "1W",
        "1WEEK": "1W",
        "WEEK": "1W",
        "1D": "1D",
        "D": "1D",
        "1DAY": "1D",
        "DAY": "1D",
        "4H": "4H",
        "240": "4H",
        "4HOUR": "4H",
        "1H": "1H",
        "60": "1H",
        "1HOUR": "1H",
    }
    cleaned_val = val.replace(" ", "")
    if cleaned_val in mapping:
        return mapping[cleaned_val]
    if "WEEK" in val or val == "W":
        return "1W"
    if "DAY" in val or val == "D":
        return "1D"
    if "240" in val or "4H" in val:
        return "4H"
    if "60" in val or "1H" in val:
        return "1H"
    return cleaned_val


def extract_interval_from_url(url: str) -> str | None:
    marker = "interval="
    if marker not in url:
        return None
    return url.split(marker, 1)[1].split("&", 1)[0]


def connect_to_debug_port(port: int = settings.TRADINGVIEW_DEBUG_PORT) -> bool:
    require_manager_available("connect_to_debug_port")
    return TradingViewClient(port).connect_to_debug_port()


def list_tabs(port: int = settings.TRADINGVIEW_DEBUG_PORT) -> list[dict]:
    require_manager_available("list_tabs")
    return TradingViewClient(port).list_tabs()


def open_or_reuse_chart_tab(port: int = settings.TRADINGVIEW_DEBUG_PORT) -> dict:
    require_manager_available("open_or_reuse_chart_tab")
    return TradingViewClient(port).open_or_reuse_chart_tab()


def open_symbol(tradingview_symbol: str, port: int = settings.TRADINGVIEW_DEBUG_PORT) -> dict:
    require_manager_available("open_symbol")
    return TradingViewClient(port).open_symbol(tradingview_symbol)


def set_timeframe(timeframe: str, port: int = settings.TRADINGVIEW_DEBUG_PORT) -> dict:
    require_manager_available("set_timeframe")
    return TradingViewClient(port).set_timeframe(timeframe)


def verify_symbol_loaded(tradingview_symbol: str, port: int = settings.TRADINGVIEW_DEBUG_PORT) -> bool:
    require_manager_available("verify_symbol_loaded")
    return TradingViewClient(port).verify_symbol_loaded(tradingview_symbol)


def fetch_visible_chart_status(port: int = settings.TRADINGVIEW_DEBUG_PORT) -> dict:
    require_manager_available("fetch_visible_chart_status")
    return TradingViewClient(port).fetch_visible_chart_status()


def fetch_candles(timeframe: str, min_candles: int = 50, port: int = settings.TRADINGVIEW_DEBUG_PORT) -> list[dict]:
    require_manager_available("fetch_candles")
    return TradingViewClient(port).fetch_candles(timeframe, min_candles)
