import logging
from typing import Dict, Any, List
from pymongo.errors import DuplicateKeyError
from database import get_database

logger = logging.getLogger("uvicorn.error")

class MLMarketRepository:
    """Repository for market_context_daily and sector_context_daily collections"""
    
    @staticmethod
    def get_market_collection():
        return get_database()["market_context_daily"]
        
    @staticmethod
    def get_sector_collection():
        return get_database()["sector_context_daily"]

    @classmethod
    async def create_indexes(cls):
        try:
            market_collection = cls.get_market_collection()
            await market_collection.create_index("market_date", unique=True)
            await market_collection.create_index("created_at")
            
            sector_collection = cls.get_sector_collection()
            await sector_collection.create_index([("sector", 1), ("market_date", 1)], unique=True)
            await sector_collection.create_index("market_date")
            await sector_collection.create_index("created_at")
        except Exception as e:
            logger.error(f"Error creating indexes for MLMarketRepository: {e}")

    @classmethod
    async def insert_market_context(cls, context_data: Dict[str, Any]) -> bool:
        try:
            await cls.get_market_collection().update_one(
                {"market_date": context_data["market_date"]},
                {"$setOnInsert": context_data},
                upsert=True
            )
            return True
        except DuplicateKeyError:
            return False
        except Exception as e:
            logger.error(f"Error inserting ML market context: {e}")
            return False

    @classmethod
    async def insert_sector_context(cls, context_data: Dict[str, Any]) -> bool:
        try:
            await cls.get_sector_collection().update_one(
                {"sector": context_data["sector"], "market_date": context_data["market_date"]},
                {"$setOnInsert": context_data},
                upsert=True
            )
            return True
        except DuplicateKeyError:
            return False
        except Exception as e:
            logger.error(f"Error inserting ML sector context: {e}")
            return False
