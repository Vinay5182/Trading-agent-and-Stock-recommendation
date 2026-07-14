import logging
from typing import Dict, Any, List, Optional
from pymongo.errors import DuplicateKeyError
from database import get_database

logger = logging.getLogger("uvicorn.error")

class MLCandidateRepository:
    """Repository for ml_candidates and ml_candidate_daily_progress collections"""
    
    @staticmethod
    def get_collection():
        return get_database()["ml_candidates"]
        
    @staticmethod
    def get_progress_collection():
        return get_database()["ml_candidate_daily_progress"]

    @classmethod
    async def create_indexes(cls):
        try:
            collection = cls.get_collection()
            await collection.create_index("candidate_id", unique=True)
            await collection.create_index("paper_trade_id")
            await collection.create_index("setup_date")
            await collection.create_index("symbol")
            await collection.create_index("strategy_version")
            await collection.create_index("status")
            await collection.create_index("created_at")
            
            prog_collection = cls.get_progress_collection()
            await prog_collection.create_index([("candidate_id", 1), ("date", 1)], unique=True)
            await prog_collection.create_index("paper_trade_id")
            await prog_collection.create_index("date")
        except Exception as e:
            logger.error(f"Error creating indexes for MLCandidateRepository: {e}")

    @classmethod
    async def insert_candidate(cls, candidate_data: Dict[str, Any]) -> bool:
        try:
            await cls.get_collection().update_one(
                {"candidate_id": candidate_data["candidate_id"]},
                {"$setOnInsert": candidate_data},
                upsert=True
            )
            return True
        except DuplicateKeyError:
            return False
        except Exception as e:
            logger.error(f"Error inserting ML candidate {candidate_data.get('candidate_id')}: {e}")
            return False

    @classmethod
    async def update_candidate(cls, candidate_id: str, update_data: Dict[str, Any]) -> bool:
        try:
            result = await cls.get_collection().update_one(
                {"candidate_id": candidate_id},
                {"$set": update_data}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating ML candidate {candidate_id}: {e}")
            return False

    @classmethod
    async def find_candidate(cls, candidate_id: str) -> Optional[Dict[str, Any]]:
        try:
            return await cls.get_collection().find_one({"candidate_id": candidate_id})
        except Exception as e:
            logger.error(f"Error finding ML candidate {candidate_id}: {e}")
            return None

    @classmethod
    async def find_by_symbol(cls, symbol: str, limit: int = 100) -> List[Dict[str, Any]]:
        try:
            cursor = cls.get_collection().find({"symbol": symbol}).sort("created_at", -1).limit(limit)
            return [doc async for doc in cursor]
        except Exception as e:
            logger.error(f"Error finding ML candidates for symbol {symbol}: {e}")
            return []

    @classmethod
    async def link_paper_trade(cls, candidate_id: str, paper_trade_id: str) -> bool:
        return await cls.update_candidate(candidate_id, {"paper_trade_id": paper_trade_id})

    @classmethod
    async def append_daily_progress(cls, progress_data: Dict[str, Any]) -> bool:
        try:
            await cls.get_progress_collection().update_one(
                {"candidate_id": progress_data["candidate_id"], "date": progress_data["date"]},
                {"$setOnInsert": progress_data},
                upsert=True
            )
            return True
        except DuplicateKeyError:
            return False
        except Exception as e:
            logger.error(f"Error appending ML daily progress: {e}")
            return False

    @classmethod
    async def get_progress(cls, candidate_id: str) -> List[Dict[str, Any]]:
        try:
            cursor = cls.get_progress_collection().find({"candidate_id": candidate_id}).sort("date", 1)
            return [doc async for doc in cursor]
        except Exception as e:
            logger.error(f"Error getting progress for ML candidate {candidate_id}: {e}")
            return []
