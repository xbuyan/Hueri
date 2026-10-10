"""Tests for the scheduler settings and the account/security hardening."""

import asyncio
import importlib.util
import pathlib

import pytest

from app.config import Settings, settings


def _load_create_user_script():
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "create_user.py"
    spec = importlib.util.spec_from_file_location("create_user_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- Registration ---


def test_registration_is_closed_when_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_REGISTRATION", False)
    res = client.post(
        "/api/auth/register", json={"email": "new@example.com", "password": "s3cret-pw"}
    )
    assert res.status_code == 403
    assert "closed" in res.json()["detail"].lower()


def test_registration_defaults_to_closed():
    assert Settings.model_fields["ALLOW_REGISTRATION"].default is False


def test_create_user_script_creates_and_resets_accounts(client):
    script = _load_create_user_script()

    asyncio.run(script.create_or_reset_user("admin@example.com", "Admin", "first-pw-123", reset=False))
    login = client.post(
        "/api/auth/login", data={"username": "admin@example.com", "password": "first-pw-123"}
    )
    assert login.status_code == 200

    with pytest.raises(script.UserError):  # duplicate
        asyncio.run(script.create_or_reset_user("admin@example.com", None, "other-pw-123", reset=False))
    with pytest.raises(script.UserError):  # too short
        asyncio.run(script.create_or_reset_user("short@example.com", None, "abc", reset=False))
    with pytest.raises(script.UserError):  # not an email
        asyncio.run(script.create_or_reset_user("not-an-email", None, "long-enough-pw", reset=False))

    asyncio.run(script.create_or_reset_user("admin@example.com", None, "second-pw-123", reset=True))
    old = client.post(
        "/api/auth/login", data={"username": "admin@example.com", "password": "first-pw-123"}
    )
    new = client.post(
        "/api/auth/login", data={"username": "admin@example.com", "password": "second-pw-123"}
    )
    assert old.status_code == 400
    assert new.status_code == 200

    with pytest.raises(script.UserError):  # reset for a user that does not exist
        asyncio.run(script.create_or_reset_user("ghost@example.com", None, "pw-12345678", reset=True))


# --- SECRET_KEY guard ---


def test_weak_secret_key_is_rejected_in_production():
    with pytest.raises(ValueError):
        Settings(_env_file=None, ENVIRONMENT="production", SECRET_KEY="super-secret-key")
    with pytest.raises(ValueError):
        Settings(_env_file=None, ENVIRONMENT="production", SECRET_KEY="too-short")


def test_strong_secret_key_is_accepted_in_production():
    strong = Settings(_env_file=None, ENVIRONMENT="production", SECRET_KEY="x" * 48)
    assert strong.ENVIRONMENT == "production"


def test_weak_secret_key_is_still_allowed_in_development():
    Settings(_env_file=None, ENVIRONMENT="development", SECRET_KEY="super-secret-key")


# --- Scheduler ---


def test_cron_hours_parse_and_fall_back():
    from app.worker import _cron_hours

    original = settings.COLLECT_CRON_HOURS
    try:
        settings.COLLECT_CRON_HOURS = "5,13"
        assert _cron_hours() == {5, 13}
        settings.COLLECT_CRON_HOURS = "6"
        assert _cron_hours() == {6}
        for bad in ("bogus", "25", "-1", ""):
            settings.COLLECT_CRON_HOURS = bad
            assert _cron_hours() == {5, 13}, bad
    finally:
        settings.COLLECT_CRON_HOURS = original


def test_default_schedule_is_twice_a_day():
    assert Settings.model_fields["COLLECT_CRON_HOURS"].default == "5,13"
    assert Settings.model_fields["COLLECT_CRON_MINUTES"].default == "0"


def test_cron_job_uses_the_configured_hours_and_minutes():
    from app.worker import WorkerSettings, _cron_hours, _cron_minutes

    job = WorkerSettings.cron_jobs[0]
    assert job.hour == _cron_hours()
    assert job.minute == _cron_minutes()
