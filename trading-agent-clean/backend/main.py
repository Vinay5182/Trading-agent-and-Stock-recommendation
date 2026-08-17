import os
import sys

# Ensure backend directory is in sys.path for imports when starting via uvicorn backend.main:app
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from config import ConfigValidationError, settings
from database import lifespan
from models import SettingsResponse
from routes import ai as ai_routes, dashboard, market, momentum, outcomes, paper, scan, score, signals, swing, system, tv
from security.operator_intent import OperatorIntentRequired, operator_intent_exception_handler
from services.error_contract import (
    config_exception_handler,
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)


app = FastAPI(title="Trading Agent Clean", version="0.2.0", lifespan=lifespan)
app.add_exception_handler(OperatorIntentRequired, operator_intent_exception_handler)
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(ConfigValidationError, config_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:5174",
        "http://localhost:5174",
        "http://127.0.0.1:5175",
        "http://localhost:5175",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request.state.request_id = uuid4().hex[:12]
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response

app.include_router(scan.router, prefix="/api/scan", tags=["scan"])
app.include_router(score.router, prefix="/api/score", tags=["score"])
app.include_router(swing.router, prefix="/api/swing", tags=["swing"])
app.include_router(momentum.router, prefix="/api/momentum", tags=["momentum"])
app.include_router(signals.router, prefix="/api/signals", tags=["signals"])
app.include_router(paper.router, prefix="/api/paper", tags=["paper"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])
app.include_router(tv.router, prefix="/api/tv", tags=["tradingview"])
app.include_router(market.router, prefix="/api/market", tags=["market"])
app.include_router(ai_routes.router, prefix="/api/ai", tags=["ai"])
app.include_router(system.router, prefix="/api/system", tags=["system"])
app.include_router(outcomes.router, prefix="/api/outcomes", tags=["outcomes"])


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/settings", response_model=SettingsResponse)
async def get_settings() -> SettingsResponse:
    return SettingsResponse(
        backend_port=settings.BACKEND_PORT,
        database_name=settings.DATABASE_NAME,
        live_trading_enabled=settings.LIVE_TRADING_ENABLED,
        paper_mode=settings.PAPER_MODE,
        tradingview_debug_port=settings.TRADINGVIEW_DEBUG_PORT,
    )
