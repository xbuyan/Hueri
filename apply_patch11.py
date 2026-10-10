#!/usr/bin/env python3
"""apply_patch11.py - patch 5 of 6: production deployment files.

Run from the ROOT of the integrate-live-collectors worktree
(~/projects/Hueri-full), with the earlier patches committed:

    python apply_patch11.py
    git status --short
    python -m pytest -q -p no:warnings 2>&1 | tail -5

Git is the safety net (no .bak files). To undo everything from this patch:

    git checkout -- .
    rm -f nginx.conf.template tests/test_deploy_files.py

What it does (the old files were read from your project, so every change is a
fix for a concrete problem):

  Dockerfile              Python 3.11 -> 3.14 (the version the 66 tests run on),
                          runs as an unprivileged user, health check uses the new
                          /api/health route.
  .dockerignore           created or extended so .env, .git, .venv, *.db, tests and
                          the frontend sources never end up inside the image. Without
                          it "COPY . ." would bake your secrets into the image.
  docker-compose.prod.yml rewritten:
                          - migrations run in the api container only; the worker waits
                            for the api to be healthy (both used to run them at once,
                            which can race on a fresh database)
                          - uvicorn trusts the nginx proxy headers, otherwise every user
                            shares one IP and the login rate limit (10/min) locks
                            everyone out together
                          - settings come from .env.production through env_file, so a
                            new setting never needs a compose edit
                          - profile.yaml is mounted from the host: edit it and restart,
                            no rebuild
                          - nginx gets the domain from DOMAIN
  nginx.conf              replaced by nginx.conf.template: the domain is no longer
                          hardcoded, so each client deployment just sets DOMAIN
  .env.example            rewritten to document every setting, with the deployment
                          variables first
  main.py                 adds GET /api/health (no login, no database)
  tests/test_deploy_files.py  6 tests that keep all of the above consistent

Not touched: DEPLOYMENT.md (rewritten in patch 6, together with the frontend
and login-protection work, so the guide describes the finished system).

Every change is validated before anything is written; any failure rolls
everything back. Safe to re-run: if nginx.conf.template exists the patch is
considered applied.
"""
import pathlib
import py_compile
import subprocess
import sys

ROOT = pathlib.Path.cwd()
EXPECTED_BRANCH = "integrate-live-collectors"

DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
COMPOSE_PROD = ROOT / "docker-compose.prod.yml"
NGINX_OLD = ROOT / "nginx.conf"
NGINX_NEW = ROOT / "nginx.conf.template"
ENV_EXAMPLE = ROOT / ".env.example"
MAIN = ROOT / "main.py"
TEST_DEPLOY = ROOT / "tests" / "test_deploy_files.py"

# ---------------------------------------------------------------------------
# New file contents
# ---------------------------------------------------------------------------

DOCKERFILE_SOURCE = r'''# ---- Stage 1: build dependencies into an isolated venv ----
FROM python:3.14-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ---- Stage 2: slim runtime ----
# No apt here: the health check uses the stdlib (python is already present),
# which keeps the image small and the build fast on slow links.
FROM python:3.14-slim

WORKDIR /app

# Nothing in the app needs root, so run unprivileged.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --chown=app:app . .

USER app

EXPOSE 8000

# /api/health needs no login and no database, so it stays valid whatever the
# auth rules are. The start period covers the database migration on first boot.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=5)" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
'''

DOCKERIGNORE_HEADER = "# Kept out of the Docker image (added by apply_patch11)"
DOCKERIGNORE_ENTRIES = [
    ".git",
    ".gitignore",
    ".env",
    ".env.*",
    "!.env.example",
    ".venv",
    "venv",
    "__pycache__",
    "*.py[cod]",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "node_modules",
    "frontend",
    "archive",
    "discovered",
    "tests",
    "*.bak*",
    ".hueri-*",
    "debug_*.html",
    "apply_patch*.py",
]

