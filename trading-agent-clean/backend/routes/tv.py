from fastapi import APIRouter
from pydantic import BaseModel

from config import settings
from tv_client import TradingViewClient, validate_symbol, validate_timeframe


router = APIRouter()


class TVTestRequest(BaseModel):
    tradingview_symbol: str | None = None
    symbol: str | None = None
    timeframe: str
    fetch_candles: bool = False


@router.post("/test-symbol")
async def test_symbol(request: TVTestRequest) -> dict:
    port = settings.TRADINGVIEW_DEBUG_PORT
    tradingview_symbol = request.tradingview_symbol or request.symbol
    result = {
        "success": False,
        "connected": False,
        "port_used": port,
        "tradingview_symbol": tradingview_symbol,
        "timeframe": request.timeframe,
        "symbol_loaded": False,
        "candles_count": 0,
        "candles": [],
        "error": None,
        "devtools_version_ok": False,
        "tabs_count": 0,
        "chart_tab_found": False,
        "json_new_method_used": None,
        "open_url": None,
        "http_error_stage": None,
        "navigation_note": None,
        "navigation_method_used": None,
        "devtools_ws_connected": False,
        "requested_symbol": tradingview_symbol,
        "current_tab_url": None,
        "current_tab_title": None,
        "symbol_verification_method": None,
        "symbol_navigation_attempted": False,
        "reason": None,
        "error_stage": None,
    }
    client = None
    try:
        if not tradingview_symbol:
            raise ValueError("symbol or tradingview_symbol is required")
        validate_symbol(tradingview_symbol)
        validate_timeframe(request.timeframe)
        client = TradingViewClient(port)
        result["connected"] = client.connect_to_debug_port()
        client.open_symbol(tradingview_symbol)
        if client.verify_symbol_loaded(tradingview_symbol):
            client.set_timeframe(request.timeframe)
        result["symbol_loaded"] = client.verify_symbol_loaded(tradingview_symbol)
        if request.fetch_candles and result["symbol_loaded"]:
            candles = client.fetch_candles(request.timeframe, min_candles=1)
            result["candles"] = candles
            result["candles_count"] = len(candles)
        result["chart_status"] = client.fetch_visible_chart_status()
        result["success"] = result["connected"] and result["symbol_loaded"]
    except Exception as exc:
        result["error"] = str(exc)
    if client is not None:
        result.update(client.diagnostics)
    return result
