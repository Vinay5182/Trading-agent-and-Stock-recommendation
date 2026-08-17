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
            await collection.create_index([("candidate_id", 1), ("decision_type", 1)], unique=True)
            await collection.create_index("paper_trade_id")
            await collection.create_index("decision_type")
            await collection.create_index("created_at")
        except Exception as e:
            logger.error(f"Error creating indexes for MLDecisionRepository: {e}")

    @classmethod
    async def insert_decision(cls, decision_data: Dict[str, Any]) -> bool:
        try:
            candidate_id = decision_data.get("candidate_id")
            decision_type = decision_data.get("decision_type", "TRADE_CLOSURE")
            if not candidate_id:
                logger.error("insert_decision called without candidate_id")
                return False
            await cls.get_collection().update_one(
                {"candidate_id": candidate_id, "decision_type": decision_type},
                {"$setOnInsert": decision_data},
                upsert=True
            )
            return True
        except DuplicateKeyError:
            return False
        except Exception as e:
            logger.error(f"Error inserting ML decision: {e}")
            return False
