from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

class NullSafeNumeric(BaseModel):
    @classmethod
    def __get_validators__(cls):
        yield cls.validate
    
    @classmethod
    def validate(cls, v):
        if v is None:
            return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

class MLCandidate(BaseModel):
    candidate_id: str = Field(..., description="Unique ID for the ML candidate")
    paper_trade_id: Optional[str] = Field(None, description="Linked paper trade ID if executed")
    strategy_version: str = Field(..., description="Version of the trading strategy")
    analysis_version: str = Field(..., description="Version of the analysis logic")
    setup_date: str = Field(..., description="Date of the setup")
    symbol: str = Field(..., description="Stock symbol")
    candidate_type: str = Field(..., description="SWING or MOMENTUM")
    status: str = Field(..., description="Current status of the candidate")
    
    # Null-safe numeric fields for features
    score: float = Field(0.0)
    momentum_score: float = Field(0.0)
    
    # Pre-setup history
    pre_setup_ohlcv_90d: List[Dict[str, Any]] = Field(default_factory=list, description="90 days of OHLCV leading up to setup")
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class MLCandidateDailyProgress(BaseModel):
    candidate_id: str = Field(..., description="Linked candidate ID")
    paper_trade_id: Optional[str] = Field(None)
    date: str = Field(..., description="Trading date")
    strategy_version: str = Field(...)
    analysis_version: str = Field(...)
    
    # OHLCV
    open: float = Field(0.0)
    high: float = Field(0.0)
    low: float = Field(0.0)
    close: float = Field(0.0)
    volume: float = Field(0.0)
    
    # Indicators
    atr_14: float = Field(0.0)
    ema_9: float = Field(0.0)
    ema_21: float = Field(0.0)
    ema_50: float = Field(0.0)
    sma_200: float = Field(0.0)
    
    created_at: datetime = Field(default_factory=datetime.utcnow)

class MLOutcome(BaseModel):
    candidate_id: str = Field(...)
    paper_trade_id: Optional[str] = Field(None)
    strategy_version: str = Field(...)
    analysis_version: str = Field(...)
    
    max_favorable_excursion: float = Field(0.0)
    max_adverse_excursion: float = Field(0.0)
    max_rr: float = Field(0.0)
    days_in_trade: int = Field(0)
    
    planned_entry: float = Field(0.0)
    actual_entry: float = Field(0.0)
    slippage: float = Field(0.0)
    missed_entry_flag: bool = Field(False)
    
    created_at: datetime = Field(default_factory=datetime.utcnow)

class MLMarketContext(BaseModel):
    market_date: str = Field(..., description="Date of the market context")
    nifty_trend: str = Field("")
    banknifty_trend: str = Field("")
    india_vix: float = Field(0.0)
    advance_decline_ratio: float = Field(0.0)
    market_breadth: float = Field(0.0)
    
    created_at: datetime = Field(default_factory=datetime.utcnow)

class MLSectorContext(BaseModel):
    sector: str = Field(...)
    market_date: str = Field(...)
    sector_trend: str = Field("")
    relative_strength_ranking: int = Field(0)
    
    created_at: datetime = Field(default_factory=datetime.utcnow)

class MLDecision(BaseModel):
    candidate_id: str = Field(...)
    paper_trade_id: Optional[str] = Field(None)
    strategy_version: str = Field(...)
    analysis_version: str = Field(...)
    
    decision_type: str = Field(...)
    decision_reason: str = Field(...)
    
    created_at: datetime = Field(default_factory=datetime.utcnow)

class MLEvent(BaseModel):
    event_id: str = Field(...)
    candidate_id: str = Field(...)
    paper_trade_id: Optional[str] = Field(None)
    event_type: str = Field(...)
    event_time: datetime = Field(default_factory=datetime.utcnow)
    
    previous_state: str = Field(...)
    new_state: str = Field(...)
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
