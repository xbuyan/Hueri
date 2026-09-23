"""Tests for the management alert feature (email/SMS notifier + pipeline wiring)."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.notifier import notifier


def make_tender(score=7.5, external_id="TEST-1", title="ESIA for the Test Corridor"):
    return SimpleNamespace(
        external_id=external_id,
        source="TEST",
        title=title,
        buyer="Test Agency",
        url="https://example.com/tenders/test-1",
        deadline_str="2026-12-31",
        analysis=SimpleNamespace(
            relevance_score=score,
            executive_summary="Strong ESIA consultancy fit.",
            eligibility_gaps=["NEMA lead firm licence"],
        ),
    )


@pytest.fixture(autouse=True)
def clean_notifier():
    notifier.reset_dedupe()
    yield
    notifier.reset_dedupe()


# --- Rendering ---


def test_email_digest_renders_tender_details():
    subject, body = notifier._render_digest([make_tender()])

    assert "1 high-fit tender" in subject
    assert "ESIA for the Test Corridor" in body
    assert "7.5/10" in body
    assert "NEMA lead firm licence" in body
    assert "https://example.com/tenders/test-1" in body


def test_sms_text_truncates_long_titles():
    long_tender = make_tender(title="X" * 200)
    text = notifier._render_sms_text([long_tender])

    assert "..." in text
    assert len(text) < 400


# --- Dev fallback behaviour (no SMTP / SMS configured in tests) ---


def test_email_falls_back_to_log_in_dev(caplog):
    with caplog.at_level(logging.INFO, logger="services.notifier"):
        summary = asyncio.run(
            notifier.send_tender_alert([make_tender()], email_recipients=["mgr@hueri.co.ke"])
        )

    assert summary["emails_logged"] == 1
    assert summary["emails_sent"] == 0
    assert "[alert:email dev-fallback]" in caplog.text


def test_sms_falls_back_to_log_in_dev(caplog):
    with caplog.at_level(logging.INFO, logger="services.notifier"):
        summary = asyncio.run(
            notifier.send_tender_alert([make_tender()], sms_recipients=["+254700000001"])
        )

    assert summary["sms_logged"] == 1
    assert summary["sms_sent"] == 0
    assert "[alert:sms dev-fallback]" in caplog.text


def test_no_recipients_means_no_delivery():
    summary = asyncio.run(
        notifier.send_tender_alert([make_tender()], email_recipients=[], sms_recipients=[])
    )
    assert summary["emails_logged"] == 0
    assert summary["sms_logged"] == 0


def test_dedupe_skips_repeated_alerts():
    first = asyncio.run(
        notifier.send_tender_alert([make_tender()], email_recipients=["mgr@hueri.co.ke"])
    )
    second = asyncio.run(
        notifier.send_tender_alert([make_tender()], email_recipients=["mgr@hueri.co.ke"])
    )

    assert first["emails_logged"] == 1
    assert second["skipped_duplicates"] == 1
    assert second["emails_logged"] == 0


def test_invalid_sms_template_is_reported_not_raised():
    import app.services.notifier as notifier_module

    original_env = settings.ENVIRONMENT
    original_url = settings.SMS_API_URL
    original_tpl = settings.SMS_PAYLOAD_TEMPLATE
    try:
        # Force the "production" code path with a broken template
        settings.ENVIRONMENT = "production"
        settings.SMS_API_URL = "https://sms.example.com/send"
        settings.SMS_PAYLOAD_TEMPLATE = '{"to": "{to}" "message": "{message}"}'  # invalid JSON

        with pytest.raises(Exception):
            asyncio.run(notifier_module.notifier._send_sms("+254700000001", "test message"))
    finally:
        settings.ENVIRONMENT = original_env
        settings.SMS_API_URL = original_url
        settings.SMS_PAYLOAD_TEMPLATE = original_tpl


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient in _send_sms tests."""

    last_payload = None

    def __init__(self, response, **kwargs):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeAsyncClient.last_payload = json
        return self._response


def _patch_httpx(monkeypatch, response):
    import app.services.notifier as notifier_module

    monkeypatch.setattr(
        notifier_module.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(response)
    )
    return notifier_module


def test_sms_gateway_at_http_201_with_failed_recipient_raises(monkeypatch):
    """Africa's Talking returns HTTP 201 even when the number is rejected —
    the per-recipient status in the body must be honoured."""
    import app.services.notifier as notifier_module

    _patch_httpx(
        monkeypatch,
        _FakeResponse(
            201,
            {"SMSMessageData": {"Recipients": [
                {"status": "InvalidPhoneNumber", "phoneNumber": "+254700000001"}
            ]}},
        ),
    )

    original_env = settings.ENVIRONMENT
    original_url = settings.SMS_API_URL
    original_tpl = settings.SMS_PAYLOAD_TEMPLATE
    try:
        settings.ENVIRONMENT = "production"
        settings.SMS_API_URL = "https://api.sandbox.africastalking.com/version1/messaging"
        settings.SMS_PAYLOAD_TEMPLATE = (
            '{"username":"sandbox","to":["{to}"],"message":"{message}"}'
        )
        with pytest.raises(RuntimeError, match="InvalidPhoneNumber"):
            asyncio.run(
                notifier_module.notifier._send_sms("+254700000001", "test message")
            )
    finally:
        settings.ENVIRONMENT = original_env
        settings.SMS_API_URL = original_url
        settings.SMS_PAYLOAD_TEMPLATE = original_tpl


