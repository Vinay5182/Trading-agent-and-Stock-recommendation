from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from config import ConfigValidationError
from services.redaction import redact_text, redact_value


GENERIC_INTERNAL_ERROR = "Internal server error. Check backend logs with the request_id."


def error_payload(code: str, message: Any, details: dict | None = None, *, request_id: str | None = None) -> dict:
    payload = {
        "code": code,
        "message": redact_text(message),
        "details": redact_value(details or {}),
    }
    if request_id:
        payload["request_id"] = request_id
    return payload


def error_response(status_code: int, code: str, message: Any, details: dict | None = None, *, request_id: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=error_payload(code, message, details, request_id=request_id))


def normalize_http_exception(exc: HTTPException) -> tuple[str, str, dict]:
    detail = exc.detail
    if isinstance(detail, dict):
        code = str(detail.get("code") or f"HTTP_{exc.status_code}")
        message = detail.get("message") or code
        details = detail.get("details") if isinstance(detail.get("details"), dict) else {
            key: value for key, value in detail.items() if key not in {"code", "message", "details"}
        }
        return code, str(message), details
    return f"HTTP_{exc.status_code}", str(detail or "HTTP error"), {}


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code, message, details = normalize_http_exception(exc)
    return error_response(exc.status_code, code, message, details, request_id=getattr(request.state, "request_id", None))


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return error_response(
        422,
        "REQUEST_VALIDATION_ERROR",
        "Request validation failed.",
        {"errors": exc.errors()},
        request_id=getattr(request.state, "request_id", None),
    )


async def config_exception_handler(request: Request, exc: ConfigValidationError) -> JSONResponse:
    return error_response(500, exc.code, exc.message, exc.details, request_id=getattr(request.state, "request_id", None))


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return error_response(
        500,
        "INTERNAL_SERVER_ERROR",
        GENERIC_INTERNAL_ERROR,
        {"exception_type": type(exc).__name__},
        request_id=getattr(request.state, "request_id", None),
    )
