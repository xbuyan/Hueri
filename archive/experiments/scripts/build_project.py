import os
import zipfile

# Complete project structure and file definitions
PROJECT_FILES = {
    "app/__init__.py": "",
    
    "app/config.py": """import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    PROJECT_NAME: str = "TenderScout AI"
    SECRET_KEY: str = os.getenv("SECRET_KEY", "super-secret-production-key-change-in-env")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7

    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", 
        "postgresql+asyncpg://postgres:postgres@localhost:5432/tenderscout"
    )
    
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    AFRICASTALKING_USERNAME: str = os.getenv("AFRICASTALKING_USERNAME", "sandbox")
    AFRICASTALKING_API_KEY: str = os.getenv("AFRICASTALKING_API_KEY", "")

    COLLECTOR_MAX_RETRIES: int = int(os.getenv("COLLECTOR_MAX_RETRIES", "3"))
    ALLOW_MOCK_FALLBACK: bool = os.getenv("ALLOW_MOCK_FALLBACK", "false").lower() == "true"

    class Config:
        env_file = ".env"

settings = Settings()
""",

    "app/models.py": """import enum
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
""",

    "app/schemas.py": """from pydantic import BaseModel, EmailStr, Field
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
""",

    "app/auth.py": """import datetime
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[datetime.timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.datetime.utcnow() + (
        expires_delta or datetime.timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
""",

    "app/collectors/__init__.py": "",

    "app/collectors/ungm.py": """import logging
import time
from typing import List, Dict, Any
import requests
from bs4 import BeautifulSoup
from app.config import settings

logger = logging.getLogger("collectors.ungm")

class UNGMCollectorError(Exception):
    pass

class UNGMCollector:
    SEARCH_URL = "https://www.ungm.org/Public/Notice/Search"

    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36"
        }

    def fetch_recent_notices(self) -> List[Dict[str, Any]]:
        last_exc = None
        for attempt in range(1, settings.COLLECTOR_MAX_RETRIES + 1):
            try:
                res = requests.get(self.SEARCH_URL, headers=self.headers, timeout=15)
                res.raise_for_status()
                return self._parse_html(res.text)
            except Exception as exc:
                last_exc = exc
                logger.warning("UNGM attempt %d failed: %s", attempt, exc)
                time.sleep(2 ** (attempt - 1))
        raise UNGMCollectorError(f"UNGM collection failed: {last_exc}")

    def _parse_html(self, html: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        notices = []
        rows = soup.select("table.table tbody tr")
        for idx, row in enumerate(rows[:10]):
            cols = row.find_all("td")
            if len(cols) >= 3:
                title = cols[0].text.strip()
                buyer = cols[1].text.strip() if len(cols) > 1 else "UN Agency"
                notices.append({
                    "external_id": f"UNGM-{idx + 1000}",
                    "source": "UNGM",
                    "title": title,
                    "buyer": buyer,
                    "url": "https://www.ungm.org/Public/Notice",
                    "deadline_str": "Open",
                    "raw_summary": f"UNGM notice from {buyer}: {title}"
                })
        return notices

ungm_collector = UNGMCollector()
""",

    "app/collectors/ppip.py": """import logging
import time
from typing import List, Dict, Any
import requests
from bs4 import BeautifulSoup
from app.config import settings

logger = logging.getLogger("collectors.ppip")

class PPIPCollectorError(Exception):
    pass

class PPIPCollector:
    BASE_URL = "https://tenders.go.ke/tenders"

    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
        }

    def fetch_recent_notices(self) -> List[Dict[str, Any]]:
        last_exc = None
        for attempt in range(1, settings.COLLECTOR_MAX_RETRIES + 1):
            try:
                response = requests.get(self.BASE_URL, headers=self.headers, timeout=20)
                response.raise_for_status()
                return self._parse_html(response.text)
            except Exception as exc:
                last_exc = exc
                logger.warning("PPIP attempt %d failed: %s", attempt, exc)
                time.sleep(2 ** (attempt - 1))
        raise PPIPCollectorError(f"PPIP collection failed: {last_exc}")

    def _parse_html(self, html_content: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html_content, "html.parser")
        notices = []
        rows = soup.find_all("tr", class_="tender-row") or soup.find_all("tr")[1:]
        
        for idx, row in enumerate(rows):
            cols = row.find_all("td")
            if len(cols) >= 4:
                title_elem = cols[0].find("a")
                title = title_elem.text.strip() if title_elem else cols[0].text.strip()
                tender_url = title_elem["href"] if title_elem and "href" in title_elem.attrs else self.BASE_URL
                buyer = cols[1].text.strip()
                deadline = cols[3].text.strip()
                
                notices.append({
                    "external_id": f"PPIP-{idx + 1000}",
                    "source": "PPIP (Kenya)",
                    "title": title,
                    "buyer": buyer,
                    "url": tender_url if tender_url.startswith("http") else f"https://tenders.go.ke{tender_url}",
                    "deadline_str": deadline,
                    "raw_summary": f"Public tender issued by {buyer}: {title}",
                })
        return notices

ppip_collector = PPIPCollector()
""",

    "app/collectors/worldbank.py": """import logging
import time
from typing import List, Dict, Any
import requests
from app.config import settings

logger = logging.getLogger("collectors.worldbank")

class WorldBankCollectorError(Exception):
    pass

class WorldBankCollector:
    API_URL = "https://search.worldbank.org/api/v2/procnotices"

    def fetch_recent_notices(self) -> List[Dict[str, Any]]:
        params = {
            "format": "json",
            "rows": "15",
            "os": "0",
            "srt": "boarddate",
            "order": "desc",
            "countrycode_exact": "KE",
        }
        
        last_exc = None
        for attempt in range(1, settings.COLLECTOR_MAX_RETRIES + 1):
            try:
                res = requests.get(self.API_URL, params=params, timeout=15)
                res.raise_for_status()
                data = res.json()
                return self._parse_json(data)
            except Exception as exc:
                last_exc = exc
                logger.warning("WorldBank attempt %d failed: %s", attempt, exc)
                time.sleep(2 ** (attempt - 1))

        raise WorldBankCollectorError(f"WorldBank API collection failed: {last_exc}")

    def _parse_json(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        notices = []
        procnotices = data.get("procnotices", {})
        
        for key, item in procnotices.items():
            if not isinstance(item, dict):
                continue
            notices.append({
                "external_id": f"WB-{item.get('id', key)}",
                "source": "World Bank STEP",
                "title": item.get("project_name", "") + " - " + item.get("notice_title", ""),
                "buyer": item.get("owner", "World Bank Group"),
                "url": item.get("url", "https://projects.worldbank.org"),
                "deadline_str": item.get("submission_date", "N/A"),
                "raw_summary": item.get("notice_type", "") + ": " + item.get("notice_title", ""),
            })
        return notices

worldbank_collector = WorldBankCollector()
""",

    "app/services/__init__.py": "",

    "app/services/ai_agent.py": """import json
import logging
from google import genai
from google.genai import types
from app.config import settings
from app.schemas import TenderAIAnalysisResult

logger = logging.getLogger("services.ai_agent")

class TenderAgentError(Exception):
    pass

class TenderScoutAgent:
    def __init__(self):
        self.client = genai.Client(api_key=settings.GEMINI_API_KEY) if settings.GEMINI_API_KEY else None

    def analyze_tender(self, tender_title: str, tender_buyer: str, tender_description: str, company_profile: dict) -> TenderAIAnalysisResult:
        if not self.client:
            raise TenderAgentError("GEMINI_API_KEY is missing")

        system_instruction = f\"\"\"
You are the Lead Procurement & Technical Risk Evaluator for {company_profile.get('company_name', 'HUERI Limited')}.
Company Profile Context:
- Description: {company_profile.get('description')}
- Core Capabilities: {', '.join(company_profile.get('core_services', []))}
- Target Regions: {', '.join(company_profile.get('target_geographies', []))}

EVALUATION WEIGHTING & SCORING MATRIX (0.0 to 10.0):
1. Service Capability Match (40% Weight): EIA, ESIA, Environmental Audits, RAPs, M&E, Feasibility Studies.
2. Geographic Alignment (20% Weight): Priority for Kenya, Uganda, Tanzania, and East Africa.
3. Regulatory & Licencing Criteria (20% Weight): Explicitly identify mandatory NEMA Lead Expert / Firm licencing or local statutory registrations.
4. Scale & Strategic Fit (20% Weight): Size, consultancy vs civil works distinction.

DISQUALIFICATION & PENALTY RULES:
- If the tender is strictly for physical works/construction without consultancy/studies, apply a -5.0 penalty.
- If mandatory NEMA registration is required and not mentioned, tag in eligibility gaps and set disqualification_risk = HIGH.

Return structured JSON adhering to the output schema.
\"\"\"

        user_prompt = f\"\"\"
Evaluate Opportunity:
Title: {tender_title}
Buyer: {tender_buyer}
Description: {tender_description}
\"\"\"

        response = self.client.models.generate_content(
            model='gemini-2.5-flash',
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=TenderAIAnalysisResult,
                temperature=0.1,
            ),
        )
        return TenderAIAnalysisResult(**json.loads(response.text))

tender_agent = TenderScoutAgent()
""",

    "main.py": """import logging
from typing import List
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select

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
    allow_origins=["*"],
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
    res_all = await db.execute(select(Tender).order_by(Tender.id.desc()))
    return res_all.scalars().all()
""",

    "requirements.txt": """fastapi==0.111.0
uvicorn[standard]==0.30.1
sqlalchemy[asyncio]==2.0.30
asyncpg==0.29.0
pydantic==2.7.4
pydantic-settings==2.3.1
google-genai==0.1.1
requests==2.32.3
beautifulsoup4==4.12.3
python-jose[cryptography]==3.3.0
passlib[bcrypt]==1.7.4
python-multipart==0.0.9
""",

    "Dockerfile": """FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
""",

    "docker-compose.prod.yml": """version: '3.8'

services:
  db:
    image: postgres:16-alpine
    container_name: tenderscout_db_prod
    restart: always
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-postgres}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-postgres}
      POSTGRES_DB: ${POSTGRES_DB:-tenderscout}
    volumes:
      - postgres_prod_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 10s
      timeout: 5s
      retries: 5

  api:
    build: .
    container_name: tenderscout_api_prod
    restart: always
    command: uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
    environment:
      - DATABASE_URL=postgresql+asyncpg://${POSTGRES_USER:-postgres}:${POSTGRES_PASSWORD:-postgres}@db:5432/${POSTGRES_DB:-tenderscout}
      - GEMINI_API_KEY=${GEMINI_API_KEY}
      - SECRET_KEY=${SECRET_KEY}
    depends_on:
      db:
        condition: service_healthy

  nginx:
    image: nginx:alpine
    container_name: tenderscout_nginx
    restart: always
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - ./frontend/dist:/usr/share/nginx/html:ro
      - /etc/letsencrypt:/etc/letsencrypt:ro
    depends_on:
      - api

volumes:
  postgres_prod_data:
""",

    "nginx.conf": """server {
    listen 80;
    server_name tenderscout.hueri.co.ke;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name tenderscout.hueri.co.ke;

    ssl_certificate /etc/letsencrypt/live/tenderscout.hueri.co.ke/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/tenderscout.hueri.co.ke/privkey.pem;

    location / {
        root /usr/share/nginx/html;
        index index.html;
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://api:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
"""
}

def build_zip():
    zip_filename = "tenderscout_extended.zip"
    print(f"Generating files and packing into '{zip_filename}'...")
    
    with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
        for filepath, content in PROJECT_FILES.items():
            dirname = os.path.dirname(filepath)
            if dirname:
                os.makedirs(dirname, exist_ok=True)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            zipf.write(filepath)
            print(f" [+] Created: {filepath}")

    print(f"\nDone! Successfully generated '{zip_filename}' ({os.path.getsize(zip_filename)} bytes).")

if __name__ == "__main__":
    build_zip()
