import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen

from websockets.sync.client import connect as ws_connect

from config import settings


SUPPORTED_TIMEFRAMES = {"1W": "W", "1D": "D", "4H": "240", "1H": "60"}


@dataclass
class TradingViewClient:
    port: int = settings.TRADINGVIEW_DEBUG_PORT

    def __post_init__(self) -> None:
        self.diagnostics = {
            "devtools_version_ok": False,
            "tabs_count": 0,
            "chart_tab_found": False,
            "json_new_method_used": None,
            "open_url": None,
            "http_error_stage": None,
            "navigation_note": None,
            "navigation_method_used": None,
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
        }

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _request_json(self, path: str) -> Any:
        request = Request(f"{self.base_url}{path}", method="GET")
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def _request_text(self, path: str) -> str:
        request = Request(f"{self.base_url}{path}", method="GET")
        with urlopen(request, timeout=5) as response:
            return response.read().decode("utf-8")

    def connect_to_debug_port(self) -> bool:
        self._request_json("/json/version")
        self.diagnostics["devtools_version_ok"] = True
        return True

    def list_tabs(self) -> list[dict]:
        tabs = self._request_json("/json/list")
        tabs = tabs if isinstance(tabs, list) else []
        self.diagnostics["tabs_count"] = len(tabs)
        self.diagnostics["chart_tab_found"] = any(is_tradingview_tab(tab) for tab in tabs)
        return tabs

    def open_or_reuse_chart_tab(self) -> dict:
        for tab in self.list_tabs():
            if is_tradingview_tab(tab):
                tab_id = tab.get("id")
                if tab_id:
                    self._request_text(f"/json/activate/{tab_id}")
                return tab
        chart_url = quote("https://www.tradingview.com/chart/", safe="")
        self.diagnostics["json_new_method_used"] = "GET"
        self.diagnostics["open_url"] = "https://www.tradingview.com/chart/"
        try:
            return self._request_json(f"/json/new?{chart_url}")
        except HTTPError as exc:
            self.diagnostics["http_error_stage"] = "open_chart_tab_failed_reused_existing_tab"
            self.diagnostics["navigation_note"] = f"/json/new returned HTTP {exc.code}; reused existing tab when available"
            tabs = self.list_tabs()
            return tabs[0] if tabs else {}

    def open_symbol(self, tradingview_symbol: str) -> dict:
        validate_symbol(tradingview_symbol)
        tab = self.open_or_reuse_chart_tab()
        interval = SUPPORTED_TIMEFRAMES["1D"]
        url = tradingview_url(tradingview_symbol, interval)
        self.diagnostics["requested_symbol"] = tradingview_symbol
        self.diagnostics["symbol_navigation_attempted"] = True
        encoded_url = quote(url, safe="")
        self.diagnostics["json_new_method_used"] = "GET"
        self.diagnostics["open_url"] = url
        try:
            opened = self._request_json(f"/json/new?{encoded_url}")
            self.diagnostics["navigation_method_used"] = "json_new"
            return opened
        except HTTPError as exc:
            self.diagnostics["http_error_stage"] = None
            self.diagnostics["navigation_note"] = f"/json/new returned HTTP {exc.code}; trying CDP Page.navigate"
            return self.navigate_with_cdp(tab, url, "open_symbol")

    def set_timeframe(self, timeframe: str) -> dict:
        interval = validate_timeframe(timeframe)
        tab = self.open_or_reuse_chart_tab()
        current_symbol = self.diagnostics.get("requested_tradingview_symbol") or extract_symbol_from_url(tab.get("url", "")) or "NSE:RELIANCE"
        url = tradingview_url(current_symbol, interval)
        encoded_url = quote(url, safe="")
        self.diagnostics["requested_timeframe"] = timeframe
        self.diagnostics["target_resolution"] = interval
        self.diagnostics["resolution_before"] = extract_interval_from_url(tab.get("url", ""))
        self.diagnostics["json_new_method_used"] = "GET"
        self.diagnostics["open_url"] = url
        try:
            opened = self._request_json(f"/json/new?{encoded_url}")
            self.diagnostics["navigation_method_used"] = "json_new"
            self.diagnostics["resolution_after"] = extract_interval_from_url(opened.get("url", ""))
            self.diagnostics["resolution_match"] = (
                self.diagnostics["resolution_after"] == interval
                if self.diagnostics["resolution_after"] is not None
                else None
            )
            return opened
        except HTTPError as exc:
            self.diagnostics["http_error_stage"] = None
            self.diagnostics["navigation_note"] = f"/json/new returned HTTP {exc.code}; trying CDP Page.navigate"
            opened = self.navigate_with_cdp(tab, url, "set_timeframe")
            self.diagnostics["resolution_after"] = extract_interval_from_url(opened.get("url", ""))
            self.diagnostics["resolution_match"] = (
                self.diagnostics["resolution_after"] == interval
                if self.diagnostics["resolution_after"] is not None
                else None
            )
            return opened

    def refresh_resolution_status(self, timeframe: str) -> None:
        interval = validate_timeframe(timeframe)
        self.diagnostics["target_resolution"] = interval
        tab = self.open_or_reuse_chart_tab()
        chart_resolution = self.get_active_chart_resolution()
        self.diagnostics["resolution_after"] = chart_resolution or extract_interval_from_url(tab.get("url", ""))
        self.diagnostics["resolution_match"] = (
            self.diagnostics["resolution_after"] == interval
            if self.diagnostics["resolution_after"] is not None
            else None
        )

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
        active = normalize_symbol(active_symbol)
        requested = normalize_symbol(requested_symbol)
        return bool(active and requested and (active == requested or active == requested.split(":", 1)[-1]))

    def load_symbol_strict(self, tradingview_symbol: str) -> bool:
        validate_symbol(tradingview_symbol)
        requested = normalize_symbol(tradingview_symbol)
        previous = self.get_active_chart_symbol()
        previous_is_different = bool(previous and not self.symbol_matches(previous, requested))
        self.diagnostics["requested_tradingview_symbol"] = requested
        self.diagnostics["previous_active_symbol"] = previous
        self.diagnostics["active_symbol_before_set"] = previous
        self.diagnostics["stale_previous_symbol_warning"] = False
        wait_used = 0
        for attempt in range(1, settings.TRADINGVIEW_SYMBOL_RETRIES + 1):
            try:
                self.open_symbol(tradingview_symbol)
                deadline = time.time() + settings.TRADINGVIEW_SYMBOL_WAIT_SECONDS
                active_after_set = None
                while time.time() <= deadline:
                    active_after_set = self.get_active_chart_symbol()
                    self.diagnostics["active_symbol_after_set"] = active_after_set
                    if self.symbol_matches(active_after_set, requested) and not (previous_is_different and active_after_set == previous):
                        break
                    time.sleep(1)
                    wait_used += 1
                time.sleep(settings.TRADINGVIEW_SYMBOL_STABILIZE_SECONDS)
                wait_used += settings.TRADINGVIEW_SYMBOL_STABILIZE_SECONDS
                active_after_stabilize = self.get_active_chart_symbol()
                self.diagnostics["active_symbol_after_stabilize"] = active_after_stabilize
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
                self.diagnostics["symbol_error_stage"] = "SYMBOL_LOAD_FAILED_OR_STALE_PREVIOUS_SYMBOL"
                self.diagnostics["symbol_error_message"] = f"active_symbol={active_after_stabilize}, previous_symbol={previous}"
            except Exception as exc:
                self.diagnostics["symbol_error_stage"] = "SYMBOL_LOAD_FAILED_OR_STALE_PREVIOUS_SYMBOL"
                self.diagnostics["symbol_error_message"] = str(exc)
            if attempt < settings.TRADINGVIEW_SYMBOL_RETRIES:
                time.sleep(2)
                wait_used += 2
        return False

    def wait_for_resolution(self, timeframe: str, timeout_seconds: int = 10) -> bool:
        interval = validate_timeframe(timeframe)
        deadline = time.time() + timeout_seconds
        while time.time() <= deadline:
            self.refresh_resolution_status(timeframe)
            if self.diagnostics.get("resolution_after") == interval:
                self.diagnostics["resolution_match"] = True
                return True
            time.sleep(1)
        self.refresh_resolution_status(timeframe)
        self.diagnostics["resolution_match"] = self.diagnostics.get("resolution_after") == interval
        return bool(self.diagnostics["resolution_match"])

    def navigate_with_cdp(self, tab: dict, url: str, stage: str) -> dict:
        ws_url = tab.get("webSocketDebuggerUrl")
        if not ws_url:
            self.diagnostics["navigation_method_used"] = "reused_tab_only"
            self.diagnostics["error_stage"] = f"{stage}:missing_websocket_debugger_url"
            self.diagnostics["reason"] = "verification_not_implemented"
            return tab
        try:
            with ws_connect(ws_url, open_timeout=5) as websocket:
                self.diagnostics["devtools_ws_connected"] = True
                websocket.send(json.dumps({"id": 1, "method": "Page.enable"}))
                websocket.recv(timeout=5)
                websocket.send(json.dumps({"id": 2, "method": "Page.navigate", "params": {"url": url}}))
                websocket.recv(timeout=5)
                self.diagnostics["navigation_method_used"] = "cdp_page_navigate"
            time.sleep(2)
            return self.find_tab_by_url(url) or tab
        except Exception as exc:
            self.diagnostics["navigation_method_used"] = "reused_tab_only"
            self.diagnostics["error_stage"] = f"{stage}:cdp_page_navigate_failed"
            self.diagnostics["reason"] = str(exc)
            return tab

    def cdp_call(self, tab: dict, method: str, params: dict | None = None, call_id: int = 1) -> dict:
        ws_url = tab.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("Missing webSocketDebuggerUrl for TradingView tab")
        with ws_connect(ws_url, open_timeout=5) as websocket:
            self.diagnostics["devtools_ws_connected"] = True
            websocket.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
            while True:
                message = json.loads(websocket.recv(timeout=15))
                if message.get("id") == call_id:
                    return message

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
                time.sleep(wait_seconds)
            attempts += 1
            result = self.evaluate_runtime(expression) or {}
            candles = result.get("candles") or []
            if candles:
                break
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


