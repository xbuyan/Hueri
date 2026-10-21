#!/usr/bin/env python3
"""apply_patch5.py - add email alerts to the HUERI tender scout.

Run from the project root (~/projects/Hueri):

    python apply_patch5.py

What it does:
  1. app/config.py            -> adds SMTP_* / ALERT_* settings (after ALLOW_MOCK_FALLBACK)
  2. app/services/alerts.py   -> new file with the alert logic
  3. main.py                  -> adds the import and POST /api/alerts/send
  4. .env                     -> appends empty SMTP/ALERT placeholders (if not present)

Safe to re-run: steps already applied are skipped. All edits are validated
before anything is written, timestamped .bak files are made, and every
changed .py file is compile-checked afterwards (backups are restored on failure).
"""
import datetime
import pathlib
import py_compile
import re
import shutil
import sys

ROOT = pathlib.Path.cwd()
CONFIG = ROOT / "app" / "config.py"
MAIN = ROOT / "main.py"
ALERTS = ROOT / "app" / "services" / "alerts.py"
ENV = ROOT / ".env"
STAMP = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

ALERTS_SOURCE = r'''import asyncio
import datetime
import html
import logging
import smtplib
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Tender, TenderAnalysis, CompanyProfile

logger = logging.getLogger("alerts")
DEFAULT_THRESHOLD = 6.5


class AlertError(Exception):
    pass


async def _profile_settings(db: AsyncSession):
    """Return (threshold, email_enabled). Falls back to defaults if no profile row exists."""
    res = await db.execute(select(CompanyProfile).order_by(CompanyProfile.id).limit(1))
    profile = res.scalar_one_or_none()
    if profile is None:
        return DEFAULT_THRESHOLD, True
    threshold = profile.minimum_score_threshold
    if threshold is None:
        threshold = DEFAULT_THRESHOLD
    return threshold, bool(profile.email_notifications)


async def _find_pending(db: AsyncSession, threshold: float):
    stmt = (
        select(Tender)
        .join(TenderAnalysis, TenderAnalysis.tender_id == Tender.id)
        .options(selectinload(Tender.analysis))
        .where(
            Tender.alerted_at.is_(None),
            Tender.is_unreviewed.is_(False),
            Tender.is_mock.is_(False),
            TenderAnalysis.relevance_score >= threshold,
        )
        .order_by(TenderAnalysis.relevance_score.desc())
    )
    return (await db.execute(stmt)).scalars().all()


def _build_message(tenders) -> EmailMessage:
    top = tenders[0].analysis.relevance_score
    msg = EmailMessage()
    msg["Subject"] = f"HUERI tender scout: {len(tenders)} new match(es), top score {top:.1f}"
    msg["From"] = settings.ALERT_FROM
    msg["To"] = settings.ALERT_TO

    text_parts, html_parts = [], []
    for t in tenders:
        a = t.analysis
        services = ", ".join(a.matched_services or [])
        text_parts.append(
            f"[{a.relevance_score:.1f}] {t.title}\n"
            f"{t.source} | {t.buyer or 'n/a'} | Deadline: {t.deadline_str or 'n/a'}\n"
            f"{a.executive_summary}\n"
            f"Matched services: {services or 'n/a'}\n{t.url}\n"
        )
        html_parts.append(
            f"<h3>[{a.relevance_score:.1f}] {html.escape(t.title)}</h3>"
            f"<p><b>{html.escape(t.source)}</b> | {html.escape(t.buyer or 'n/a')} | "
            f"Deadline: {html.escape(t.deadline_str or 'n/a')}</p>"
            f"<p>{html.escape(a.executive_summary)}</p>"
            f"<p>Matched services: {html.escape(services or 'n/a')}</p>"
            f'<p><a href="{html.escape(t.url, quote=True)}">Open tender</a></p><hr>'
        )
    msg.set_content("\n".join(text_parts))
    msg.add_alternative("".join(html_parts), subtype="html")
    return msg


def _send_email(msg: EmailMessage) -> None:
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as s:
        s.starttls()
        if settings.SMTP_USER:
            s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        s.send_message(msg)


async def send_pending_alerts(db: AsyncSession, dry_run: bool = False) -> dict:
    threshold, email_on = await _profile_settings(db)
    if not email_on:
        return {"sent": 0, "reason": "email_notifications is off"}

    tenders = await _find_pending(db, threshold)
    if not tenders:
        return {"sent": 0, "threshold": threshold, "reason": "nothing pending"}
    if dry_run:
        return {
            "sent": 0,
            "would_send": len(tenders),
            "threshold": threshold,
            "titles": [t.title for t in tenders],
        }

    if not (settings.SMTP_HOST and settings.ALERT_FROM and settings.ALERT_TO):
        raise AlertError("SMTP_HOST, ALERT_FROM and ALERT_TO must be set")

    msg = _build_message(tenders)
    try:
        await asyncio.to_thread(_send_email, msg)
    except (smtplib.SMTPException, OSError) as exc:
        # Nothing is marked as alerted, so the next run retries these tenders.
        raise AlertError(f"Email send failed: {exc}")

    now = datetime.datetime.utcnow()
    for t in tenders:
        t.alerted_at = now
    await db.commit()
    return {"sent": len(tenders), "threshold": threshold}
'''

