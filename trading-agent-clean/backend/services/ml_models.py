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
    
    # Phase 2.2B: TradingView Observer Enrichment
    tradingview_status: Optional[str] = Field(None, description="Raw TV status")
    confirmation_status: Optional[str] = Field(None, description="Mapped confirmation status")
    confidence: Optional[float] = Field(None, description="Confidence score")
    quality_grade: Optional[str] = Field(None, description="Trade quality grade")
    trap_status: Optional[str] = Field(None, description="Trap status")
    strategy_decision: Optional[str] = Field(None, description="Strategy decision")
    diagnostics: Optional[str] = Field(None, description="Risk diagnostics")
    rejection_reason: Optional[str] = Field(None, description="Rejection reason")
    paper_trade_valid: Optional[bool] = Field(None, description="Paper plan validity")
    entry: Optional[float] = Field(None, description="Entry price")
    stop_loss: Optional[float] = Field(None, description="Stop loss price")
    targets: Optional[List[float]] = Field(None, description="Target prices")
    risk_reward: Optional[float] = Field(None, description="Risk reward ratio")
    timestamp: Optional[datetime] = Field(None, description="Time of observation")
    observer_version: Optional[str] = Field(None, description="Observer analysis version")
    
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
    sensex_trend: str = Field("")
    india_vix: float = Field(0.0)
    vix_regime: str = Field("")
    total_advances: int = Field(0)
    total_declines: int = Field(0)
    advance_decline_ratio: float = Field(0.0)
    percent_advancing: float = Field(0.0)
    market_breadth: float = Field(0.0)
    stocks_above_20_ema: int = Field(0)
    stocks_above_50_ema: int = Field(0)
    stocks_above_200_ema: int = Field(0)
    top_3_strongest_sectors: List[str] = Field(default_factory=list)
    top_3_weakest_sectors: List[str] = Field(default_factory=list)
    market_regime: str = Field("")
    source_versions: Dict[str, str] = Field(default_factory=dict)
    
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)

class MLSectorContext(BaseModel):
    sector: str = Field(...)
    market_date: str = Field(...)
    sector_trend: str = Field("")
    relative_strength_ranking: int = Field(0)
    
    created_at: datetime = Field(default_factory=datetime.utcnow)

class MLUnifiedMarketContext(BaseModel):
    trade_date: str = Field(..., description="Trade date in YYYY-MM-DD format")
    generated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    market_context: Dict[str, Any] = Field(default_factory=dict)
    sector_context: Dict[str, Any] = Field(default_factory=dict)

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
