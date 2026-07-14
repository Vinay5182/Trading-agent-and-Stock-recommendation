import logging
from typing import Dict, Any, List
from pymongo.errors import DuplicateKeyError
from database import get_database

logger = logging.getLogger("uvicorn.error")

class MLOutcomeRepository:
    """Repository for ml_candidate_outcomes collection"""
    
    @staticmethod
    def get_collection():
        return get_database()["ml_candidate_outcomes"]

    @classmethod
    async def create_indexes(cls):
        try:
            collection = cls.get_collection()
            # Ensure 1:1 relationship
            await collection.create_index("candidate_id", unique=True)
            await collection.create_index("paper_trade_id")
            await collection.create_index("max_rr")
            await collection.create_index("strategy_version")
            await collection.create_index("created_at")
        except Exception as e:
            logger.error(f"Error creating indexes for MLOutcomeRepository: {e}")

    @classmethod
    async def insert_outcome(cls, outcome_data: Dict[str, Any]) -> bool:
        try:
            await cls.get_collection().update_one(
                {"candidate_id": outcome_data["candidate_id"]},
                {"$setOnInsert": outcome_data},
                upsert=True
            )
            return True
        except DuplicateKeyError:
            return False
        except Exception as e:
            logger.error(f"Error inserting ML outcome: {e}")
            return False
