from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from config import settings
from services.tradingview_manager import tradingview_manager
from tv_client import TradingViewClient, validate_symbol, validate_timeframe


router = APIRouter()


class TVTestRequest(BaseModel):
    tradingview_symbol: str | None = None
    symbol: str | None = None
    timeframe: str
    fetch_candles: bool = False


@router.post("/test-symbol")
async def test_symbol(request: TVTestRequest) -> dict:
    return await tradingview_manager.run_sync(
        "tv.test_symbol",
        _test_symbol_sync,
        request,
        timeout_seconds=settings.TRADINGVIEW_SYMBOL_TIMEOUT_SECONDS,
        retries=1,
    )


@router.get("/runtime-status")
async def runtime_status() -> dict:
    return tradingview_manager.runtime_status()


@router.get("/attachable-tabs")
async def attachable_tabs() -> dict:
    return await tradingview_manager.run_sync(
        "tv.list_attachable_tabs",
        _list_attachable_tabs_sync,
        timeout_seconds=25,
        retries=0,
    )


@router.post("/attach-tab")
async def attach_tab(target_id: str = Query(...)) -> dict:
    try:
        return await tradingview_manager.run_sync(
            "tv.attach_tab",
            _attach_tab_sync,
            target_id,
            timeout_seconds=25,
            retries=0,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/detach-tab")
async def detach_tab() -> dict:
    tradingview_manager.detach_target()
    return {"attached": False, "attached_target": None}


def _list_attachable_tabs_sync() -> dict:
    client = TradingViewClient(settings.TRADINGVIEW_DEBUG_PORT)
    client.connect_to_debug_port()
    targets = client.list_attachable_chart_targets()
    auto_attached = False
    attached = tradingview_manager.attached_target_snapshot()
    if attached and not any(target.get("target_id") == attached.get("target_id") for target in targets):
        tradingview_manager.clear_attachment_if_target(attached.get("target_id"))
        attached = None
    if len(targets) == 1 and not attached:
        attached = tradingview_manager.attach_target(targets[0])
        auto_attached = True
    return {
        "targets": targets,
        "count": len(targets),
        "auto_attached": auto_attached,
        "attached_target": attached,
        "attached_target_id": attached.get("target_id") if attached else None,
        "diagnostics": client.diagnostics,
    }


def _attach_tab_sync(target_id: str) -> dict:
    client = TradingViewClient(settings.TRADINGVIEW_DEBUG_PORT)
    client.connect_to_debug_port()
    target = client.validate_attachable_target(target_id)
    if not target:
        raise ValueError("INVALID_TV_TARGET: target is not an attachable TradingView chart")
    attached = tradingview_manager.attach_target(target)
    return {"attached": True, "attached_target": attached, "attached_target_id": attached.get("target_id")}


def _test_symbol_sync(request: TVTestRequest) -> dict:
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
        client = TradingViewClient(
            port,
            attached_target_id=tradingview_manager.attached_target_id,
            require_attached_tab=True,
        )
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
