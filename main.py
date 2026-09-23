import logging
from contextlib import asynccontextmanager
from typing import List, Optional

import redis.asyncio as aioredis
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
from app.models import Tender, TenderAnalysis, User, TenderStatus, Notification
from app.schemas import UserCreate, UserOut, Token, TenderOut
from app.auth import get_password_hash, verify_password, create_access_token, get_current_user
from app.collectors.ungm import ungm_collector
from app.collectors.ppip import ppip_collector
from app.collectors.worldbank import worldbank_collector
from app.services.ai_agent import tender_agent
from app.services.notifier import notifier

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
    logger.info(
        "Startup complete",
        extra={
            "environment": settings.ENVIRONMENT,
            "alerts_enabled": settings.ALERTS_ENABLED,
            "redis_enabled": bool(settings.REDIS_URL),
            "smtp_configured": bool(settings.SMTP_HOST),
            "sms_configured": bool(settings.SMS_API_URL),
        },
    )
    yield
    await engine.dispose()
    if _redis is not None:
        await _redis.aclose()


app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)

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

HUERI_PROFILE = {
    "company_name": "HUERI Limited",
    "description": "Environmental, social, and sustainability engineering consultancy based in Nairobi, Kenya.",
    "core_services": ["Environmental Impact Assessment (EIA)", "ESIA", "Environmental Audits", "Resettlement Action Plans (RAP)", "M&E"],
    "target_geographies": ["Kenya", "Uganda", "Tanzania", "East Africa"]
}


def _tender_alert_dict(notice: dict, tender: Tender, ai_eval) -> dict:
    """Plain-dict snapshot for the notifier.

    The notifier must never touch ORM relationship attributes
    (e.g. tender.analysis): lazy-loading them outside the async session
    context raises MissingGreenlet. Column values on `tender` are already
    loaded, so they are safe.
    """
    return {
        "id": tender.id,
        "external_id": tender.external_id,
        "source": tender.source,
        "title": tender.title,
        "buyer": tender.buyer,
        "url": tender.url,
        "deadline_str": tender.deadline_str,
        "analysis": {
            "relevance_score": ai_eval.relevance_score,
            "executive_summary": ai_eval.executive_summary,
            "eligibility_gaps": ai_eval.eligibility_gaps,
        },
    }

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


@app.post("/api/tenders/trigger-collect", response_model=List[TenderOut])
@limiter.limit(settings.RATE_LIMIT_EXPENSIVE)
async def trigger_collection_pipeline(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run all collectors, analyze new notices, and alert management
    about high-fit tenders. Requires authentication."""
    all_notices = []

    for collector in [ungm_collector, ppip_collector, worldbank_collector]:
        try:
            all_notices.extend(collector.fetch_recent_notices())
        except Exception as e:
            logger.error("Collector failed: %s", e)

    high_fit_tenders: List[dict] = []

    for notice in all_notices:
        stmt = select(Tender).where(Tender.external_id == notice["external_id"])
        res = await db.execute(stmt)
        if not res.scalar_one_or_none():
            tender = Tender(**notice, is_mock=False)
            db.add(tender)
            await db.flush()

            try:
                ai_eval = tender_agent.analyze_tender(
                    tender.title, tender.buyer or "", tender.raw_summary or "", HUERI_PROFILE
                )
                analysis = TenderAnalysis(
                    tender_id=tender.id,
                    relevance_score=ai_eval.relevance_score,
                    is_fit=ai_eval.is_fit,
                    executive_summary=ai_eval.executive_summary,
                    matched_services=ai_eval.matched_services,
                    eligibility_gaps=ai_eval.eligibility_gaps,
                    suggested_next_steps=ai_eval.suggested_next_steps,
                    status=TenderStatus.NEW
                )
                db.add(analysis)
                if ai_eval.relevance_score >= settings.ALERT_MIN_SCORE:
                    high_fit_tenders.append(_tender_alert_dict(notice, tender, ai_eval))
            except Exception as eval_err:
                logger.error("AI analysis failed for tender %s: %s", tender.external_id, eval_err)

    await db.commit()

    # --- Management alerts (email + SMS) ---
    if settings.ALERTS_ENABLED and high_fit_tenders:
        try:
            alert_summary = await notifier.send_tender_alert(high_fit_tenders)
            logger.info("Alert run summary: %s", alert_summary)

            email_status = (
                "sent" if alert_summary["emails_sent"]
                else "logged" if alert_summary["emails_logged"]
                else "skipped"
            )
            sms_status = (
                "sent" if alert_summary["sms_sent"]
                else "logged" if alert_summary["sms_logged"]
                else "skipped"
            )
            for t in high_fit_tenders:
                if email_status != "skipped":
                    db.add(Notification(
                        tender_id=t["id"], channel="email",
                        recipient=", ".join(settings.alert_email_recipients_list()) or "management",
                        status=email_status,
                    ))
                if sms_status != "skipped":
                    db.add(Notification(
                        tender_id=t["id"], channel="sms",
                        recipient=", ".join(settings.alert_sms_recipients_list()) or "management",
                        status=sms_status,
                    ))
            await db.commit()
        except Exception:
            logger.exception("Alerting failed for %d tenders", len(high_fit_tenders))

    res_all = await db.execute(
        select(Tender).options(selectinload(Tender.analysis)).order_by(Tender.id.desc())
    )
    return res_all.scalars().all()


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
                import json
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
            import json
            await redis.setex(cache_key, settings.ANALYTICS_CACHE_TTL_SECONDS, json.dumps(summary))
    except Exception as e:
        logger.warning("Redis cache write failed: %s", e)

    return summary
