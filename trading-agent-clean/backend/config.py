import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    BACKEND_PORT: int = int(os.getenv("BACKEND_PORT", "8011"))
    MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    DATABASE_NAME: str = os.getenv("DATABASE_NAME", "trading_agent_clean")
    LIVE_TRADING_ENABLED: bool = False
    PAPER_MODE: bool = True
    TRADINGVIEW_DEBUG_PORT: int = 9222
    TRADINGVIEW_RESOLUTION_WAIT_SECONDS: int = 10
    TRADINGVIEW_TIMEFRAME_STABILIZE_SECONDS: int = 6
    TRADINGVIEW_CANDLE_STABILITY_WAIT_SECONDS: int = 2
    TRADINGVIEW_OHLCV_RETRIES: int = 5
    TRADINGVIEW_MIN_CANDLES: int = 30
    TRADINGVIEW_SYMBOL_WAIT_SECONDS: int = 10
    TRADINGVIEW_SYMBOL_STABILIZE_SECONDS: int = 3
    TRADINGVIEW_SYMBOL_RETRIES: int = 5


settings = Settings()
