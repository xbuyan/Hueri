import json
import logging
from contextlib import asynccontextmanager
from typing import List, Optional

import redis.asyncio as aioredis
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Depends, HTTPException, Query, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.core.logging import setup_logging
from app.db import engine, get_db, init_db
from app.models import Tender, TenderAnalysis, User
from app.schemas import UserCreate, UserOut, Token, TenderOut
from app.auth import get_password_hash, verify_password, create_access_token, get_current_user
from app.services.pipeline import (
    STATUS_QUEUED,
    run_collection_pipeline,
)

setup_logging()
logger = logging.getLogger("main")

# --- Rate limiting ---


limiter = Limiter(key_func=get_remote_address, enabled=settings.RATE_LIMIT_ENABLED)

# --- Redis cache (optional; disabled when REDIS_URL is unset) ---

_redis: Optional[aioredis.Redis] = None


async def _get_redis() -> Optional[aioredis.Redis]:
    global _redis
    if not settings.REDIS_URL:
        return None
    if _redis is None:
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # arq job-enqueue pool (None when Redis is not configured)
    global _arq_pool
    if settings.REDIS_URL:
        try:
            _arq_pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
            logger.info("arq pool created; collection runs will be queued")
        except Exception as e:
            logger.warning("arq pool unavailable (%s); pipeline will run inline", e)
            _arq_pool = None
    logger.info(
        "Startup complete",
        extra={
            "environment": settings.ENVIRONMENT,
            "alerts_enabled": settings.ALERTS_ENABLED,
            "redis_enabled": bool(settings.REDIS_URL),
            "queue_enabled": _arq_pool is not None,
            "smtp_configured": bool(settings.SMTP_HOST),
            "sms_configured": bool(settings.SMS_API_URL),
        },
    )
    yield
    if _arq_pool is not None:
        await _arq_pool.aclose()
    await engine.dispose()
    if _redis is not None:
        await _redis.aclose()


app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)
_arq_pool = None

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SecurityHeadersMiddleware:
    """Adds standard hardened-response headers to every reply."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                for name, value in [
                    (b"X-Content-Type-Options", b"nosniff"),
                    (b"X-Frame-Options", b"DENY"),
                    (b"Referrer-Policy", b"strict-origin-when-cross-origin"),
                ]:
                    headers.append((name, value))
                if settings.ENVIRONMENT == "production":
                    headers.append(
                        (b"Strict-Transport-Security", b"max-age=63072000; includeSubDomains")
                    )
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeadersMiddleware)

# --- AUTH ROUTES ---


@app.post("/api/auth/register", response_model=UserOut)
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def register(request: Request, user_in: UserCreate, db: AsyncSession = Depends(get_db)):
    stmt = select(User).where(User.email == user_in.email)
    res = await db.execute(stmt)
    if res.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        email=user_in.email,
        hashed_password=get_password_hash(user_in.password),
        full_name=user_in.full_name
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@app.post("/api/auth/login", response_model=Token)
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    stmt = select(User).where(User.email == form_data.username)
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect email or password")

    access_token = create_access_token(data={"sub": user.email})
    return {"access_token": access_token, "token_type": "bearer"}


@app.get("/api/auth/me", response_model=UserOut)
async def read_me(current_user: User = Depends(get_current_user)):
    """Return the authenticated user's profile."""
    return current_user


# --- TENDER ROUTES ---


@app.get("/api/tenders", response_model=List[TenderOut])
async def get_tenders(
    min_score: float = Query(0.0, ge=0.0, le=100.0),
    db: AsyncSession = Depends(get_db)
):
    """Fetch tenders filtered by minimum AI relevance score."""
    stmt = (
        select(Tender)
        .outerjoin(TenderAnalysis)
        .options(selectinload(Tender.analysis))
        .where(
            (TenderAnalysis.relevance_score >= min_score) | (TenderAnalysis.id == None)
        )
        .order_by(Tender.id.desc())
    )
    res = await db.execute(stmt)
    return res.scalars().all()


@app.post("/api/tenders/trigger-collect")
@limiter.limit(settings.RATE_LIMIT_EXPENSIVE)
async def trigger_collection_pipeline(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Trigger a collection run: collectors, AI analysis, and management
    alerts for high-fit tenders. Requires authentication.

    With Redis configured the run is queued on the arq worker and this
    returns {"status": "queued"} immediately; poll /api/tenders/collect-status
    for progress. Without Redis the pipeline runs inline (dev/CI behaviour).
    """
    if _arq_pool is not None:
        try:
            from app.worker import JOB_ID  # local import: keeps worker optional

            await _arq_pool.enqueue_job("run_collection_job", _job_id=JOB_ID)
            return {"status": STATUS_QUEUED, "job_id": JOB_ID}
        except Exception as e:
            logger.warning("Could not enqueue collection job (%s); running inline", e)

    summary = await run_collection_pipeline({})
    logger.info("Inline pipeline run: %s", summary)

    res_all = await db.execute(
        select(Tender).options(selectinload(Tender.analysis)).order_by(Tender.id.desc())
    )
    return res_all.scalars().all()


@app.get("/api/tenders/collect-status")
async def collection_job_status(current_user: User = Depends(get_current_user)):
    """Report the state of the queued collection run.

    One of: idle | queued | running | complete | failed | inline (no queue).
    """
    if _arq_pool is None:
        return {"status": "inline"}
    try:
        from app.worker import JOB_STATUS_KEY  # local import: keeps worker optional

        redis = await _get_redis()
        if redis is None:
            return {"status": "unknown"}
        raw = await redis.get(JOB_STATUS_KEY)
        return json.loads(raw) if raw else {"status": "idle"}
    except Exception as e:
        logger.warning("Could not read collection job status: %s", e)
        return {"status": "unknown"}


# --- ANALYTICS ROUTES ---


@app.get("/api/analytics/summary")
async def get_analytics_summary(db: AsyncSession = Depends(get_db)):
    """Return dashboard analytics summary for tenders (cached via Redis when configured)."""
    cache_key = "analytics:summary:v1"

    try:
        redis = await _get_redis()
        if redis is not None:
            cached = await redis.get(cache_key)
            if cached:
                return json.loads(cached)
    except Exception as e:
        logger.warning("Redis cache unavailable, computing fresh: %s", e)

    total_tenders = (await db.execute(select(func.count(Tender.id)))).scalar() or 0
    total_analyses = (await db.execute(select(func.count(TenderAnalysis.id)))).scalar() or 0
    fit_tenders = (
        await db.execute(
            select(func.count(TenderAnalysis.id)).where(TenderAnalysis.is_fit == True)
        )
    ).scalar() or 0

    summary = {
        "total_tenders": total_tenders,
        "total_analyzed": total_analyses,
        "high_fit_tenders": fit_tenders,
    }

    try:
        redis = await _get_redis()
        if redis is not None:
            await redis.setex(cache_key, settings.ANALYTICS_CACHE_TTL_SECONDS, json.dumps(summary))
    except Exception as e:
        logger.warning("Redis cache write failed: %s", e)

    return summary
