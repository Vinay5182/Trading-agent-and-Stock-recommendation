import logging
import asyncio
from typing import Dict, Any, Optional

from services.ml_candidate_repository import MLCandidateRepository
from services.ml_market_repository import MLMarketRepository
from services.ml_event_repository import MLEventRepository
from services.ml_outcome_repository import MLOutcomeRepository
from services.ml_decision_repository import MLDecisionRepository

logger = logging.getLogger("uvicorn.error")

class MLPipeline:
    """
    Central orchestration service for ML Data Acquisition.
    Responsible for ingesting events and persisting state into the ML schemas.
    """
    
    @classmethod
    async def initialize_indexes(cls):
        """Creates all required indexes for ML collections."""
        try:
            await asyncio.gather(
                MLCandidateRepository.create_indexes(),
                MLMarketRepository.create_indexes(),
                MLEventRepository.create_indexes(),
                MLOutcomeRepository.create_indexes(),
                MLDecisionRepository.create_indexes()
            )
            logger.info("Successfully initialized ML collection indexes.")
        except Exception as e:
            logger.error(f"Failed to initialize ML indexes: {e}")

    # Stub methods to be implemented in Phase 2.2
    
    @classmethod
    async def record_candidate_setup(cls, candidate_data: Dict[str, Any]):
        """Called when a candidate passes precheck."""
        try:
            from datetime import datetime
            import uuid
            from database import get_database

            symbol = candidate_data.get("canonical_symbol") or candidate_data.get("symbol")
            if not symbol:
                return
                
            db = get_database()
            
            # Fetch 90d history directly from MongoDB
            history = []
            try:
                cursor = db.historical_ohlcv.find({
                    "exchange": candidate_data.get("exchange") or "NSE",
                    "canonical_symbol": symbol,
                    "timeframe": "1d",
                    "is_closed": True
                }).sort("candle_open_at", -1).limit(90)
                
                history_docs = [doc async for doc in cursor]
                history_docs.sort(key=lambda x: x.get("candle_open_at", ""))
                
                for doc in history_docs:
                    doc.pop("_id", None)
                    history.append(doc)
            except Exception as e:
                logger.error(f"Failed to fetch warmup history for {symbol}: {e}")
                
            # Extract basic identifiers
            market_date = candidate_data.get("updated_at", datetime.utcnow().isoformat())[:10]
            
            from services.ml_models import MLMarketContext, MLSectorContext, MLCandidate
            
            # Create Market Context
            market_context = MLMarketContext(
                market_date=market_date,
                created_at=datetime.utcnow()
            ).model_dump()
            await MLMarketRepository.insert_market_context(market_context)
            
            # Create Sector Context
            sector = candidate_data.get("sector", "UNKNOWN")
            sector_context = MLSectorContext(
                sector=sector,
                market_date=market_date,
                created_at=datetime.utcnow()
            ).model_dump()
            await MLMarketRepository.insert_sector_context(sector_context)
            
            candidate_types = []
            if candidate_data.get("swing_candidate"):
                candidate_types.append("SWING")
            if candidate_data.get("momentum_candidate"):
                candidate_types.append("MOMENTUM")
                
            for c_type in candidate_types:
                import hashlib
                strategy_version = candidate_data.get("score_version", "v1.0")
                unique_str = f"{symbol}_{market_date}_{c_type}_{strategy_version}"
                candidate_id = f"cand_{hashlib.md5(unique_str.encode()).hexdigest()[:12]}"
                
                candidate = {
                    "candidate_id": candidate_id,
                    "paper_trade_id": None,
                    "candidate_type": c_type,
                    "strategy_version": strategy_version,
                    "analysis_version": "v1.0",
                    "setup_date": market_date,
                    "symbol": symbol,
                    "status": "PRECHECK_PASSED",
                    "pre_setup_ohlcv_90d": history,
                    "created_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow()
                }
                
                for k, v in candidate_data.items():
                    if k not in candidate:
                        candidate[k] = v
                        
                # Sanitize nulls to pass strict Pydantic numeric validation
                for k in ["score", "momentum_score"]:
                    if candidate.get(k) is None:
                        candidate[k] = 0.0
                        
                candidate_model = MLCandidate(**candidate)
                candidate_dict = candidate_model.model_dump()
                
                # Preserve extra feature fields not defined in the schema
                for k, v in candidate.items():
                    if k not in candidate_dict:
                        candidate_dict[k] = v
                        
                existing = await MLCandidateRepository.find_candidate(candidate_id)
                if existing:
                    update_doc = {k: v for k, v in candidate_dict.items() if k not in ["candidate_id", "created_at", "paper_trade_id", "pre_setup_ohlcv_90d"]}
                    update_doc["updated_at"] = datetime.utcnow()
                    await MLCandidateRepository.update_candidate(candidate_id, update_doc)
                else:
                    await MLCandidateRepository.insert_candidate(candidate_dict)
        except Exception as e:
            logger.error(f"ML Pipeline record_candidate_setup failed: {e}")

    @classmethod
    async def record_tv_confirmation(cls, confirmation_data: Dict[str, Any]):
        """Called when a TV confirmation completes."""
        pass
        
    @classmethod
    async def record_trade_creation(cls, trade_data: Dict[str, Any]):
        """Called when a paper trade is successfully created."""
        pass
        
    @classmethod
    async def record_trade_transition(cls, trade_id: str, old_status: str, new_status: str, context: Optional[Dict[str, Any]] = None):
        """Called when a paper trade outcome status changes."""
        pass

    @classmethod
    async def record_daily_progress(cls, progress_data: Dict[str, Any]):
        """Called daily to append progress for active candidates."""
        pass

    @classmethod
    async def record_outcome(cls, outcome_data: Dict[str, Any]):
        """Called when a candidate reaches a terminal state."""
        pass

# Global instance for easy usage
ml_pipeline = MLPipeline()
