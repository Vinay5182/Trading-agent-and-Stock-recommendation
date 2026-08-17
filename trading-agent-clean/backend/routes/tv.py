from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Any

from config import settings
from security.operator_intent import require_operator_intent
from services.tradingview_manager import TradingViewPreflightError, tradingview_manager
from tv_client import validate_symbol, validate_timeframe


router = APIRouter()


class TVTestRequest(BaseModel):
    tradingview_symbol: str | None = None
    symbol: str | None = None
    timeframe: str
    fetch_candles: bool = False


@router.post("/test-symbol")
async def test_symbol(
    request: TVTestRequest,
    _operator_intent: None = Depends(require_operator_intent),
) -> Any:
    try:
        return await tradingview_manager.run_operation(
            "tv.test_symbol",
            _test_symbol_sync,
            request,
            require_chart=True,
            allow_single_tab_auto_attach=True,
            timeout_seconds=settings.TRADINGVIEW_SYMBOL_TIMEOUT_SECONDS,
            retries=1,
        )
    except TradingViewPreflightError as exc:
        status_code = 409 if exc.code == "TV_OPERATION_BUSY" else 400
        return JSONResponse(status_code=status_code, content=exc.details)


@router.get("/runtime-status")
async def runtime_status() -> dict:
    return tradingview_manager.runtime_status()


@router.get("/attachable-tabs")
async def attachable_tabs() -> Any:
    try:
        return await tradingview_manager.run_read_only_inspection(
            "tv.list_attachable_tabs",
            _list_attachable_tabs_sync,
            timeout_seconds=25,
        )
    except TradingViewPreflightError as exc:
        if exc.code in ("TV_OPERATION_BUSY", "TV_MANAGER_BUSY"):
            return _attachable_tabs_busy_payload(exc)
        return JSONResponse(status_code=400, content=exc.details)
    except TimeoutError as exc:
        return JSONResponse(
            status_code=504,
            content={"code": "TV_DISCOVERY_TIMEOUT", "message": str(exc), "retryable": True},
        )


@router.post("/attach-tab")
async def attach_tab(
    target_id: str = Query(...),
    _operator_intent: None = Depends(require_operator_intent),
) -> Any:
    try:
        return await tradingview_manager.run_operation(
            "tv.attach_tab",
            _attach_tab_sync,
            target_id,
            require_chart=False,
            timeout_seconds=25,
            retries=0,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TradingViewPreflightError as exc:
        status_code = 409 if exc.code == "TV_OPERATION_BUSY" else 400
        return JSONResponse(status_code=status_code, content=exc.details)
    except TimeoutError as exc:
        return JSONResponse(
            status_code=504,
            content={"code": "TV_ATTACH_TIMEOUT", "message": str(exc), "retryable": True},
        )


@router.post("/detach-tab")
async def detach_tab(_operator_intent: None = Depends(require_operator_intent)) -> Any:
    try:
        return await tradingview_manager.detach_target_serialized()
    except TradingViewPreflightError as exc:
        status_code = 409 if exc.code == "TV_OPERATION_BUSY" else 400
        return JSONResponse(status_code=status_code, content=exc.details)
    except TimeoutError as exc:
        return JSONResponse(
            status_code=504,
            content={"code": "TV_DETACH_TIMEOUT", "message": str(exc), "retryable": True},
        )


def _list_attachable_tabs_sync() -> dict:
    client = tradingview_manager.get_client()
    client.connect_to_debug_port()
    list_targets = getattr(client, "list_attachable_chart_targets", None) or client.list_attachable_targets
    targets = list_targets()
    preflight = tradingview_manager.get_preflight_status()
    attached = tradingview_manager.attached_target_snapshot()
    attached_target_id = attached.get("target_id") if attached else None
    target_ids = {str(target.get("target_id")) for target in targets if target.get("target_id")}
    attached_target_visible = bool(attached_target_id and str(attached_target_id) in target_ids)
    return {
        "targets": targets,
        "count": len(targets),
        "auto_attached": False,
        "attached_target": attached,
        "attached_target_id": attached_target_id,
        "attached_target_visible": attached_target_visible,
        "attached_target_stale": bool(attached_target_id and not attached_target_visible),
        "manual_attachment_required": attached_target_id is None or not attached_target_visible,
        "manager_busy": False,
        "state": preflight.get("state"),
        "diagnostics": getattr(client, "diagnostics", {}),
    }


def _attach_tab_sync(target_id: str) -> dict:
    client = tradingview_manager.get_client()
    client.connect_to_debug_port()
    target = client.validate_attachable_target(target_id)
    if not target:
        raise ValueError("INVALID_TV_TARGET: target is not an attachable TradingView chart")
    attached = tradingview_manager.attach_target(target)
    return {"attached": True, "attached_target": attached, "attached_target_id": attached.get("target_id")}


def _attachable_tabs_busy_payload(exc: TradingViewPreflightError) -> dict:
    attached = tradingview_manager.attached_target_snapshot()
    attached_target_id = attached.get("target_id") if attached else None
    status = tradingview_manager.operation_status_snapshot()
    return {
        "targets": [],
        "count": 0,
        "auto_attached": False,
        "attached_target": attached,
        "attached_target_id": attached_target_id,
        "attached_target_visible": False,
        "attached_target_stale": False,
        "manual_attachment_required": attached_target_id is None,
        "manager_busy": True,
        "queue_length": status["queue_length"],
        "active_operation": status["active_operation"],
        "target_listing_skipped": True,
        "diagnostics": {
            "code": exc.code,
            "message": exc.message,
        },
    }


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
        client = tradingview_manager.get_client(require_attached_tab=True)
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
