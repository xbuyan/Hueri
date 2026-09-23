"""Tests for the arq worker and the queue/inline trigger behaviour."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.pipeline import run_collection_pipeline
from app.worker import JOB_STATUS_KEY, collect_and_notify, run_collection_job


def test_cron_minutes_parses_config():
    from app.worker import _cron_minutes

    original = settings.COLLECT_CRON_MINUTES
    try:
        settings.COLLECT_CRON_MINUTES = "0,15,30,45"
        assert _cron_minutes() == {0, 15, 30, 45}

        settings.COLLECT_CRON_MINUTES = "bogus"
        assert _cron_minutes() == {0, 30}  # safe fallback
    finally:
        settings.COLLECT_CRON_MINUTES = original


@pytest.mark.asyncio
async def test_run_collection_job_sets_status_keys(monkeypatch):
    """The queued job publishes running -> complete status around the pipeline."""
    fake_redis = AsyncMock()
    ctx = {"redis": fake_redis}

    async def fake_pipeline(_ctx):
        return {"notices": 3, "new_tenders": 2}

    monkeypatch.setattr("app.worker.run_collection_pipeline", fake_pipeline)

    summary = await run_collection_job(ctx)

    assert summary["new_tenders"] == 2
    assert fake_redis.set.await_count == 2
    first_call = fake_redis.set.await_args_list[0]
    assert first_call.args[0] == JOB_STATUS_KEY
    assert '"running"' in first_call.args[1]
    last_call = fake_redis.set.await_args_list[-1]
    assert '"complete"' in last_call.args[1]


@pytest.mark.asyncio
async def test_run_collection_job_marks_failure(monkeypatch):
    fake_redis = AsyncMock()
    ctx = {"redis": fake_redis}

    async def boom(_ctx):
        raise RuntimeError("collectors down")

    monkeypatch.setattr("app.worker.run_collection_pipeline", boom)

    with pytest.raises(RuntimeError):
        await run_collection_job(ctx)

    last_call = fake_redis.set.await_args_list[-1]
    assert '"failed"' in last_call.args[1]


@pytest.mark.asyncio
async def test_collect_and_notify_clears_status_key(monkeypatch):
    fake_redis = AsyncMock()

    async def fake_pipeline(_ctx):
        return {"notices": 1}

    monkeypatch.setattr("app.worker.run_collection_pipeline", fake_pipeline)

    summary = await collect_and_notify({"redis": fake_redis})

    assert summary == {"notices": 1}
    fake_redis.delete.assert_awaited_once_with(JOB_STATUS_KEY)


def test_trigger_collect_inline_when_no_redis(client, monkeypatch):
    """Without REDIS_URL the endpoint runs the pipeline inline (dev/CI path)."""
    import main as main_module

    monkeypatch.setattr(main_module, "_arq_pool", None)

    async def fake_pipeline(_ctx):
        return {"notices": 0, "new_tenders": 0, "analyzed": 0}

    monkeypatch.setattr(main_module, "run_collection_pipeline", fake_pipeline)

    res = client.post(
        "/api/auth/register", json={"email": "q@example.com", "password": "s3cret-pw"}
    )
    assert res.status_code == 200
    login = client.post(
        "/api/auth/login", data={"username": "q@example.com", "password": "s3cret-pw"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    collect = client.post("/api/tenders/trigger-collect", headers=headers)
    assert collect.status_code == 200
    assert isinstance(collect.json(), list)  # inline mode returns tenders list

    status = client.get("/api/tenders/collect-status", headers=headers)
    assert status.status_code == 200
    assert status.json() == {"status": "inline"}


def test_trigger_collect_queues_when_pool_available(client, monkeypatch):
    """With an arq pool present the endpoint enqueues and returns queued."""
    import main as main_module

    fake_pool = AsyncMock()
    monkeypatch.setattr(main_module, "_arq_pool", fake_pool)

    res = client.post(
        "/api/auth/register", json={"email": "q2@example.com", "password": "s3cret-pw"}
    )
    assert res.status_code == 200
    login = client.post(
        "/api/auth/login", data={"username": "q2@example.com", "password": "s3cret-pw"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    collect = client.post("/api/tenders/trigger-collect", headers=headers)
    assert collect.status_code == 200
    body = collect.json()
    assert body["status"] == "queued"
    assert body["job_id"] == "manual_collect"

    fake_pool.enqueue_job.assert_awaited_once_with(
        "run_collection_job", _job_id="manual_collect"
    )


def test_pipeline_module_exists_and_exports():
    """Guard against accidental circular-import regressions."""
    import app.services.pipeline as pm

    assert callable(pm.run_collection_pipeline)
    assert pm.HUERI_PROFILE["company_name"] == "HUERI Limited"
    assert {pm.STATUS_QUEUED, pm.STATUS_RUNNING, pm.STATUS_COMPLETE, pm.STATUS_FAILED} == {
        "queued",
        "running",
        "complete",
        "failed",
    }
