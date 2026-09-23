import enum
import datetime
from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, ForeignKey, Enum, JSON
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

class TenderStatus(str, enum.Enum):
    NEW = "NEW"
    UNDER_REVIEW = "UNDER_REVIEW"
    BIDDING = "BIDDING"
    DISCARDED = "DISCARDED"

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class CompanyProfile(Base):
    __tablename__ = "company_profiles"

    id = Column(Integer, primary_key=True, index=True)
    company_name = Column(String(255), default="HUERI Limited")
    description = Column(Text, nullable=False)
    core_services = Column(JSON, nullable=False)
    target_geographies = Column(JSON, nullable=False)
    minimum_score_threshold = Column(Float, default=6.5)
    phone_number = Column(String(50), nullable=True)
    email_notifications = Column(Boolean, default=True)
    sms_notifications = Column(Boolean, default=True)

class Tender(Base):
    __tablename__ = "tenders"

    id = Column(Integer, primary_key=True, index=True)
    external_id = Column(String(255), unique=True, index=True, nullable=False)
    source = Column(String(100), index=True, nullable=False)
    title = Column(String(500), nullable=False)
    buyer = Column(String(255), nullable=True)
    url = Column(Text, nullable=False)
    deadline_str = Column(String(100), nullable=True)
    raw_summary = Column(Text, nullable=True)
    is_mock = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    analysis = relationship("TenderAnalysis", back_populates="tender", uselist=False)

class TenderAnalysis(Base):
    __tablename__ = "tender_analyses"

    id = Column(Integer, primary_key=True, index=True)
    tender_id = Column(Integer, ForeignKey("tenders.id"), unique=True, nullable=False)
    relevance_score = Column(Float, nullable=False)
    is_fit = Column(Boolean, nullable=False)
    executive_summary = Column(Text, nullable=False)
    matched_services = Column(JSON, nullable=False)
    eligibility_gaps = Column(JSON, nullable=False)
    suggested_next_steps = Column(JSON, nullable=False)
    status = Column(Enum(TenderStatus), default=TenderStatus.NEW)
    evaluated_at = Column(DateTime, default=datetime.datetime.utcnow)

    tender = relationship("Tender", back_populates="analysis")
