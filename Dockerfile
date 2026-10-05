# ---- Stage 1: build dependencies into an isolated venv ----
FROM python:3.14-slim AS builder

WORKDIR /app

# No compiler is installed: every pinned dependency publishes a prebuilt wheel for
# this Python, so the build stays fast. If a future pin needs to compile, pip will
# fail with a clear error; add build-essential here (apt-get update && apt-get
# install -y --no-install-recommends build-essential) in that case.

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
