import logging
from datetime import datetime
from typing import Dict, Any, Optional
from pymongo.errors import DuplicateKeyError
from database import get_database

logger = logging.getLogger("uvicorn.error")

class MLMarketRepository:
    """Repository for consolidated market_context_daily collection"""
    
    @staticmethod
    def get_market_collection():
        return get_database()["market_context_daily"]
        
    @staticmethod
    def get_sector_collection():
        """Deprecated: Retained for backward compatibility if queried; points to market_context_daily"""
        return get_database()["market_context_daily"]

    @classmethod
    async def create_indexes(cls):
        market_collection = cls.get_market_collection()
        try:
            await market_collection.create_index("trade_date", unique=True)
        except Exception:
            pass
        try:
            await market_collection.create_index("generated_at")
        except Exception:
            pass

    @classmethod
    async def insert_market_context(cls, context_data: Dict[str, Any], sector_context: Optional[Dict[str, Any]] = None) -> bool:
        try:
            trade_date = context_data.get("trade_date") or context_data.get("market_date") or datetime.utcnow().strftime("%Y-%m-%d")
            
            market_context_sub = context_data.get("market_context")
            if not isinstance(market_context_sub, dict):
                market_context_sub = {
                    "market_phase": context_data.get("market_phase") or context_data.get("market_regime") or "ACCUMULATION",
                    "market_bias": context_data.get("market_bias") or context_data.get("nifty_trend") or "NEUTRAL",
                    "market_sentiment": context_data.get("market_sentiment") or "NEUTRAL",
                    "trend_strength": context_data.get("trend_strength") or "MODERATE",
                    "volatility_regime": context_data.get("volatility_regime") or context_data.get("vix_regime") or "NORMAL_VOLATILITY",
                    "breadth": float(context_data.get("breadth") or context_data.get("market_breadth") or 0.0),
                    "advance_decline": context_data.get("advance_decline") or f"{context_data.get('total_advances', 0)}:{context_data.get('total_declines', 0)}",
                    "index_summary": context_data.get("index_summary") or f"Nifty: {context_data.get('nifty_trend', 'FLAT')}, BankNifty: {context_data.get('banknifty_trend', 'FLAT')}",
                    "leading_index": context_data.get("leading_index") or "NIFTY50",
                    "weakest_index": context_data.get("weakest_index") or "NIFTYIT",
                    "market_strength_score": float(context_data.get("market_strength_score") or context_data.get("percent_advancing") or 50.0),
                    "liquidity_environment": context_data.get("liquidity_environment") or "NORMAL",
                    "risk_environment": context_data.get("risk_environment") or "MODERATE",
                    "trading_environment": context_data.get("trading_environment") or "FAVORABLE",
                    "institutional_activity": context_data.get("institutional_activity") or "BALANCED",
                    "fii_dii_summary": context_data.get("fii_dii_summary") or "Net Neutral",
                    "vix_context": context_data.get("vix_context") or f"VIX at {context_data.get('india_vix', 0.0)} ({context_data.get('vix_regime', 'NORMAL')})",
                    "important_observations": context_data.get("important_observations") or [],
                    "summary": context_data.get("summary") or "Daily market context generated",
                    "confidence": float(context_data.get("confidence") or 0.85)
                }

            sector_context_sub = sector_context if sector_context is not None else context_data.get("sector_context")

            now_iso = datetime.utcnow().isoformat()
            
            set_payload = {
                "trade_date": trade_date,
                "market_date": trade_date,
                "generated_at": now_iso,
                "market_context": market_context_sub
            }
            if sector_context_sub is not None:
                set_payload["sector_context"] = sector_context_sub

            update_payload = {"$set": set_payload}
            if sector_context_sub is None:
                update_payload["$setOnInsert"] = {"sector_context": {}}

            await cls.get_market_collection().update_one(
                {"trade_date": trade_date},
                update_payload,
                upsert=True
            )
            return True
        except Exception as e:
            logger.error(f"Error inserting unified market context: {e}")
            return False

    @classmethod
    async def insert_sector_context(cls, context_data: Dict[str, Any]) -> bool:
        try:
            trade_date = context_data.get("trade_date") or context_data.get("market_date") or datetime.utcnow().strftime("%Y-%m-%d")
            sector_name = context_data.get("sector") or "UNKNOWN"

            if sector_name == "UNKNOWN":
                logger.warning(f"Skipping sector context insertion for UNKNOWN / unmapped sector on {trade_date}")
                return False

            existing_doc = await cls.get_market_collection().find_one({"trade_date": trade_date})
            if not existing_doc or not existing_doc.get("market_context"):
                logger.warning(f"market_context_daily document for {trade_date} does not exist or has empty market_context; skipping sector insert for {sector_name}")
                return False

            sector_data_sub = context_data.get("sector_data") or context_data.get("sector_context")
            if not isinstance(sector_data_sub, dict):
                sector_data_sub = {
                    "trend": context_data.get("trend") or context_data.get("sector_trend") or "NEUTRAL",
                    "strength": context_data.get("strength") or "NEUTRAL",
                    "momentum": context_data.get("momentum") or "NEUTRAL",
                    "breadth": float(context_data.get("breadth") or 0.0),
                    "leaders": context_data.get("leaders") or [],
                    "laggards": context_data.get("laggards") or [],
                    "relative_strength": int(context_data.get("relative_strength") or context_data.get("relative_strength_ranking") or 0),
                    "institutional_interest": context_data.get("institutional_interest") or "NEUTRAL",
                    "volume_strength": context_data.get("volume_strength") or "NORMAL",
                    "score": float(context_data.get("score") or 50.0),
                    "summary": context_data.get("summary") or f"Sector context for {sector_name}"
                }

            now_iso = datetime.utcnow().isoformat()

            await cls.get_market_collection().update_one(
                {"trade_date": trade_date},
                {
                    "$set": {
                        "trade_date": trade_date,
                        "market_date": trade_date,
                        "generated_at": now_iso,
                        f"sector_context.{sector_name}": sector_data_sub
                    }
                },
                upsert=False
            )
            return True

        except Exception as e:
            logger.error(f"Error inserting sector context into unified market_context_daily: {e}")
            return False

    @classmethod
    async def get_unified_context(cls, trade_date: str) -> Optional[Dict[str, Any]]:
        try:
            return await cls.get_market_collection().find_one(
                {"$or": [{"trade_date": trade_date}, {"market_date": trade_date}]},
                {"_id": 0}
            )
        except Exception as e:
            logger.error(f"Error fetching unified market context for {trade_date}: {e}")
            return None
