import logging
from contextlib import asynccontextmanager

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from config import settings, validate_settings


logger = logging.getLogger("uvicorn.error")
mongo_client: AsyncIOMotorClient | None = None


async def connect_to_mongo() -> None:
    global mongo_client
    validate_settings(settings)
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
    validation = validate_settings(settings)
    await connect_to_mongo()
    from services.mongo_indexes import ensure_active_indexes
    from services.paper_automation import initialize_scheduler_status, shutdown_paper_automation, start_paper_automation_once
    from ai.daily_ohlcv_collector import shutdown_daily_ohlcv_scheduler, start_daily_ohlcv_scheduler_once

    automation_started = False
    daily_ohlcv_scheduler_started = False
    try:
        from services.ml_pipeline import ml_pipeline
        await ml_pipeline.initialize_indexes()
        
        index_summary = await ensure_active_indexes(get_database())
        logger.info(
            "Critical Mongo indexes verified expected=%s verified=%s created=%s",
            index_summary.get("critical_expected"),
            len(index_summary.get("critical_verified", [])),
            len(index_summary.get("critical_created", [])),
        )
        if validation["automation_disabled"]:
            logger.info("Smoke read-only startup mode active; paper automation disabled")
            yield
            return
        await initialize_scheduler_status(get_database())
        from services.tradingview_manager import tradingview_manager
        await tradingview_manager.validate_preference_on_restart()
        automation_task = start_paper_automation_once()
        automation_started = True
        logger.info("Registered paper automation background task name=%s", automation_task.get_name())
        daily_task = start_daily_ohlcv_scheduler_once(get_database)
        if daily_task is not None:
            daily_ohlcv_scheduler_started = True
            logger.info("Registered daily OHLCV scheduler background task name=%s", daily_task.get_name())
        yield
    finally:
        if daily_ohlcv_scheduler_started:
            await shutdown_daily_ohlcv_scheduler()
        if automation_started:
            await shutdown_paper_automation()
        await close_mongo_connection()
