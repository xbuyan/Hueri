import os
from typing import Optional
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    PROJECT_NAME: str = "TenderScout AI"
    SECRET_KEY: str = "super-secret-key"
    
    # Explicit SQLite connection string (no external DB needed)
    DATABASE_URL: str = "sqlite+aiosqlite:///./tenderscout.db"
    
    # Optional API key for Gemini agent
    GEMINI_API_KEY: Optional[str] = ""

    class Config:
        extra = "ignore"

settings = Settings()
