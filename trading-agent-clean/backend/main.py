from fastapi import FastAPI

from config import settings
from database import lifespan
from models import SettingsResponse
from routes import momentum, paper, scan, signals, swing


app = FastAPI(
    title="Trading Agent Clean",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(scan.router, prefix="/api/scan", tags=["scan"])
app.include_router(swing.router, prefix="/api/swing", tags=["swing"])
app.include_router(momentum.router, prefix="/api/momentum", tags=["momentum"])
app.include_router(signals.router, prefix="/api/signals", tags=["signals"])
app.include_router(paper.router, prefix="/api/paper", tags=["paper"])


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
