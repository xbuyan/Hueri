from pydantic import BaseModel, EmailStr, Field
from typing import List, Optional
from datetime import datetime
from app.models import TenderStatus

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    email: Optional[str] = None

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    full_name: Optional[str] = None

class UserOut(BaseModel):
    id: int
    email: EmailStr
    full_name: Optional[str]

    class Config:
        from_attributes = True

class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, description="Minimum 8 characters")

class TenderAIAnalysisResult(BaseModel):
    relevance_score: float = Field(description="Relevance score from 0.0 to 10.0 based on core weights")
    is_fit: bool = Field(description="True if total match score >= 6.5")
    confidence_score: float = Field(description="Model confidence rating between 0.0 and 1.0")
    executive_summary: str = Field(description="Concise strategic summary highlighting project scope")
    matched_services: List[str] = Field(description="Capabilities directly aligned with HUERI profile")
    eligibility_gaps: List[str] = Field(description="Required certifications, licencing, or operational gaps")
    disqualification_risk: str = Field(description="LOW, MEDIUM, or HIGH risk rating based on mandatory requirements")
    suggested_next_steps: List[str] = Field(description="Priority actions for proposal strategy")

class TenderAnalysisOut(BaseModel):
    id: int
    relevance_score: float
    is_fit: bool
    executive_summary: str
    matched_services: List[str]
    eligibility_gaps: List[str]
    suggested_next_steps: List[str]
    status: TenderStatus
    evaluated_at: datetime

    class Config:
        from_attributes = True

class TenderOut(BaseModel):
    id: int
    external_id: str
    source: str
    title: str
    buyer: Optional[str]
    url: str
    deadline_str: Optional[str]
    is_mock: bool
    created_at: datetime
    analysis: Optional[TenderAnalysisOut] = None

    class Config:
        from_attributes = True

class StatusUpdatePayload(BaseModel):
    status: TenderStatus
