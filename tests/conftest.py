"""Shared test configuration.

These env vars must be set BEFORE the app is imported (config.py reads
the environment once at import time).
"""

import asyncio
import os

import pytest

os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("ENVIRONMENT", "testing")
os.environ.setdefault("ALERTS_ENABLED", "true")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by a per-test SQLite database.

    Patches app.db.engine/async_session so both the startup hook
    (init_db) and the get_db dependency hit the test database.
    """
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app import db as app_db
    from main import app

    test_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    test_sessionmaker = async_sessionmaker(
        test_engine, expire_on_commit=False, class_=AsyncSession
    )
    monkeypatch.setattr(app_db, "engine", test_engine)
    monkeypatch.setattr(app_db, "async_session", test_sessionmaker)

    with TestClient(app) as c:
        yield c

    asyncio.run(test_engine.dispose())
