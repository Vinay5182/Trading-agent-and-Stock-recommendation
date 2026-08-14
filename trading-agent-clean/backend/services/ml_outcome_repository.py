import logging
from typing import Dict, Any, List, Optional
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
            await collection.create_index("outcome_id", unique=True)
            await collection.create_index("paper_trade_id")
            await collection.create_index("result")
            await collection.create_index("max_rr")
            await collection.create_index("strategy_version")
            await collection.create_index("created_at")
        except Exception as e:
            logger.error(f"Error creating indexes for MLOutcomeRepository: {e}")

    @classmethod
    async def insert_outcome(cls, outcome_data: Dict[str, Any]) -> bool:
        try:
            candidate_id = outcome_data.get("candidate_id")
            if not candidate_id:
                logger.error("insert_outcome called without candidate_id")
                return False
            await cls.get_collection().update_one(
                {"candidate_id": candidate_id},
                {"$setOnInsert": outcome_data},
                upsert=True
            )
            return True
        except DuplicateKeyError:
            return False
        except Exception as e:
            logger.error(f"Error inserting ML outcome: {e}")
            return False

    @classmethod
    async def get_outcome_by_candidate_id(cls, candidate_id: str) -> Optional[Dict[str, Any]]:
        try:
            return await cls.get_collection().find_one({"candidate_id": candidate_id}, {"_id": 0})
        except Exception as e:
            logger.error(f"Error finding outcome for candidate {candidate_id}: {e}")
            return None

    @classmethod
    async def get_outcome_by_trade_id(cls, paper_trade_id: str) -> Optional[Dict[str, Any]]:
        try:
            return await cls.get_collection().find_one({"paper_trade_id": paper_trade_id}, {"_id": 0})
        except Exception as e:
            logger.error(f"Error finding outcome for paper trade {paper_trade_id}: {e}")
            return None

    @classmethod
    async def get_outcome_by_id(cls, outcome_id: str) -> Optional[Dict[str, Any]]:
        try:
            return await cls.get_collection().find_one({"outcome_id": outcome_id}, {"_id": 0})
        except Exception as e:
            logger.error(f"Error finding outcome {outcome_id}: {e}")
            return None

    @classmethod
    async def list_outcomes_by_result(cls, result: str, limit: int = 500) -> List[Dict[str, Any]]:
        try:
            cursor = cls.get_collection().find({"result": result}, {"_id": 0}).sort("created_at", -1).limit(limit)
            return [doc async for doc in cursor]
        except Exception as e:
            logger.error(f"Error listing outcomes for result {result}: {e}")
            return []

