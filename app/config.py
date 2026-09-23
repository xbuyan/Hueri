from typing import List, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Application ---
    PROJECT_NAME: str = "TenderScout AI"
    ENVIRONMENT: str = "development"  # "development" | "production"
    DEBUG: bool = False

    # --- Security ---
    SECRET_KEY: str = "super-secret-key"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480  # 8 hours
    # Comma-separated list; extend via env (e.g. https://tenders.hueri.co.ke)
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000"

    # --- Database ---
    DATABASE_URL: str = "sqlite+aiosqlite:///./tenderscout.db"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_PRE_PING: bool = True

    # --- Collectors ---
    COLLECTOR_MAX_RETRIES: int = 3

    # --- AI agent ---
    GEMINI_API_KEY: Optional[str] = ""

    # --- Alerts: email (SMTP) ---
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 587
    SMTP_USERNAME: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_FROM_EMAIL: Optional[str] = None
    SMTP_USE_TLS: bool = True
    # Comma-separated list of management recipients for alerts
    ALERT_EMAIL_RECIPIENTS: Optional[str] = None

    # --- Alerts: SMS (generic HTTP gateway) ---
    # Works with any gateway that accepts a JSON POST. Configure the payload
    # template with {to} and {message} placeholders, e.g. for Africa's Talking:
    #   SMS_API_URL=https://api.africastalking.com/version1/messaging
    #   SMS_PAYLOAD_TEMPLATE={"to":["{to}"],"message":"{message}"}}
    #   SMS_HEADERS={"apiKey":"...","Content-Type":"application/json","Accept":"application/json"}
    SMS_API_URL: Optional[str] = None
    SMS_PAYLOAD_TEMPLATE: Optional[str] = None
    SMS_HEADERS: Optional[str] = None
    SMS_SENDER_ID: Optional[str] = None
    # Comma-separated list of management phone numbers (E.164, e.g. +2547XXXXXXXX)
    ALERT_SMS_RECIPIENTS: Optional[str] = None

    # --- Alerts: behaviour ---
    # Minimum relevance score (0-10) that triggers a management notification
    ALERT_MIN_SCORE: float = 7.0
    # Send alerts inline with the collection pipeline (False = log a TODO for the worker queue)
    ALERTS_ENABLED: bool = True

    # --- Redis (caching + background jobs) ---
    # Empty string disables caching AND queueing (dev/CI run the pipeline
    # inline instead of via the arq worker).
    REDIS_URL: Optional[str] = None
    ANALYTICS_CACHE_TTL_SECONDS: int = 60

    # Cron: minutes past the hour for automatic collection runs (UTC)
    COLLECT_CRON_MINUTES: str = "0,30"

    # --- Rate limiting ---
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_AUTH: str = "10/minute"   # per-IP on login/register
    RATE_LIMIT_EXPENSIVE: str = "2/minute"  # per-user on trigger-collect

    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    def alert_email_recipients_list(self) -> List[str]:
        return [e.strip() for e in (self.ALERT_EMAIL_RECIPIENTS or "").split(",") if e.strip()]

    def alert_sms_recipients_list(self) -> List[str]:
        return [n.strip() for n in (self.ALERT_SMS_RECIPIENTS or "").split(",") if n.strip()]


settings = Settings()
