from contextlib import asynccontextmanager

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from config import settings


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
    try:
        yield
    finally:
        await close_mongo_connection()
