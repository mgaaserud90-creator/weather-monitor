"""
Async PostgreSQL database connection and session management.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config.loader import get_config


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_database() -> None:
    """Initialize the async database engine and session factory."""
    global _engine, _session_factory

    config = get_config()
    _engine = create_async_engine(
        config.postgres.database_url,
        pool_size=config.postgres.min_connections,
        max_overflow=config.postgres.max_connections - config.postgres.min_connections,
        pool_pre_ping=True,
        pool_recycle=300,
        echo=False,
    )

    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def get_session() -> AsyncSession:
    """Get a new async database session."""
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _session_factory()


async def close_database() -> None:
    """Close the database engine and release all connections."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
