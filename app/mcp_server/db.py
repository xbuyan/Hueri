"""Database access for the MCP service: Postgres only, SELECT-only role expected.

The real boundary is the database role (GRANT SELECT on tenders and
tender_analyses only). The connection settings below are a second layer:
sessions open read-only, queries time out, idle transactions are cut.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

from .settings import ConfigError

REQUIRED_URL_PREFIX = "postgresql+asyncpg://"


def database_url_from_env(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    url = env.get("MCP_DATABASE_URL", "").strip()
    if not url:
        raise ConfigError("MCP_DATABASE_URL is required (use the SELECT-only role)")
    if not url.startswith(REQUIRED_URL_PREFIX):
        raise ConfigError(f"MCP_DATABASE_URL must start with {REQUIRED_URL_PREFIX}")
    return url


def make_engine(url: str, *, pool_size: int = 5, max_overflow: int = 5):
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "default_transaction_read_only": "on",
                "statement_timeout": "5000",
                "idle_in_transaction_session_timeout": "10000",
                "application_name": "hueri-mcp",
            }
        },
    )


def make_session_factory(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(engine, expire_on_commit=False)
