import os
from dataclasses import dataclass


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
    TRADINGVIEW_SYMBOL_TIMEOUT_SECONDS: int = int(os.getenv("TRADINGVIEW_SYMBOL_TIMEOUT_SECONDS", "90"))
    PAPER_UPDATE_SCHEDULER_ENABLED: bool = env_bool("PAPER_UPDATE_SCHEDULER_ENABLED", False)
    PAPER_UPDATE_SCHEDULER_MODE: str = os.getenv("PAPER_UPDATE_SCHEDULER_MODE", "dry_run_only")
    PAPER_UPDATE_SCHEDULER_DRY_RUN_ONLY: bool = env_bool("PAPER_UPDATE_SCHEDULER_DRY_RUN_ONLY", True)
    PAPER_UPDATE_SCHEDULER_ALLOW_REAL_WRITES: bool = env_bool("PAPER_UPDATE_SCHEDULER_ALLOW_REAL_WRITES", False)
    PAPER_UPDATE_SCHEDULER_INTERVAL_MINUTES: int = int(os.getenv("PAPER_UPDATE_SCHEDULER_INTERVAL_MINUTES", "30"))
    PAPER_UPDATE_SCHEDULER_AFTER_MARKET_CLOSE_ONLY: bool = env_bool("PAPER_UPDATE_SCHEDULER_AFTER_MARKET_CLOSE_ONLY", True)
    PAPER_UPDATE_SCHEDULER_DRY_RUN_FIRST: bool = env_bool("PAPER_UPDATE_SCHEDULER_DRY_RUN_FIRST", True)
    PAPER_UPDATE_SCHEDULER_MAX_TRADES: int = int(os.getenv("PAPER_UPDATE_SCHEDULER_MAX_TRADES", "6"))
    PAPER_UPDATE_SCHEDULER_MAX_WRITES: int = int(os.getenv("PAPER_UPDATE_SCHEDULER_MAX_WRITES", "1"))
    PAPER_MARKET_SNAPSHOT_RETENTION_DAYS: int = int(os.getenv("PAPER_MARKET_SNAPSHOT_RETENTION_DAYS", "14"))


settings = Settings()
