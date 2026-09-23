"""Offline smoke tests for the TenderScout API.

No network access happens here: collectors and the Gemini agent are
stubbed, and every test runs against a throwaway SQLite database.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import db as app_db
from app.db import get_db
from main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by a per-test SQLite database.

    Patches app.db.engine/async_session so both the startup hook
    (init_db) and the get_db dependency hit the test database.
    """
    test_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    test_sessionmaker = async_sessionmaker(
        test_engine, expire_on_commit=False, class_=AsyncSession
    )
    monkeypatch.setattr(app_db, "engine", test_engine)
    monkeypatch.setattr(app_db, "async_session", test_sessionmaker)

    with TestClient(app) as c:
        yield c

    asyncio.run(test_engine.dispose())


def register_and_login(client, email="scout@example.com", password="s3cret-pw"):
    res = client.post(
        "/api/auth/register",
        json={"email": email, "password": password, "full_name": "Test Scout"},
    )
    assert res.status_code == 200, res.text

    res = client.post("/api/auth/login", data={"username": email, "password": password})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


# --- AUTH ---


def test_register_login_me(client):
    headers = register_and_login(client)

    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["email"] == "scout@example.com"


def test_me_requires_valid_token(client):
    assert client.get("/api/auth/me").status_code == 401
    assert (
        client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-token"}).status_code
        == 401
    )


def test_login_rejects_bad_credentials(client):
    register_and_login(client)
    res = client.post(
        "/api/auth/login", data={"username": "scout@example.com", "password": "wrong"}
    )
    assert res.status_code == 400


def test_register_duplicate_email(client):
    register_and_login(client)
    res = client.post(
        "/api/auth/register", json={"email": "scout@example.com", "password": "another-pw"}
    )
    assert res.status_code == 400


# --- READ ENDPOINTS ---


def test_tenders_and_analytics_empty(client):
    assert client.get("/api/tenders").json() == []
    assert client.get("/api/analytics/summary").json() == {
        "total_tenders": 0,
        "total_analyzed": 0,
        "high_fit_tenders": 0,
    }


def test_trigger_collect_requires_auth(client):
    assert client.post("/api/tenders/trigger-collect").status_code == 401


# --- COLLECTION PIPELINE (collectors + AI agent stubbed) ---


FAKE_NOTICE = {
    "external_id": "TEST-1",
    "source": "TEST",
    "title": "ESIA for the Test Corridor Project",
    "buyer": "Test Agency",
    "url": "https://example.com/tenders/test-1",
    "deadline_str": "2026-12-31",
    "raw_summary": "Consultancy services for an environmental and social impact assessment.",
}


class FakeAIResult:
    relevance_score = 7.5
    is_fit = True
    executive_summary = "Strong ESIA consultancy fit."
    matched_services = ["ESIA"]
    eligibility_gaps = ["NEMA lead firm licence"]
    suggested_next_steps = ["Download tender documents"]


@pytest.fixture()
def stubbed_pipeline(client, monkeypatch):
    import main as main_module

    monkeypatch.setattr(
        main_module.ungm_collector, "fetch_recent_notices", lambda: [dict(FAKE_NOTICE)]
    )
    monkeypatch.setattr(main_module.ppip_collector, "fetch_recent_notices", lambda: [])
    monkeypatch.setattr(main_module.worldbank_collector, "fetch_recent_notices", lambda: [])
    monkeypatch.setattr(main_module.tender_agent, "analyze_tender", lambda *a, **k: FakeAIResult())
    return client


def test_trigger_collect_pipeline(stubbed_pipeline):
    client = stubbed_pipeline
    headers = register_and_login(client)

    res = client.post("/api/tenders/trigger-collect", headers=headers)
    assert res.status_code == 200, res.text

    tenders = res.json()
    assert len(tenders) == 1
    assert tenders[0]["external_id"] == "TEST-1"
    assert tenders[0]["analysis"]["relevance_score"] == 7.5
    assert tenders[0]["analysis"]["is_fit"] is True


def test_trigger_collect_is_idempotent(stubbed_pipeline):
    client = stubbed_pipeline
    headers = register_and_login(client)

    client.post("/api/tenders/trigger-collect", headers=headers)
    res = client.post("/api/tenders/trigger-collect", headers=headers)
    assert res.status_code == 200
    assert len(res.json()) == 1, "duplicate notices must not be stored twice"

    assert client.get("/api/analytics/summary").json() == {
        "total_tenders": 1,
        "total_analyzed": 1,
        "high_fit_tenders": 1,
    }


def test_tenders_min_score_filter(stubbed_pipeline):
    client = stubbed_pipeline
    headers = register_and_login(client)
    client.post("/api/tenders/trigger-collect", headers=headers)

    below = client.get("/api/tenders", params={"min_score": 7.0})
    assert [t["external_id"] for t in below.json()] == ["TEST-1"]

    above = client.get("/api/tenders", params={"min_score": 8.0})
    assert above.json() == []
