from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from database import lifespan
from models import SettingsResponse
from routes import ai as ai_routes, dashboard, market, momentum, paper, scan, score, signals, swing, tv


app = FastAPI(title="Trading Agent Clean", version="0.2.0", lifespan=lifespan)
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
