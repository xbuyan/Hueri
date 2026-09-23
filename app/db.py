from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# SQLite (local dev) does not support server-side pooling; the pool kwargs
# only apply to production databases such as PostgreSQL (asyncpg).
engine_kwargs = {}
if settings.DATABASE_URL.startswith("postgresql"):
    engine_kwargs = {
        "pool_size": settings.DB_POOL_SIZE,
        "max_overflow": settings.DB_MAX_OVERFLOW,
        "pool_pre_ping": settings.DB_POOL_PRE_PING,
    }

engine = create_async_engine(settings.DATABASE_URL, echo=False, **engine_kwargs)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db():
    async with async_session() as session:
        yield session


async def init_db() -> None:
    """Create tables (dev convenience; Alembic owns schema in production)."""
    from app.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
