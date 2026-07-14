import logging
from typing import Dict, Any, List
from pymongo.errors import DuplicateKeyError
from database import get_database

logger = logging.getLogger("uvicorn.error")

class MLDecisionRepository:
    """Repository for ml_candidate_decisions collection"""
    
    @staticmethod
    def get_collection():
        return get_database()["ml_candidate_decisions"]

    @classmethod
    async def create_indexes(cls):
        try:
            collection = cls.get_collection()
            await collection.create_index("candidate_id")
            await collection.create_index("paper_trade_id")
            await collection.create_index("decision_type")
            await collection.create_index("created_at")
        except Exception as e:
            logger.error(f"Error creating indexes for MLDecisionRepository: {e}")

    @classmethod
    async def insert_decision(cls, decision_data: Dict[str, Any]) -> bool:
        try:
            # Decisions are likely append-only
            await cls.get_collection().insert_one(decision_data)
            return True
        except DuplicateKeyError:
            return False
        except Exception as e:
            logger.error(f"Error inserting ML decision: {e}")
            return False
