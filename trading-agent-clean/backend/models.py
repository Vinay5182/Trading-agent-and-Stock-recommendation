from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


StrategyType = Literal["SWING", "MOMENTUM"]
FinalStatus = Literal[
    "CONFIRMED_SIGNAL",
    "WAIT_FOR_RETEST",
    "REJECTED",
    "TECHNICAL_FAILED",
    "MOMENTUM_SIGNAL",
    "MOMENTUM_WATCHLIST",
    "MOMENTUM_REJECTED",
]


class ScanRow(BaseModel):
    scan_run_id: str
    selected_index: str
    strategy_type: StrategyType
    exchange: Literal["NSE", "BSE"]
    symbol: str
    tradingview_symbol: str
    score: float
    status: FinalStatus
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SettingsResponse(BaseModel):
    backend_port: int
    database_name: str
    live_trading_enabled: bool
    paper_mode: bool
    tradingview_debug_port: int