def test_sms_gateway_at_success_returns_body(monkeypatch):
    import app.services.notifier as notifier_module

    body = {"SMSMessageData": {"Recipients": [
        {"status": "Success", "phoneNumber": "+254700000001",
         "messageId": "ATUid_xxx", "cost": "KES 1.00"}
    ]}}
    _patch_httpx(monkeypatch, _FakeResponse(201, body))

    original_env = settings.ENVIRONMENT
    original_url = settings.SMS_API_URL
    original_tpl = settings.SMS_PAYLOAD_TEMPLATE
    try:
        settings.ENVIRONMENT = "production"
        settings.SMS_API_URL = "https://api.sandbox.africastalking.com/version1/messaging"
        settings.SMS_PAYLOAD_TEMPLATE = (
            '{"username":"sandbox","to":["{to}"],"message":"{message}"}'
        )
        result = asyncio.run(
            notifier_module.notifier._send_sms("+254700000001", "test message")
        )
        assert result == body
        # The AT payload template must produce username/to[]/message JSON
        assert _FakeAsyncClient.last_payload["to"] == ["+254700000001"]
        assert "test message" in _FakeAsyncClient.last_payload["message"]
    finally:
        settings.ENVIRONMENT = original_env
        settings.SMS_API_URL = original_url
        settings.SMS_PAYLOAD_TEMPLATE = original_tpl


# --- Pipeline integration (full offline stack, collectors + AI stubbed) ---


FAKE_NOTICE = {
    "external_id": "ALERT-1",
    "source": "TEST",
    "title": "ESIA for the Alert Corridor Project",
    "buyer": "Test Agency",
    "url": "https://example.com/tenders/alert-1",
    "deadline_str": "2026-12-31",
    "raw_summary": "Consultancy services for an environmental and social impact assessment.",
}


class FakeAIResult:
    relevance_score = 9.0
    is_fit = True
    executive_summary = "Very strong ESIA fit."
    matched_services = ["ESIA"]
    eligibility_gaps = []
    suggested_next_steps = ["Download tender documents"]


@pytest.fixture()
def alert_pipeline(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app import db as app_db
    import main as main_module

    # NullPool: same rationale as in conftest.py — never pool aiosqlite
    # connections across the TestClient portal loop and per-test loops.
    test_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", poolclass=NullPool
    )
    test_sessionmaker = async_sessionmaker(
        test_engine, expire_on_commit=False, class_=AsyncSession
    )
    monkeypatch.setattr(app_db, "engine", test_engine)
    monkeypatch.setattr(app_db, "async_session", test_sessionmaker)

    import app.services.pipeline as pipeline_module

    async def fake_ungm():
        return [dict(FAKE_NOTICE)]

    async def fake_empty():
        return []

    monkeypatch.setattr(pipeline_module.ungm_collector, "fetch_recent_notices", fake_ungm)
    monkeypatch.setattr(pipeline_module.ppip_collector, "fetch_recent_notices", fake_empty)
    monkeypatch.setattr(pipeline_module.worldbank_collector, "fetch_recent_notices_async", fake_empty)
    monkeypatch.setattr(pipeline_module.tender_agent, "analyze_tender", lambda *a, **k: FakeAIResult())

    # Configure alert recipients for the pipeline run
    monkeypatch.setattr(settings, "ALERT_EMAIL_RECIPIENTS", "mgr@hueri.co.ke")
    monkeypatch.setattr(settings, "ALERT_SMS_RECIPIENTS", "+254700000001")

    with TestClient(main_module.app) as c:
        yield c

    asyncio.run(test_engine.dispose())


def test_pipeline_persists_notification_rows(alert_pipeline, caplog):
    client = alert_pipeline

    res = client.post(
        "/api/auth/register",
        json={"email": "mgr@example.com", "password": "s3cret-pw"},
    )
    assert res.status_code == 200
    login = client.post(
        "/api/auth/login", data={"username": "mgr@example.com", "password": "s3cret-pw"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    with caplog.at_level(logging.INFO, logger="services.notifier"):
        collect = client.post("/api/tenders/trigger-collect", headers=headers)
    assert collect.status_code == 200, collect.text

    # The dev fallback must have fired for the 9.0-score tender
    assert "[alert:email dev-fallback]" in caplog.text
    assert "[alert:sms dev-fallback]" in caplog.text

    # ...and Notification audit rows (email + sms) must exist in the test database
    from sqlalchemy import select

    from app import db as app_db
    from app.models import Notification

    async def fetch_rows():
        async with app_db.async_session() as session:
            res = await session.execute(select(Notification))
            return res.scalars().all()

    rows = asyncio.run(fetch_rows())
    channels = {r.channel for r in rows}
    assert "email" in channels
    assert "sms" in channels
    assert all(r.status == "logged" for r in rows)
