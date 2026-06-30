from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse


class ConfigValidationError(ValueError):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    clean = value.strip().lower()
    if clean in {"1", "true", "yes", "on"}:
        return True
    if clean in {"0", "false", "no", "off"}:
        return False
    raise ConfigValidationError(
        "CONFIG_INVALID_BOOLEAN",
        f"{name} must be one of true/false, yes/no, on/off, or 1/0.",
        {"field": name},
    )


def env_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.getenv(name)
    value = str(default) if raw is None else raw.strip()
    try:
        parsed = int(value)
    except Exception as exc:
        raise ConfigValidationError("CONFIG_INVALID_INTEGER", f"{name} must be an integer.", {"field": name}) from exc
    if minimum is not None and parsed < minimum:
        raise ConfigValidationError("CONFIG_INTEGER_TOO_SMALL", f"{name} must be >= {minimum}.", {"field": name})
    if maximum is not None and parsed > maximum:
        raise ConfigValidationError("CONFIG_INTEGER_TOO_LARGE", f"{name} must be <= {maximum}.", {"field": name})
    return parsed


def env_str(name: str, default: str, *, required: bool = False) -> str:
    value = os.getenv(name, default).strip()
    if required and not value:
        raise ConfigValidationError("CONFIG_REQUIRED", f"{name} is required.", {"field": name})
    return value


@dataclass(frozen=True)
class Settings:
    BACKEND_HOST: str = field(default_factory=lambda: env_str("BACKEND_HOST", "127.0.0.1"))
    FRONTEND_HOST: str = field(default_factory=lambda: env_str("FRONTEND_HOST", "127.0.0.1"))
    BACKEND_PORT: int = field(default_factory=lambda: env_int("BACKEND_PORT", 8011, minimum=1, maximum=65535))
    FRONTEND_PORT: int = field(default_factory=lambda: env_int("FRONTEND_PORT", 5173, minimum=1, maximum=65535))
    MONGO_PORT: int = field(default_factory=lambda: env_int("MONGO_PORT", 27017, minimum=1, maximum=65535))
    MONGO_URI: str = field(default_factory=lambda: env_str("MONGO_URI", "mongodb://localhost:27017", required=True))
    DATABASE_NAME: str = field(default_factory=lambda: env_str("DATABASE_NAME", "trading_agent_clean", required=True))
    LIVE_TRADING_ENABLED: bool = False
    PAPER_MODE: bool = True
    TRADINGVIEW_DEBUG_PORT: int = field(default_factory=lambda: env_int("TRADINGVIEW_DEBUG_PORT", 9222, minimum=1, maximum=65535))
    TRADINGVIEW_RESOLUTION_WAIT_SECONDS: int = 10
    TRADINGVIEW_TIMEFRAME_STABILIZE_SECONDS: int = 6
    TRADINGVIEW_CANDLE_STABILITY_WAIT_SECONDS: int = 2
    TRADINGVIEW_OHLCV_RETRIES: int = 2
    TRADINGVIEW_MIN_CANDLES: int = 30
    TRADINGVIEW_SYMBOL_WAIT_SECONDS: int = 10
    TRADINGVIEW_SYMBOL_STABILIZE_SECONDS: int = 3
    TRADINGVIEW_SYMBOL_RETRIES: int = 2
    TRADINGVIEW_SYMBOL_TIMEOUT_SECONDS: int = field(default_factory=lambda: env_int("TRADINGVIEW_SYMBOL_TIMEOUT_SECONDS", 90, minimum=1, maximum=600))
    PAPER_UPDATE_SCHEDULER_ENABLED: bool = field(default_factory=lambda: env_bool("PAPER_UPDATE_SCHEDULER_ENABLED", False))
    PAPER_UPDATE_SCHEDULER_MODE: str = field(default_factory=lambda: env_str("PAPER_UPDATE_SCHEDULER_MODE", "dry_run_only"))
    PAPER_UPDATE_SCHEDULER_DRY_RUN_ONLY: bool = field(default_factory=lambda: env_bool("PAPER_UPDATE_SCHEDULER_DRY_RUN_ONLY", True))
    PAPER_UPDATE_SCHEDULER_ALLOW_REAL_WRITES: bool = field(default_factory=lambda: env_bool("PAPER_UPDATE_SCHEDULER_ALLOW_REAL_WRITES", False))
    PAPER_UPDATE_SCHEDULER_INTERVAL_MINUTES: int = field(default_factory=lambda: env_int("PAPER_UPDATE_SCHEDULER_INTERVAL_MINUTES", 30, minimum=1, maximum=1440))
    PAPER_UPDATE_SCHEDULER_AFTER_MARKET_CLOSE_ONLY: bool = field(default_factory=lambda: env_bool("PAPER_UPDATE_SCHEDULER_AFTER_MARKET_CLOSE_ONLY", True))
    PAPER_UPDATE_SCHEDULER_DRY_RUN_FIRST: bool = field(default_factory=lambda: env_bool("PAPER_UPDATE_SCHEDULER_DRY_RUN_FIRST", True))
    PAPER_UPDATE_SCHEDULER_MAX_TRADES: int = field(default_factory=lambda: env_int("PAPER_UPDATE_SCHEDULER_MAX_TRADES", 6, minimum=1, maximum=200))
    PAPER_UPDATE_SCHEDULER_MAX_WRITES: int = field(default_factory=lambda: env_int("PAPER_UPDATE_SCHEDULER_MAX_WRITES", 1, minimum=0, maximum=200))
    PAPER_MARKET_SNAPSHOT_RETENTION_DAYS: int = field(default_factory=lambda: env_int("PAPER_MARKET_SNAPSHOT_RETENTION_DAYS", 14, minimum=1, maximum=3650))
    SMOKE_READ_ONLY_MODE: bool = field(default_factory=lambda: env_bool("SMOKE_READ_ONLY_MODE", False))

    STARTING_VIRTUAL_BALANCE: float = 250000.0
    LEVERAGE: float = 2.5
    MINIMUM_ENTRY_MARGIN: float = 5000.0
    PORTFOLIO_MARGIN_LIMIT_PERCENT: float = 80.0
    PORTFOLIO_RISK_LIMIT_PERCENT: float = 5.0

    GRADE_RISK_PERCENT_A_PLUS: float = 0.50
    GRADE_MARGIN_CAP_A_PLUS: float = 10.0
    GRADE_RISK_PERCENT_A: float = 0.35
    GRADE_MARGIN_CAP_A: float = 8.0
    GRADE_RISK_PERCENT_B: float = 0.25
    GRADE_MARGIN_CAP_B: float = 6.0


