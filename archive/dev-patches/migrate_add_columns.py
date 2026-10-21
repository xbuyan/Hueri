#!/usr/bin/env python3
"""Adds tenders.is_unreviewed and tenders.alerted_at if missing. Safe to re-run.
Run from the project root, with the venv active and the DB up:  python migrate_add_columns.py
Works on Postgres and SQLite, using DATABASE_URL from your app settings.
"""
import asyncio

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings


async def main():
    engine = create_async_engine(settings.DATABASE_URL)
    is_pg = engine.dialect.name == "postgresql"
    false_lit = "FALSE" if is_pg else "0"
    wanted = {
        "is_unreviewed": f"BOOLEAN NOT NULL DEFAULT {false_lit}",
        "alerted_at": "TIMESTAMP",
    }
    async with engine.begin() as conn:
        cols = await conn.run_sync(lambda c: {col["name"] for col in inspect(c).get_columns("tenders")})
        for name, ddl in wanted.items():
            if name in cols:
                print(f"tenders.{name}: already exists, skipped")
            else:
                await conn.execute(text(f"ALTER TABLE tenders ADD COLUMN {name} {ddl}"))
                print(f"tenders.{name}: added")
    await engine.dispose()


asyncio.run(main())
