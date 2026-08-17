import logging
from typing import Dict, Any, List
from pymongo.errors import DuplicateKeyError
from database import get_database

logger = logging.getLogger("uvicorn.error")

class MLEventRepository:
    """Repository for ml_events_log collection"""
    
    @staticmethod
    def get_collection():
        return get_database()["ml_events_log"]

    @classmethod
    async def create_indexes(cls):
        try:
            collection = cls.get_collection()
            await collection.create_index("event_id", unique=True)
            await collection.create_index("candidate_id")
            await collection.create_index("paper_trade_id")
            await collection.create_index("event_type")
            await collection.create_index("event_time")
            
            # TTL index for events log (e.g. 730 days)
            await collection.create_index("created_at", expireAfterSeconds=63072000)
        except Exception as e:
            logger.error(f"Error creating indexes for MLEventRepository: {e}")

    @classmethod
    async def insert_event(cls, event_data: Dict[str, Any]) -> bool:
        try:
            # Events are append-only
            await cls.get_collection().update_one(
                {"event_id": event_data["event_id"]},
                {"$setOnInsert": event_data},
                upsert=True
            )
            return True
        except DuplicateKeyError:
            return False
        except Exception as e:
            logger.error(f"Error inserting ML event: {e}")
            return False
