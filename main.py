import logging
from typing import List, Optional
from fastapi import FastAPI, Depends, HTTPException, Query, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Base, Tender, TenderAnalysis, User, TenderStatus
from app.schemas import UserCreate, UserOut, Token, TenderOut
from app.auth import get_password_hash, verify_password, create_access_token
from app.collectors.ungm import ungm_collector
from app.collectors.ppip import ppip_collector
from app.collectors.worldbank import worldbank_collector
from app.services.ai_agent import tender_agent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

engine = create_async_engine(settings.DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def get_db():
    async with async_session() as session:
        yield session

app = FastAPI(title=settings.PROJECT_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

HUERI_PROFILE = {
    "company_name": "HUERI Limited",
    "description": "Environmental, social, and sustainability engineering consultancy based in Nairobi, Kenya.",
    "core_services": ["Environmental Impact Assessment (EIA)", "ESIA", "Environmental Audits", "Resettlement Action Plans (RAP)", "M&E"],
    "target_geographies": ["Kenya", "Uganda", "Tanzania", "East Africa"]
}

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# --- AUTH ROUTES ---

@app.post("/api/auth/register", response_model=UserOut)
async def register(user_in: UserCreate, db: AsyncSession = Depends(get_db)):
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
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    stmt = select(User).where(User.email == form_data.username)
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect email or password")
    
    access_token = create_access_token(data={"sub": user.email})
    return {"access_token": access_token, "token_type": "bearer"}

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
async def trigger_collection_pipeline(db: AsyncSession = Depends(get_db)):
    all_notices = []
    
    for collector in [ungm_collector, ppip_collector, worldbank_collector]:
        try:
            all_notices.extend(collector.fetch_recent_notices())
        except Exception as e:
            logger.error("Collector failed: %s", e)

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
            except Exception as eval_err:
                logger.error("AI analysis failed for tender %s: %s", tender.external_id, eval_err)

    await db.commit()
    res_all = await db.execute(
        select(Tender).options(selectinload(Tender.analysis)).order_by(Tender.id.desc())
    )
    return res_all.scalars().all()

# --- ANALYTICS ROUTES ---

@app.get("/api/analytics/summary")
async def get_analytics_summary(db: AsyncSession = Depends(get_db)):
    """Return dashboard analytics summary for tenders."""
    total_tenders = (await db.execute(select(func.count(Tender.id)))).scalar() or 0
    total_analyses = (await db.execute(select(func.count(TenderAnalysis.id)))).scalar() or 0
    fit_tenders = (
        await db.execute(
            select(func.count(TenderAnalysis.id)).where(TenderAnalysis.is_fit == True)
        )
    ).scalar() or 0

    return {
        "total_tenders": total_tenders,
        "total_analyzed": total_analyses,
        "high_fit_tenders": fit_tenders,
    }