COMPOSE_SOURCE = r'''# Production stack for ONE client deployment: db, redis, api, worker, nginx.
#
#   alias dcp='docker compose -f docker-compose.prod.yml --env-file .env.production'
#   dcp up -d --build
#
# Settings come from .env.production (see .env.example). The client's business
# configuration is profile.yaml, mounted read-only: edit it, then `dcp restart api worker`.

x-app-env: &app-env
  ENVIRONMENT: production
  LOG_MODE: json
  DATABASE_URL: postgresql+asyncpg://${POSTGRES_USER:-postgres}:${POSTGRES_PASSWORD:?set in .env.production}@db:5432/${POSTGRES_DB:-tenderscout}
  REDIS_URL: redis://redis:6379/0
  PROFILE_PATH: /app/profile.yaml

services:
  db:
    image: postgres:16-alpine
    container_name: tenderscout_db_prod
    restart: always
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-postgres}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set in .env.production}
      POSTGRES_DB: ${POSTGRES_DB:-tenderscout}
    volumes:
      - postgres_prod_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-postgres}"]
      interval: 10s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    container_name: tenderscout_redis_prod
    restart: always
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5

  # The api container is the only place that runs database migrations.
  # --forwarded-allow-ips: the api is reachable only through nginx, so trust its
  # X-Forwarded-For header; otherwise every user shares one IP and the login
  # rate limit applies to everyone at once.
  api:
    build: .
    image: tenderscout-app:latest
    container_name: tenderscout_api_prod
    restart: always
    env_file: .env.production
    environment: *app-env
    command: sh -c "alembic upgrade head && uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4 --proxy-headers --forwarded-allow-ips '*'"
    volumes:
      - ./profile.yaml:/app/profile.yaml:ro
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy

  # Same image as the api. Waits for the api to be healthy (migrations done).
  worker:
    image: tenderscout-app:latest
    pull_policy: never
    container_name: tenderscout_worker_prod
    restart: always
    env_file: .env.production
    environment: *app-env
    command: arq app.worker.WorkerSettings
    volumes:
      - ./profile.yaml:/app/profile.yaml:ro
    depends_on:
      api:
        condition: service_healthy
      redis:
        condition: service_healthy

  nginx:
    image: nginx:alpine
    container_name: tenderscout_nginx
    restart: always
    ports:
      - "80:80"
      - "443:443"
    environment:
      DOMAIN: ${DOMAIN:?set DOMAIN in .env.production}
    volumes:
      - ./nginx.conf.template:/etc/nginx/templates/default.conf.template:ro
      - ./frontend/dist:/usr/share/nginx/html:ro
      - /etc/letsencrypt:/etc/letsencrypt:ro
    depends_on:
      - api

volumes:
  postgres_prod_data:
'''

NGINX_SOURCE = r'''# Rendered when the nginx container starts: ${DOMAIN} comes from the container
# environment (docker-compose.prod.yml). nginx's own variables ($host, $uri...)
# are left alone, so DOMAIN is the only placeholder in this file.
server {
    listen 80;
    server_name ${DOMAIN};
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    http2 on;
    server_name ${DOMAIN};

    ssl_certificate /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;

    client_max_body_size 1m;

    gzip on;
    gzip_types text/css application/javascript application/json image/svg+xml;

    # Security headers (nosniff, frame deny, referrer policy, HSTS) are added by the
    # api itself; adding them here too would send each one twice.

    location /api/ {
        proxy_pass http://api:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }

    location / {
        root /usr/share/nginx/html;
        index index.html;
        try_files $uri $uri/ /index.html;
    }
}
'''

ENV_EXAMPLE_SOURCE = r'''# TenderScout settings for ONE deployment.
#
# On the server: copy this file to .env.production, fill in the values, then
#   chmod 600 .env.production
# Never commit the real file. Business settings (company, services, keywords,
# countries, alert threshold) live in profile.yaml, not here.
# Avoid the "$" character in secrets: docker compose treats it specially.

# ---- Deployment (docker-compose.prod.yml and nginx) --------------------------
# The public host name of this deployment, without https://
DOMAIN=tenderscout.example.com
POSTGRES_USER=tenderscout
POSTGRES_PASSWORD=change-me-to-a-long-random-value
POSTGRES_DB=tenderscout
# DATABASE_URL, REDIS_URL, ENVIRONMENT and PROFILE_PATH are set by the compose file.

# ---- Security -----------------------------------------------------------------
# Generate with: python3 -c "import secrets; print(secrets.token_urlsafe(48))"
# In production the app refuses to start with the default key or one shorter
# than 32 characters (so this placeholder must be replaced).
SECRET_KEY=change-me
ENVIRONMENT=production
# Public sign-up stays closed; create accounts with scripts/create_user.py
ALLOW_REGISTRATION=false
CORS_ORIGINS=https://tenderscout.example.com
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=480
DEBUG=false
PROJECT_NAME=TenderScout AI

# ---- Business configuration ---------------------------------------------------
# Path to the client profile. The compose file sets this; for local runs the default
# is profile.yaml in the project root.
# PROFILE_PATH=profile.yaml
# Optional override of alerts.min_score from profile.yaml. Leave commented out to
# use the profile (an empty value would be rejected).
# ALERT_MIN_SCORE=6.5

# ---- AI scoring ---------------------------------------------------------------
GEMINI_API_KEY=

# ---- Scheduler (UTC) ----------------------------------------------------------
# Default: 05:00 and 13:00 UTC = 08:00 and 16:00 East Africa Time, twice a day,
# which keeps a free Gemini quota intact. Both are comma-separated lists.
COLLECT_CRON_HOURS=5,13
COLLECT_CRON_MINUTES=0
COLLECTOR_MAX_RETRIES=3
REQUEST_TIMEOUT_S=20

# ---- Alerts: email ------------------------------------------------------------
ALERTS_ENABLED=true
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_FROM_EMAIL=
SMTP_USE_TLS=true
# Comma-separated recipients
ALERT_EMAIL_RECIPIENTS=

# ---- Alerts: SMS (optional; any gateway that accepts a JSON POST) --------------
SMS_API_URL=
SMS_PAYLOAD_TEMPLATE=
SMS_HEADERS=
SMS_SENDER_ID=
# Comma-separated numbers in international format, e.g. +2547XXXXXXXX
ALERT_SMS_RECIPIENTS=

# ---- Performance and limits ---------------------------------------------------
ANALYTICS_CACHE_TTL_SECONDS=60
RATE_LIMIT_ENABLED=true
RATE_LIMIT_AUTH=10/minute
RATE_LIMIT_EXPENSIVE=2/minute
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=20
DB_POOL_PRE_PING=true
'''

