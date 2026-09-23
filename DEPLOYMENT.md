# Production Deployment Guide

Step-by-step deployment of TenderScout AI (Hueri) to a single production host
with real TLS certificates (Let's Encrypt), PostgreSQL, Redis, the arq worker,
and nginx.

Reference stack: Ubuntu 22.04/24.04, Docker Engine 24+, Docker Compose v2,
domain `tenderscout.hueri.co.ke` (replace with your own domain everywhere).

---

## 1. Provision the host

```bash
# Update and install Docker (official convenience script) + compose plugin
sudo apt update && sudo apt -y upgrade
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # log out/in afterwards

# Firewall: SSH, HTTP (ACME challenges), HTTPS
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

Create a DNS **A record** pointing `tenderscout.hueri.co.ke` at the host's
public IP and verify it resolves before continuing — Let's Encrypt will not
issue a certificate until it does.

## 2. Get the code and build the frontend

```bash
sudo mkdir -p /srv/hueri && sudo chown "$USER" /srv/hueri
cd /srv/hueri
git clone https://github.com/xbuyan/Hueri.git app && cd app

# nginx serves the built SPA from ./frontend/dist (bind-mounted into the
# nginx container), so the bundle must be built on the host:
cd frontend
npm ci && npm run build
cd ..
```

## 3. Issue the TLS certificate (before first start)

The prod nginx container binds ports 80/443, so for the **first** issuance run
certbot standalone while nothing is listening:

```bash
sudo apt install -y certbot
sudo certbot certonly --standalone \
  -d tenderscout.hueri.co.ke \
  --agree-tos -m admin@hueri.co.ke --no-eff-email
```

Certificates land in `/etc/letsencrypt/live/tenderscout.hueri.co.ke/` —
exactly the paths `nginx.conf` already mounts (`/etc/letsencrypt:ro`).

> **Using a different domain?** Replace the domain in both `nginx.conf`
> (`server_name`, `ssl_certificate*` paths) and this guide.

## 4. Configure secrets

```bash
# Generate strong values first:
python3 -c "import secrets; print('SECRET_KEY =', secrets.token_urlsafe(48))"
python3 -c "import secrets; print('POSTGRES_PASSWORD =', secrets.token_urlsafe(24))"
```

Create `.env.production` next to `docker-compose.prod.yml` (never commit it):

```ini
SECRET_KEY=<generated value>            # required — compose refuses to start without it
POSTGRES_PASSWORD=<generated value>     # required
POSTGRES_USER=hueri
POSTGRES_DB=tenderscout

CORS_ORIGINS=https://tenderscout.hueri.co.ke
GEMINI_API_KEY=<your Google AI Studio key>

# --- Alerts: email (SMTP) ---
SMTP_HOST=smtp.zoho.com                 # or Gmail Workspace / SES / Hostinger
SMTP_PORT=587
SMTP_USERNAME=tenders@hueri.co.ke
SMTP_PASSWORD=<mailbox password>
SMTP_FROM_EMAIL=tenders@hueri.co.ke
ALERT_EMAIL_RECIPIENTS=ceo@hueri.co.ke,ops@hueri.co.ke

# --- Alerts: SMS (generic JSON gateway, e.g. Africa's Talking) ---
SMS_API_URL=https://api.africastalking.com/version1/messaging
SMS_PAYLOAD_TEMPLATE={"username":"myuser","to":["{to}"],"message":"{message}"}
SMS_HEADERS={"apiKey":"AT-KEY","Content-Type":"application/json"}
ALERT_SMS_RECIPIENTS=+2547XXXXXXXX,+2547YYYYYYYY

ALERT_MIN_SCORE=7.0
COLLECT_CRON_MINUTES=0,30               # automatic runs every 30 min (UTC)
```

Lock it down: `chmod 600 .env.production`.

## 5. Start the stack

> **Note:** compose interpolates the whole file for *every* command — so
> `build`, `ps`, `logs` etc. also fail with "required variable SECRET_KEY"
> until `.env.production` exists. Define a shell alias once and use it
> everywhere below:
>
> ```bash
> alias dcp='docker compose -f docker-compose.prod.yml --env-file .env.production'
> ```

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build
docker compose -f docker-compose.prod.yml ps   # all five services healthy
```

What each service does on boot:

| Service | Behaviour |
|---|---|
| `db` | PostgreSQL 16 with a named volume (`postgres_prod_data`) |
| `redis` | Redis 7 — job queue + analytics cache |
| `api` | runs `alembic upgrade head` (migrations), then uvicorn ×4 workers |
| `worker` | runs migrations, then arq worker with the collection cron (`COLLECT_CRON_MINUTES`) |
| `nginx` | TLS termination, serves `frontend/dist`, proxies `/api/` to the api service |

## 6. Create the management account & verify

```bash
curl -s -X POST https://tenderscout.hueri.co.ke/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"you@hueri.co.ke","password":"<strong password>","full_name":"HUERI Management"}'
```

Then:

1. Open `https://tenderscout.hueri.co.ke` — the dashboard loads over TLS.
2. Sign in via the header button (token is kept in localStorage).
3. Change the initial password immediately: **Account → Change Password**.
4. Trigger a run with **Sync Sources** (queued through the worker), or wait
   for the cron. High-fit tenders fire email/SMS alerts to management.
5. Smoke checks:

```bash
curl -sf https://tenderscout.hueri.co.ke/api/analytics/summary
docker compose -f docker-compose.prod.yml logs -f api worker   # JSON logs
```

## 7. TLS renewal (automated)

Certbot's systemd timer renews twice daily. The renewal must free port 80/443,
so attach hooks that bounce the nginx container:

```bash
sudo certbot renew --dry-run   # validates the whole flow now
```

Create `/etc/letsencrypt/renewal-hooks-deploy/reload-nginx.sh`:

```bash
#!/bin/sh
cd /srv/hueri/app
docker compose -f docker-compose.prod.yml exec -T nginx nginx -s reload
```

```bash
sudo chmod +x /etc/letsencrypt/renewal-hooks-deploy/reload-nginx.sh
```

`certbot renew` keeps the standalone listener only when it actually renews;
the deploy hook reloads nginx so the container picks up new certs with zero
downtime.

### Optional hardening

Add to the `listen 443` server block in `nginx.conf`:

```nginx
ssl_protocols TLSv1.2 TLSv1.3;
ssl_prefer_server_ciphers off;
ssl_stapling on;
ssl_stapling_verify on;
```

Then `docker compose -f docker-compose.prod.yml restart nginx`.

## 8. Operations

**Deploy an update**

```bash
cd /srv/hueri/app
git pull
cd frontend && npm ci && npm run build && cd ..
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build
# schema changes apply automatically: api and worker run `alembic upgrade head`
```

**Database backup (nightly cron)**

```bash
# /etc/cron.d/hueri-backup
0 2 * * * root docker compose -f /srv/hueri/app/docker-compose.prod.yml exec -T db \
  pg_dump -U hueri tenderscout | gzip > /srv/backups/tenderscout-$(date +\%F).sql.gz
```

**Restore**: `gunzip -c <file>.sql.gz | docker compose exec -T db psql -U hueri tenderscout`

**Logs / job queue**

```bash
docker compose -f docker-compose.prod.yml logs -f worker   # cron runs + alerts
docker compose -f docker-compose.prod.yml exec redis redis-cli
```

**Troubleshooting**

| Symptom | Fix |
|---|---|
| `docker build` crawls at `Get:1 http://deb.debian.org ...` | Network/proxy throttling Debian mirrors — not a stack problem. Retry (layer cache resumes), or point apt at a local/regional mirror in the Dockerfile |
| `required variable SECRET_KEY is missing a value` on *any* compose command | Create `.env.production` first (section 4) and pass `--env-file` (see the alias note in section 5) |
| `nginx` fails to start: no cert files | Section 3 not done, or domain mismatch between cert path and `nginx.conf` |
| Alerts not arriving | Check `SMTP_*`/`SMS_*` in `.env.production`; worker logs show `logged` (fallback) vs `sent`; empty config = log-only by design |
| `429 Too Many Requests` | slowapi limits: 10/min auth, 2/min collection — expected |
| Collection stuck queued | `redis-cli` → `KEYS collection:*`; worker logs show `run_collection_job` |
| Frontend shows stale bundle | `frontend/dist` not rebuilt before `up --build` (section 8, step 1) |

## 9. Security checklist before go-live

- [ ] `SECRET_KEY` and `POSTGRES_PASSWORD` randomly generated, `.env.production` chmod 600
- [ ] Initial management password rotated via **Account → Change Password**
- [ ] `CORS_ORIGINS` limited to the real domain (no localhost)
- [ ] TLS: `curl -I http://tenderscout.hueri.co.ke` returns `301` to https
- [ ] Firewall: only 22/80/443 open; PostgreSQL/Redis are compose-internal only
- [ ] Backups cron in place and restore tested once
