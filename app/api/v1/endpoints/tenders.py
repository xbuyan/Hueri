from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from typing import List

from app.db.session import get_db
from app.models.tender import Tender, TenderAnalysis
from app.schemas.tender import TenderResponse

router = APIRouter(prefix="/tenders", tags=["Tenders"])

@router.get("", response_model=List[TenderResponse], status_code=status.HTTP_200_OK)
async def list_tenders(
    min_score: float = Query(0.0, ge=0.0, le=100.0),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db)
):
    stmt = (
        select(Tender)
        .outerjoin(TenderAnalysis)
        .options(selectinload(Tender.analysis))
        .where(TenderAnalysis.relevance_score >= min_score)
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(stmt)
    return result.scalars().all()
