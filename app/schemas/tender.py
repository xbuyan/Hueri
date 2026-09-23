from datetime import datetime
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field

class TenderStatus(str, Enum):
    NEW = "NEW"
    REVIEWED = "REVIEWED"
    SHORTLISTED = "SHORTLISTED"
    ARCHIVED = "ARCHIVED"

class TenderAnalysisBase(BaseModel):
    relevance_score: float = Field(..., ge=0.0, le=100.0)
    is_fit: bool
    executive_summary: Optional[str] = None
    matched_services: List[str] = Field(default_factory=list)
    eligibility_gaps: List[str] = Field(default_factory=list)
    suggested_next_steps: List[str] = Field(default_factory=list)
    status: TenderStatus = TenderStatus.NEW

class TenderAnalysisResponse(TenderAnalysisBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tender_id: int
    evaluated_at: datetime

class TenderBase(BaseModel):
    external_id: str
    source: str
    title: str
    buyer: str
    url: Optional[str] = None
    deadline_str: Optional[str] = None
    raw_summary: Optional[str] = None
    is_mock: bool = False

class TenderResponse(TenderBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    analysis: Optional[TenderAnalysisResponse] = None
