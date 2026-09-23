# Hueri — Tender Intelligence Platform

Automated procurement tender scouting for **HUERI Limited** (Nairobi-based ESIA consultancy). The platform collects tender notices from UNGM, Kenya PPIP, and World Bank STEP, scores each one against HUERI's capability profile with an AI agent, and **alerts management by email and SMS** when a high-fit opportunity appears.

## Features

- **Multi-source collection** — UNGM, PPIP (Kenya), World Bank STEP scrapers/APIs with retry + backoff
- **AI evaluation** — Gemini 2.5 Flash scores every tender (service match 40%, geography 20%, regulatory 20%, scale 20%) and returns a structured analysis
- **Management alerts** — email (SMTP) + SMS (generic JSON gateway) notifications for tenders above the score threshold, with dedupe and a `notifications` audit trail
- **Queue + cron** — collection runs on an arq worker (Redis) with a built-in scheduler; inline fallback without Redis
- **JWT auth** — register/login, protected collection trigger, `/api/auth/me`
- **Dashboard** — React + Vite + Tailwind frontend
- **Ops-ready** — Alembic migrations, JSON structured logging, Redis caching, rate limiting, security headers, Docker/Compose, CI with pytest

## Architecture

```
frontend (React/Vite)  ──►  FastAPI (main.py)  ──►  PostgreSQL / SQLite
                                   │              ▲
                    ┌──────────────┼──────────────┤
                    ▼              ▼              ▼
               collectors/    services/       Redis cache
            UNGM, PPIP, WB   ai_agent.py     (analytics)
                             notifier.py
                             (email + SMS)
```

## Quickstart (local dev)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env          # then edit values
alembic upgrade head          # create schema
uvicorn main:app --reload     # http://localhost:8000/docs
```

Frontend:

```bash
cd frontend && npm install && npm run dev    # http://localhost:5173
```

## Quickstart (Docker)

```bash
cp .env.example .env          # dev defaults work out of the box
docker compose up --build     # api on :8000, postgres on :5432, redis on :6379
```

The api container runs `alembic upgrade head` before starting uvicorn.

### Bootstrap management account

Collection runs are authenticated, and the dashboard signs in automatically against a bootstrap management account. Create it once after starting the API:

```bash
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "management@hueri.co.ke", "password": "changeme", "full_name": "HUERI Management"}'
```

The dashboard's sign-in helper (`frontend/src/components/Dashboard.jsx`) defaults to those credentials — for anything beyond local testing, register a strong password and update the helper accordingly.

## Configuring alerts

Alerts fire during `POST /api/tenders/trigger-collect` for every tender whose AI relevance score is ≥ `ALERT_MIN_SCORE` (default 7.0). Each channel is independent and degrades to a log line when unconfigured (dev/CI never sends real messages).

### Email (any SMTP provider)

Works with Gmail Workspace, Zoho, SES (SMTP interface), Hostinger, etc.

```env
ENVIRONMENT=production
SMTP_HOST=smtp.zoho.com
SMTP_PORT=587
SMTP_USERNAME=tenders@hueri.co.ke
SMTP_PASSWORD=...
SMTP_FROM_EMAIL=tenders@hueri.co.ke
ALERT_EMAIL_RECIPIENTS=ceo@hueri.co.ke,ops@hueri.co.ke
```

### SMS (generic JSON gateway)

The gateway adapter is configuration-only — point it at any JSON API. Placeholders `{to}` and `{message}` are substituted into the payload template.

**Africa's Talking** (East Africa):

```env
ENVIRONMENT=production
SMS_API_URL=https://api.africastalking.com/version1/messaging
SMS_PAYLOAD_TEMPLATE={"username":"myuser","to":["{to}"],"message":"{message}"}
SMS_HEADERS={"apiKey":"AT-KEY","Content-Type":"application/json","Accept":"application/json"}
ALERT_SMS_RECIPIENTS=+2547XXXXXXXX,+2547YYYYYYYY
```

**Telnyx** (global):

```env
SMS_API_URL=https://api.telnyx.com/v2/messages
SMS_PAYLOAD_TEMPLATE={"to":"{to}","from":"+2547XXXXXXXX","text":"{message}"}
SMS_HEADERS={"Authorization":"Bearer TELNYX_KEY","Content-Type":"application/json"}
```

Every alert is recorded in the `notifications` table (`channel`, `recipient`, `status`, `created_at`) for auditing. Duplicate alerts for the same tender/recipients are skipped within a run.

## Background worker & scheduler

When `REDIS_URL` is set (compose does this), `POST /api/tenders/trigger-collect` **queues** the run on an [arq](https://arq-docs.helpmanual.io/) worker and returns `{"status": "queued"}` immediately — the API never blocks on collectors or Gemini. The dashboard polls `GET /api/tenders/collect-status` (idle/queued/running/complete/failed) until the run finishes.

Without Redis, the same endpoint runs the pipeline **inline** (dev/CI behaviour), so nothing breaks in minimal setups.

**The scheduler**: the worker also runs a cron job that executes the full pipeline every `COLLECT_CRON_MINUTES` (default `0,30` — twice an hour, UTC), so tenders and alerts flow without anyone pressing a button.

```bash
# run the worker locally (needs REDIS_URL)
arq app.worker.WorkerSettings
```

Compose starts the worker as its own service (`tenderscout_worker`), running `alembic upgrade head` first. Alerts fire from whichever process runs the pipeline — worker (queued/cron) or API (inline).

## Database migrations

```bash
alembic upgrade head                      # apply
alembic revision --autogenerate -m "..."  # after changing app/models.py
alembic downgrade -1                      # rollback last
```

`migrations/env.py` reads `DATABASE_URL`, so the same command works for SQLite (dev) and PostgreSQL (prod).

## Testing

```bash
pip install -r requirements-dev.txt
pytest                # 17 offline tests: auth, pipeline, alerts, analytics
```

Tests are fully offline: collectors and the AI agent are stubbed, each test gets a throwaway SQLite DB, rate limiting is disabled via `tests/conftest.py`.

## Production deployment

1. Provision a host with Docker + Compose.
2. Create `.env.production` (see `.env.example`); **`SECRET_KEY` and `POSTGRES_PASSWORD` are mandatory** and the app refuses to boot with the dev default `SECRET_KEY`.
3. `docker compose -f docker-compose.prod.yml up -d --build`
4. Terminate TLS at the nginx service (config in `nginx.conf`, certs under `/etc/letsencrypt`).

What production mode changes: JSON logs to stdout, HSTS header, real email/SMS delivery, 4 uvicorn workers, migrations on boot.

## Environment variables

See [`.env.example`](.env.example) for the full annotated list, including:
database URL + pool sizing, CORS origins, Gemini key, SMTP and SMS gateway settings, alert threshold, Redis URL, and rate-limit strings.

## Roadmap

- **Deadline reminders** — SMS/email nudges as tender deadlines approach
- **Company profile UI** — manage `HUERI_PROFILE` and alert thresholds from the dashboard
- **Per-user alert preferences** — notification settings beyond the global management recipients

## CI

GitHub Actions (`.github/workflows/ci.yml`) installs dependencies, verifies the app imports, and runs the pytest suite on every push and PR.