def validate_settings(value: Settings) -> dict:
    parsed = urlparse(value.MONGO_URI)
    if parsed.scheme not in {"mongodb", "mongodb+srv"} or not parsed.netloc:
        raise ConfigValidationError("CONFIG_INVALID_MONGO_URI", "MONGO_URI must be a valid MongoDB URI.", {"field": "MONGO_URI"})
    if not value.DATABASE_NAME.replace("_", "").replace("-", "").isalnum():
        raise ConfigValidationError("CONFIG_INVALID_DATABASE_NAME", "DATABASE_NAME contains unsupported characters.", {"field": "DATABASE_NAME"})
    if value.PAPER_UPDATE_SCHEDULER_MODE not in {"dry_run_only", "disabled", "real"}:
        raise ConfigValidationError(
            "CONFIG_INVALID_SCHEDULER_MODE",
            "PAPER_UPDATE_SCHEDULER_MODE must be disabled, dry_run_only, or real.",
            {"field": "PAPER_UPDATE_SCHEDULER_MODE"},
        )
    if value.SMOKE_READ_ONLY_MODE:
        return {"ok": True, "automation_disabled": True, "smoke_read_only_mode": True}
    if value.PAPER_UPDATE_SCHEDULER_MODE == "real" and not value.PAPER_UPDATE_SCHEDULER_ALLOW_REAL_WRITES:
        raise ConfigValidationError(
            "CONFIG_UNSAFE_SCHEDULER",
            "Real scheduler mode requires PAPER_UPDATE_SCHEDULER_ALLOW_REAL_WRITES=true.",
            {"field": "PAPER_UPDATE_SCHEDULER_ALLOW_REAL_WRITES"},
        )
    return {"ok": True, "automation_disabled": False, "smoke_read_only_mode": False}


settings = Settings()
validate_settings(settings)

GENUINE_OPEN_STATUSES = {"ACTIVE", "T1_PARTIAL", "T2_PARTIAL", "TARGET_1_HIT"}
