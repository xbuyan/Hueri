"""Alert delivery: email via SMTP, SMS via a generic HTTP gateway.

Design notes:
- Email uses SMTP so any provider works (Gmail Workspace, Zoho, SES
  SMTP interface, Hostinger...) without vendor lock-in.
- SMS targets a generic JSON gateway configured entirely via settings
  (URL, payload template with {to}/{message} placeholders, headers), so
  gateways like Africa's Talking or Telnyx plug in without code changes.
- Both channels degrade gracefully: when unconfigured (or outside
  production) alerts are logged instead of sent, so local runs and CI
  never fail because of missing credentials.
- Dedupe: a notification for a given (recipient, tender set) is sent
  once per process run, in every mode — the dev log-fallback dedupes too.
- Inputs may be ORM objects or plain dicts (the pipeline passes dicts to
  avoid lazy-load issues outside the async session context).
"""

import json
import logging
import ssl
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiosmtplib
import httpx

from app.config import settings

logger = logging.getLogger("services.notifier")


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a dict or an object."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class Notifier:
    """Sends management alerts over email and SMS, with dev fallbacks."""

    def __init__(self) -> None:
        self._sent_keys: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send_tender_alert(
        self,
        tenders: Sequence[Any],
        email_recipients: Optional[Sequence[str]] = None,
        sms_recipients: Optional[Sequence[str]] = None,
    ) -> Dict[str, int]:
        """Notify management about high-fit tenders.

        Returns a summary dict: {"emails_sent", "emails_logged",
        "sms_sent", "sms_logged", "skipped_duplicates"}.
        """
        summary = {
            "emails_sent": 0,
            "emails_logged": 0,
            "sms_sent": 0,
            "sms_logged": 0,
            "skipped_duplicates": 0,
        }
        if not tenders:
            return summary

        emails = list(
            email_recipients
            if email_recipients is not None
            else settings.alert_email_recipients_list()
        )
        phones = list(
            sms_recipients
            if sms_recipients is not None
            else settings.alert_sms_recipients_list()
        )

        # ---- Email channel ----
        if emails:
            fresh = self._fresh_recipients("email", emails, tenders)
            summary["skipped_duplicates"] += len(emails) - len(fresh)
            if fresh:
                if settings.SMTP_HOST and settings.ENVIRONMENT == "production":
                    subject, body = self._render_digest(tenders)
                    for email, key in fresh:
                        self._sent_keys.add(key)
                        try:
                            await self._send_email(subject, body, [email])
                            summary["emails_sent"] += 1
                        except Exception:
                            logger.exception("Email alert to %s failed", email)
                else:
                    logger.info(
                        "[alert:email dev-fallback] to=%s subject=%r body=%s",
                        [email for email, _ in fresh],
                        *self._render_digest(tenders),
                    )
                    summary["emails_logged"] += 1
                    self._sent_keys.update(key for _, key in fresh)

        # ---- SMS channel ----
        if phones:
            fresh = self._fresh_recipients("sms", phones, tenders)
            summary["skipped_duplicates"] += len(phones) - len(fresh)
            if fresh:
                if settings.SMS_API_URL and settings.ENVIRONMENT == "production":
                    sms_body = self._render_sms_text(tenders)
                    for phone, key in fresh:
                        self._sent_keys.add(key)
                        try:
                            await self._send_sms(phone, sms_body)
                            summary["sms_sent"] += 1
                        except Exception:
                            logger.exception("SMS alert to %s failed", phone)
                else:
                    logger.info(
                        "[alert:sms dev-fallback] to=%s text=%s",
                        [phone for phone, _ in fresh],
                        self._render_sms_text(tenders),
                    )
                    summary["sms_logged"] += 1
                    self._sent_keys.update(key for _, key in fresh)

        return summary

    def reset_dedupe(self) -> None:
        """Clear in-run dedupe state (used by tests)."""
        self._sent_keys.clear()

    # ------------------------------------------------------------------
    # Dedupe helpers
    # ------------------------------------------------------------------

    def _fresh_recipients(
        self, channel: str, recipients: Sequence[str], tenders: Sequence[Any]
    ) -> List[Tuple[str, str]]:
        """Return (recipient, dedupe-key) pairs not yet notified this run."""
        fresh: List[Tuple[str, str]] = []
        for recipient in recipients:
            key = f"{channel}:{recipient}:{self._tender_keys(tenders)}"
            if key not in self._sent_keys:
                fresh.append((recipient, key))
        return fresh

    def _tender_keys(self, tenders: Sequence[Any]) -> str:
        return ",".join(
            sorted(str(_get(t, "external_id", id(t))) for t in tenders)
        )

    # ------------------------------------------------------------------
    # Message rendering
    # ------------------------------------------------------------------

    def _render_digest(self, tenders: Sequence[Any]) -> Tuple[str, str]:
        lines: List[str] = []
        for t in tenders:
            score = _get(_get(t, "analysis"), "relevance_score")
            score_str = f"{score:.1f}/10" if score is not None else "n/a"
            summary = (_get(_get(t, "analysis"), "executive_summary") or "").strip()
            gaps = _get(_get(t, "analysis"), "eligibility_gaps") or []

            lines.append(
                f"* {_get(t, 'title')}\n"
                f"  Source: {_get(t, 'source')} | Buyer: {_get(t, 'buyer') or 'n/a'}\n"
                f"  Match score: {score_str}\n"
                f"  Deadline: {_get(t, 'deadline_str') or 'n/a'}\n"
                f"  Link: {_get(t, 'url')}"
            )
            if summary:
                lines.append(f"  Summary: {summary}")
            if gaps:
                lines.append(f"  Gaps: {', '.join(gaps)}")
            lines.append("")

        subject = (
            f"[{settings.PROJECT_NAME}] {len(tenders)} high-fit "
            f"tender{'s' if len(tenders) != 1 else ''} for HUERI"
        )
        body = (
            "The following tenders scored above the alert threshold "
            f"({settings.ALERT_MIN_SCORE:.1f}/10):\n\n" + "\n".join(lines).rstrip()
        )
        return subject, body

    def _render_sms_text(self, tenders: Sequence[Any]) -> str:
        parts: List[str] = []
        for t in tenders:
            score = _get(_get(t, "analysis"), "relevance_score")
            score_str = f"{score:.1f}" if score is not None else "?"
            title = _get(t, "title") or ""
            title = title if len(title) <= 60 else title[:57] + "..."
            parts.append(f"[{score_str}] {title} ({_get(t, 'source')}) - {_get(t, 'url')}")
        return "HUERI high-fit tenders: " + " | ".join(parts)

    # ------------------------------------------------------------------
    # Delivery backends
    # ------------------------------------------------------------------

    async def _send_email(self, subject: str, body: str, to: Sequence[str]) -> None:
        msg = EmailMessage()
        msg["From"] = settings.SMTP_FROM_EMAIL or settings.SMTP_USERNAME or ""
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg.set_content(body)

        tls_context = ssl.create_default_context()
        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USERNAME,
            password=settings.SMTP_PASSWORD,
            start_tls=settings.SMTP_USE_TLS,
            tls_context=tls_context if settings.SMTP_USE_TLS else None,
            timeout=30,
        )
        logger.info("Email alert sent to %s", to)

    async def _send_sms(self, to: str, text: str) -> None:
        template = settings.SMS_PAYLOAD_TEMPLATE or '{"to": "{to}", "message": "{message}"}'
        payload_str = template.replace("{to}", to).replace("{message}", text)
        try:
            payload: Dict[str, Any] = json.loads(payload_str)
        except json.JSONDecodeError:
            logger.error(
                "SMS_PAYLOAD_TEMPLATE produced invalid JSON for %s; check quoting", to
            )
            raise

        headers = {"Content-Type": "application/json"}
        if settings.SMS_HEADERS:
            try:
                headers.update(json.loads(settings.SMS_HEADERS))
            except json.JSONDecodeError:
                logger.error("SMS_HEADERS is not valid JSON; using default headers")

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(settings.SMS_API_URL, json=payload, headers=headers)
            resp.raise_for_status()
        logger.info("SMS alert sent to %s", to)


notifier = Notifier()