def validate_timeframe(timeframe: str) -> str:
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError("Unsupported timeframe. Use 1W, 1D, 4H, or 1H")
    return SUPPORTED_TIMEFRAMES[timeframe]


def tradingview_url(tradingview_symbol: str, interval: str) -> str:
    return f"https://www.tradingview.com/chart/?symbol={quote(tradingview_symbol, safe='')}&interval={interval}"


def extract_symbol_from_url(url: str) -> str | None:
    marker = "symbol="
    if marker not in url:
        return None
    value = url.split(marker, 1)[1].split("&", 1)[0]
    return unquote(value).replace("%3A", ":")


def normalize_symbol(value: object) -> str | None:
    if value is None:
        return None
    text = unquote(str(value)).strip().upper()
    return text or None


def extract_interval_from_url(url: str) -> str | None:
    marker = "interval="
    if marker not in url:
        return None
    return url.split(marker, 1)[1].split("&", 1)[0]


def connect_to_debug_port(port: int = settings.TRADINGVIEW_DEBUG_PORT) -> bool:
    return TradingViewClient(port).connect_to_debug_port()


def list_tabs(port: int = settings.TRADINGVIEW_DEBUG_PORT) -> list[dict]:
    return TradingViewClient(port).list_tabs()


def open_or_reuse_chart_tab(port: int = settings.TRADINGVIEW_DEBUG_PORT) -> dict:
    return TradingViewClient(port).open_or_reuse_chart_tab()


def open_symbol(tradingview_symbol: str, port: int = settings.TRADINGVIEW_DEBUG_PORT) -> dict:
    return TradingViewClient(port).open_symbol(tradingview_symbol)


def set_timeframe(timeframe: str, port: int = settings.TRADINGVIEW_DEBUG_PORT) -> dict:
    return TradingViewClient(port).set_timeframe(timeframe)


def verify_symbol_loaded(tradingview_symbol: str, port: int = settings.TRADINGVIEW_DEBUG_PORT) -> bool:
    return TradingViewClient(port).verify_symbol_loaded(tradingview_symbol)


def fetch_visible_chart_status(port: int = settings.TRADINGVIEW_DEBUG_PORT) -> dict:
    return TradingViewClient(port).fetch_visible_chart_status()


def fetch_candles(timeframe: str, min_candles: int = 50, port: int = settings.TRADINGVIEW_DEBUG_PORT) -> list[dict]:
    return TradingViewClient(port).fetch_candles(timeframe, min_candles)
