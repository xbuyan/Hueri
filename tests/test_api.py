"""Offline smoke tests for the TenderScout API.

No network access happens here: collectors and the Gemini agent are
stubbed, and every test runs against a throwaway SQLite database
(the shared `client` fixture lives in conftest.py).
"""

import pytest
from fastapi.testclient import TestClient

import main as main_module


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
    import app.services.pipeline as pipeline_module

    async def fake_ungm():
        return [dict(FAKE_NOTICE)]

    async def fake_empty():
        return []

    monkeypatch.setattr(pipeline_module.ungm_collector, "fetch_recent_notices", fake_ungm)
    monkeypatch.setattr(pipeline_module.ppip_collector, "fetch_recent_notices", fake_empty)
    monkeypatch.setattr(pipeline_module.worldbank_collector, "fetch_recent_notices_async", fake_empty)
    monkeypatch.setattr(pipeline_module.tender_agent, "analyze_tender", lambda *a, **k: FakeAIResult())
    return client


def test_collectors_are_fully_async():
    """Regression guard: collectors must never go back to blocking requests."""
    import inspect

    from app.collectors import ungm, ppip, worldbank

    assert inspect.iscoroutinefunction(ungm.ungm_collector.fetch_recent_notices)
    assert inspect.iscoroutinefunction(ppip.ppip_collector.fetch_recent_notices)
    assert inspect.iscoroutinefunction(worldbank.worldbank_collector.fetch_recent_notices_async)

    for module in (ungm, ppip, worldbank):
        assert "import requests" not in inspect.getsource(module), (
            f"{module.__name__} regressed to blocking 'requests'"
        )


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


def test_change_password_flow(client):
    email = "pw-change@example.com"
    old_password = "s3cret-pw"
    new_password = "n3w-s3cret-pw-9"

    # register_and_login creates the account and returns auth headers
    headers = register_and_login(client, email=email, password=old_password)

    # Wrong current password is rejected
    res = client.post(
        "/api/auth/change-password",
        headers=headers,
        json={"current_password": "wrong", "new_password": new_password},
    )
    assert res.status_code == 400

    # New password must differ from old
    res = client.post(
        "/api/auth/change-password",
        headers=headers,
        json={"current_password": old_password, "new_password": old_password},
    )
    assert res.status_code == 400

    # Enforce minimum length via validation
    res = client.post(
        "/api/auth/change-password",
        headers=headers,
        json={"current_password": old_password, "new_password": "short"},
    )
    assert res.status_code == 422

    # Happy path: rotate, then login with the new password
    res = client.post(
        "/api/auth/change-password",
        headers=headers,
        json={"current_password": old_password, "new_password": new_password},
    )
    assert res.status_code == 200, res.text

    res = client.post(
        "/api/auth/login", data={"username": email, "password": new_password}
    )
    assert res.status_code == 200, "login with new password must succeed"
    res = client.post(
        "/api/auth/login", data={"username": email, "password": old_password}
    )
    assert res.status_code == 400, "old password must stop working"


def test_update_tender_status_workflow(stubbed_pipeline):
    """PATCH /api/tenders/{id}/status moves a tender through the pipeline."""
    client = stubbed_pipeline
    headers = register_and_login(client)
    client.post("/api/tenders/trigger-collect", headers=headers)

    res = client.get("/api/tenders")
    tender_id = res.json()[0]["id"]

    # Happy path: mark as bidding
    res = client.patch(
        f"/api/tenders/{tender_id}/status",
        headers=headers,
        json={"status": "BIDDING"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["analysis"]["status"] == "BIDDING"

    # Other transitions work too
    for status_value in ("UNDER_REVIEW", "DISCARDED", "NEW"):
        res = client.patch(
            f"/api/tenders/{tender_id}/status",
            headers=headers,
            json={"status": status_value},
        )
        assert res.status_code == 200
        assert res.json()["analysis"]["status"] == status_value


def test_update_tender_status_requires_auth(stubbed_pipeline):
    client = stubbed_pipeline
    headers = register_and_login(client)
    client.post("/api/tenders/trigger-collect", headers=headers)
    tender_id = client.get("/api/tenders").json()[0]["id"]

    res = client.patch(
        f"/api/tenders/{tender_id}/status", json={"status": "BIDDING"}
    )
    assert res.status_code in (401, 403)


def test_update_tender_status_404_and_validation(stubbed_pipeline):
    client = stubbed_pipeline
    headers = register_and_login(client)

    # Nonexistent tender
    res = client.patch(
        "/api/tenders/99999/status",
        headers=headers,
        json={"status": "BIDDING"},
    )
    assert res.status_code == 404

    # Invalid enum value -> 422 from Pydantic
    res = client.patch(
        "/api/tenders/1/status",
        headers=headers,
        json={"status": "NOT_A_STATUS"},
    )
    assert res.status_code == 422