TEST_DEPLOY_SOURCE = r'''"""Guards for the deployment files: the Docker, compose and nginx setup must stay
consistent with the app (one Python version, unprivileged container, migrations
in exactly one place, secrets kept out of the image, every setting documented)."""

import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_dockerfile_uses_one_python_version_and_runs_unprivileged():
    dockerfile = _read("Dockerfile")
    versions = set(re.findall(r"^FROM (python:\S+)", dockerfile, re.M))
    assert versions == {"python:3.14-slim"}
    assert re.search(r"^USER app$", dockerfile, re.M)
    assert "HEALTHCHECK" in dockerfile and "/api/health" in dockerfile


def test_compose_prod_structure():
    services = yaml.safe_load(_read("docker-compose.prod.yml"))["services"]
    assert set(services) == {"db", "redis", "api", "worker", "nginx"}
    # migrations run in exactly one place, and the worker waits for the api
    assert "alembic upgrade head" in services["api"]["command"]
    assert "alembic" not in services["worker"]["command"]
    assert services["worker"]["depends_on"]["api"]["condition"] == "service_healthy"
    # only nginx is reachable from outside
    for name in ("db", "redis", "api", "worker"):
        assert "ports" not in services[name], name
    assert "443:443" in services["nginx"]["ports"]
    # login rate limits need the real client IP, so uvicorn must trust the proxy
    assert "--forwarded-allow-ips" in services["api"]["command"]
    assert "DOMAIN" in services["nginx"]["environment"]


def test_nginx_template_only_needs_the_domain_variable():
    template = _read("nginx.conf.template")
    assert set(re.findall(r"\$\{(\w+)\}", template)) == {"DOMAIN"}
    assert "proxy_pass http://api:8000;" in template
    assert not (ROOT / "nginx.conf").exists()


def test_dockerignore_keeps_secrets_and_junk_out_of_the_image():
    entries = {line.strip() for line in _read(".dockerignore").splitlines()}
    assert {".env", ".git", ".venv", "frontend", "*.db"} <= entries


def test_env_example_documents_every_setting():
    from app.config import Settings

    text = _read(".env.example")
    missing = [name for name in Settings.model_fields if name not in text]
    assert not missing, f"add these to .env.example: {missing}"


def test_health_endpoint_needs_no_login(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
'''

MAIN_ANCHOR = "# --- AUTH ROUTES ---\n"
MAIN_NEW = '''# --- HEALTH ---


@app.get("/api/health")
async def health():
    """Liveness probe for Docker and uptime monitors: no login, no database."""
    return {"status": "ok"}


# --- AUTH ROUTES ---
'''

TRACKED_TO_CHECK = [
    "Dockerfile", "docker-compose.prod.yml", "nginx.conf", "main.py", ".env.example",
]


def fail(msg):
    print(f"ABORTED: {msg}\nNothing was changed.")
    sys.exit(1)


def git(*args, check=True):
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=check
        )
    except subprocess.CalledProcessError as exc:
        fail(f"git {' '.join(args)} failed: {exc.stderr.strip()}")


def read(path):
    if not path.exists():
        fail(f"{path.relative_to(ROOT)} not found - run this from ~/projects/Hueri-full")
    return path.read_text()


def preflight():
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != EXPECTED_BRANCH:
        fail(f"you are on branch '{branch}', expected '{EXPECTED_BRANCH}'")
    dirty = git("status", "--porcelain", "--", *TRACKED_TO_CHECK).stdout.strip()
    if dirty:
        fail("these files have uncommitted changes (commit the earlier patches first):\n" + dirty)
    if TEST_DEPLOY.exists():
        fail("tests/test_deploy_files.py already exists but nginx.conf.template does not; resolve by hand")


