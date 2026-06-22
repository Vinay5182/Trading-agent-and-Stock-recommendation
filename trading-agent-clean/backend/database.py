import logging
from contextlib import asynccontextmanager

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from config import settings


logger = logging.getLogger("uvicorn.error")
mongo_client: AsyncIOMotorClient | None = None


async def connect_to_mongo() -> None:
    global mongo_client
    mongo_client = AsyncIOMotorClient(settings.MONGO_URI)


async def close_mongo_connection() -> None:
    global mongo_client
    if mongo_client is not None:
        mongo_client.close()
        mongo_client = None


def get_database() -> AsyncIOMotorDatabase:
    if mongo_client is None:
        raise RuntimeError("MongoDB client is not initialized")
    return mongo_client[settings.DATABASE_NAME]


@asynccontextmanager
async def lifespan(app):
    await connect_to_mongo()
    from services.mongo_indexes import ensure_active_indexes
    from services.paper_automation import initialize_scheduler_status, shutdown_paper_automation, start_paper_automation_once

    await ensure_active_indexes(get_database())
    await initialize_scheduler_status(get_database())
    automation_task = start_paper_automation_once()
    logger.info("Registered paper automation background task name=%s", automation_task.get_name())
    try:
        yield
    finally:
        await shutdown_paper_automation()
        await close_mongo_connection()