CONFIG_FIELDS = [
    'SMTP_HOST: str = ""',
    "SMTP_PORT: int = 587",
    'SMTP_USER: str = ""',
    'SMTP_PASSWORD: str = ""',
    'ALERT_FROM: str = ""',
    'ALERT_TO: str = ""  # comma-separated recipients',
]

MAIN_IMPORT_ANCHOR = "from app.collectors.base import CollectorError"
MAIN_IMPORT_NEW = "from app.services.alerts import send_pending_alerts, AlertError"

MAIN_ROUTE_ANCHOR = '@app.get("/api/tenders", response_model=List[TenderOut])'
MAIN_ROUTE_NEW = '''@app.post("/api/alerts/send")
async def send_alerts(dry_run: bool = False, db: AsyncSession = Depends(get_db)):
    try:
        return await send_pending_alerts(db, dry_run=dry_run)
    except AlertError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

'''

ENV_BLOCK = """
# --- email alerts (apply_patch5) ---
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
ALERT_FROM=
ALERT_TO=
"""


def fail(msg):
    print(f"ABORTED: {msg}\nNothing was changed.")
    sys.exit(1)


def plan_config():
    if not CONFIG.exists():
        fail("app/config.py not found - run this from ~/projects/Hueri")
    text = CONFIG.read_text()
    if "SMTP_HOST" in text:
        print("config.py: already patched, skipping")
        return None
    matches = list(re.finditer(r"^([ \t]+)ALLOW_MOCK_FALLBACK\b.*$", text, re.M))
    if len(matches) != 1:
        fail(
            f"expected exactly one 'ALLOW_MOCK_FALLBACK' field line in app/config.py, "
            f"found {len(matches)}. Add the six SMTP/ALERT fields by hand."
        )
    m = matches[0]
    indent = m.group(1)
    block = "\n".join(indent + line for line in CONFIG_FIELDS)
    return text[: m.end()] + "\n" + block + text[m.end():]


def plan_main():
    if not MAIN.exists():
        fail("main.py not found - run this from ~/projects/Hueri")
    text = MAIN.read_text()
    if "send_pending_alerts" in text:
        print("main.py: already patched, skipping")
        return None
    if text.count(MAIN_IMPORT_ANCHOR) != 1:
        fail(f"import anchor not found exactly once in main.py: {MAIN_IMPORT_ANCHOR!r}")
    if text.count(MAIN_ROUTE_ANCHOR) != 1:
        fail(f"route anchor not found exactly once in main.py: {MAIN_ROUTE_ANCHOR!r}")
    text = text.replace(MAIN_IMPORT_ANCHOR, MAIN_IMPORT_ANCHOR + "\n" + MAIN_IMPORT_NEW)
    text = text.replace(MAIN_ROUTE_ANCHOR, MAIN_ROUTE_NEW + MAIN_ROUTE_ANCHOR)
    return text


def main():
    if not ALERTS.parent.is_dir():
        fail("app/services/ not found - run this from ~/projects/Hueri")

    new_config = plan_config()
    new_main = plan_main()
    alerts_exists = ALERTS.exists()
    alerts_same = alerts_exists and ALERTS.read_text() == ALERTS_SOURCE

    changed = []   # (path, backup or None)

    def backup(path):
        bak = path.with_name(path.name + f".bak-{STAMP}")
        shutil.copy2(path, bak)
        return bak

    try:
        if new_config is not None:
            changed.append((CONFIG, backup(CONFIG)))
            CONFIG.write_text(new_config)
            print("config.py: added SMTP/ALERT settings")

        if alerts_same:
            print("alerts.py: already up to date, skipping")
        else:
            changed.append((ALERTS, backup(ALERTS) if alerts_exists else None))
            ALERTS.write_text(ALERTS_SOURCE)
            print("alerts.py: written")

        if new_main is not None:
            changed.append((MAIN, backup(MAIN)))
            MAIN.write_text(new_main)
            print("main.py: added import and POST /api/alerts/send")

        for path, _ in changed:
            py_compile.compile(str(path), doraise=True)
    except Exception as exc:  # restore everything on any failure
        print(f"ERROR: {exc}\nRestoring backups...")
        for path, bak in changed:
            if bak is not None:
                shutil.copy2(bak, path)
            elif path.exists():
                path.unlink()
        print("Restored. Nothing was changed.")
        sys.exit(1)

    if ENV.exists():
        env_text = ENV.read_text()
        if "SMTP_HOST" in env_text:
            print(".env: SMTP settings already present, skipping")
        else:
            shutil.copy2(ENV, ENV.with_name(f".env.bak-{STAMP}"))
            with ENV.open("a") as f:
                if not env_text.endswith("\n"):
                    f.write("\n")
                f.write(ENV_BLOCK)
            print(".env: appended empty SMTP/ALERT placeholders (fill them in)")
    else:
        print(".env not found - add SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, ALERT_FROM, ALERT_TO yourself")

    print("\nDone. Next:")
    print("  1. Fill in the SMTP_* and ALERT_* values in .env")
    print("  2. Restart: uvicorn main:app --reload --port 8000")
    print('  3. Dry run: curl -X POST "localhost:8000/api/alerts/send?dry_run=true"')


if __name__ == "__main__":
    main()