def plan_dockerignore():
    if DOCKERIGNORE.exists():
        existing = DOCKERIGNORE.read_text()
        present = {line.strip() for line in existing.splitlines()}
        missing = [entry for entry in DOCKERIGNORE_ENTRIES if entry not in present]
        if not missing:
            return existing
        sep = "" if existing.endswith("\n") or not existing else "\n"
        return existing + sep + "\n" + DOCKERIGNORE_HEADER + "\n" + "\n".join(missing) + "\n"
    return DOCKERIGNORE_HEADER + "\n" + "\n".join(DOCKERIGNORE_ENTRIES) + "\n"


def plan_env_example():
    """The hand-written file plus any Settings field it does not mention yet, so
    the new test passes even if the config has fields this patch did not know about."""
    text = ENV_EXAMPLE_SOURCE
    code = "from app.config import Settings\nprint('\\n'.join(Settings.model_fields))\n"
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        if "No module named" in proc.stderr:
            return text, "skipped the settings completeness check (project packages not importable here)"
        fail("could not read the Settings fields:\n" + proc.stderr.strip()[-1200:])
    names = [n for n in proc.stdout.split() if n]
    missing = [n for n in names if n not in text]
    if missing:
        text += "\n# ---- Other settings (see app/config.py) ----------------------------------------\n"
        text += "\n".join(f"# {name}=" for name in missing) + "\n"
    return text, f"all {len(names)} settings documented" + (f" ({len(missing)} appended)" if missing else "")


def plan_edits():
    old_docker = read(DOCKERFILE)
    if "python:3.11-slim" not in old_docker:
        fail("Dockerfile is not the version this patch was written for (no 'python:3.11-slim')")
    old_compose = read(COMPOSE_PROD)
    if "tenderscout_db_prod" not in old_compose or "arq app.worker.WorkerSettings" not in old_compose:
        fail("docker-compose.prod.yml is not the version this patch was written for")
    old_nginx = read(NGINX_OLD)
    if "proxy_pass http://api:8000;" not in old_nginx:
        fail("nginx.conf is not the version this patch was written for")
    main = read(MAIN)
    if main.count(MAIN_ANCHOR) != 1:
        fail(f"main.py: expected exactly one '# --- AUTH ROUTES ---' marker, found {main.count(MAIN_ANCHOR)}")
    if "/api/health" in main:
        fail("main.py already has a /api/health route; resolve by hand")

    env_text, env_note = plan_env_example()
    planned = {
        DOCKERFILE: DOCKERFILE_SOURCE,
        DOCKERIGNORE: plan_dockerignore(),
        COMPOSE_PROD: COMPOSE_SOURCE,
        NGINX_NEW: NGINX_SOURCE,
        NGINX_OLD: None,                      # deleted: replaced by the template
        ENV_EXAMPLE: env_text,
        MAIN: main.replace(MAIN_ANCHOR, MAIN_NEW),
        TEST_DEPLOY: TEST_DEPLOY_SOURCE,
    }
    return planned, env_note


def smoke_check():
    proc = subprocess.run(
        [sys.executable, "-c", "import main; print('main imports')"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return True, proc.stdout.strip()
    if "No module named" in proc.stderr:
        return True, "skipped (project packages are not importable from this interpreter)"
    return False, proc.stderr.strip()[-1500:]


def main():
    if NGINX_NEW.exists():
        print("nginx.conf.template already exists - patch 5 appears to be applied. Nothing to do.")
        return
    preflight()
    planned, env_note = plan_edits()

    originals = {}   # path -> previous text (None = did not exist)
    try:
        for path, text in planned.items():
            originals[path] = path.read_text() if path.exists() else None
            if text is None:
                path.unlink()
                print(f"removed {path.relative_to(ROOT)}")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            print(f"wrote {path.relative_to(ROOT)}")
        for path, text in planned.items():
            if text is not None and path.suffix == ".py":
                py_compile.compile(str(path), doraise=True)
        ok, message = smoke_check()
        if not ok:
            raise RuntimeError("the app failed to import after the patch:\n" + message)
        print(f"smoke check: {message}")
    except Exception as exc:  # roll back on any failure
        print(f"ERROR: {exc}\nRolling back...")
        for path, old in originals.items():
            if old is None:
                if path.exists():
                    path.unlink()
            else:
                path.write_text(old)
        print("Rolled back. Nothing was changed.")
        sys.exit(1)

    print(f".env.example: {env_note}")
    print("\nDone. Next:")
    print("  git status --short")
    print("  python -m pytest -q -p no:warnings 2>&1 | tail -5     # expect all green (72 tests)")
    print("  note: DEPLOYMENT.md still describes the old files; patch 6 rewrites it")


if __name__ == "__main__":
    main()
