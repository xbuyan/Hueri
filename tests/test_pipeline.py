"""Pipeline behaviour: resumable scoring, unreviewed notices, quota handling and
the alerted_at rules. Collectors and Gemini are stubbed; the database is a
throwaway SQLite file (the shared `client` fixture patches it in)."""

import asyncio

import pytest
from sqlalchemy import select

from app import db as app_db
from app.config import settings
from app.models import Notification, Tender, TenderAnalysis
from app.services.ai_agent import FatalAgentError, QuotaExhaustedError


def _notice(n, title="ESIA for the Test Corridor Project"):
    return {
        "external_id": f"PIPE-{n}",
        "source": "TEST",
        "title": f"{title} {n}",
        "buyer": "Test Agency",
        "url": f"https://example.com/tenders/pipe-{n}",
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


async def _rows(model):
    async with app_db.async_session() as db:
        return list((await db.execute(select(model))).scalars().all())


async def _save_tender(notice, **extra):
    async with app_db.async_session() as db:
        db.add(Tender(**notice, is_mock=False, **extra))
        await db.commit()


@pytest.fixture()
def pipeline(client, monkeypatch):
    import app.services.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module.ungm_collector, "collect", lambda: ([_notice(1)], []))
    monkeypatch.setattr(pipeline_module.worldbank_collector, "collect", lambda: ([], []))
    monkeypatch.setattr(
        pipeline_module.tender_agent, "analyze_tender", lambda *a, **k: FakeAIResult()
    )
    monkeypatch.setattr(settings, "ALERT_EMAIL_RECIPIENTS", "mgr@example.com")
    return pipeline_module


def _run(pipeline_module):
    return asyncio.run(pipeline_module.run_collection_pipeline({}))


def test_saved_but_unscored_tender_is_scored_and_alerted(pipeline):
    """Regression: a tender saved by an earlier run (for example while Gemini was
    unavailable) is scored when a collector returns it again, and then alerted.
    Alerting used to crash because the tender kept a stale 'analysis = None'."""
    asyncio.run(_save_tender(_notice(1), is_unreviewed=True))

    summary = _run(pipeline)

    assert summary["new_tenders"] == 0
    assert summary["analyzed"] == 1
    assert summary["high_fit"] == 1
    assert summary["emails_sent"] == 1  # a log-only (dev) run still counts as handled
    tender = asyncio.run(_rows(Tender))[0]
    assert tender.is_unreviewed is False
    assert {row.channel for row in asyncio.run(_rows(Notification))} == {"email"}


def test_alerted_at_is_stamped_only_after_a_real_delivery(pipeline, monkeypatch):
    calls = []

    async def fake_send(tenders):
        calls.append([t["external_id"] for t in tenders])
        return {
            "emails_sent": 1, "emails_logged": 0,
            "sms_sent": 0, "sms_logged": 0, "skipped_duplicates": 0,
        }

    monkeypatch.setattr(pipeline.notifier, "send_tender_alert", fake_send)

    _run(pipeline)
    assert calls == [["PIPE-1"]]
    assert asyncio.run(_rows(Tender))[0].alerted_at is not None

    _run(pipeline)
    assert len(calls) == 1  # already alerted: never sent twice


def test_log_only_runs_leave_alerted_at_empty(pipeline):
    _run(pipeline)  # ENVIRONMENT=testing: the notifier only logs, nothing is delivered
    assert asyncio.run(_rows(Tender))[0].alerted_at is None


def test_unreviewed_notices_are_saved_without_calling_gemini(pipeline, monkeypatch):
    gemini_calls = []
    monkeypatch.setattr(
        pipeline.ungm_collector, "collect", lambda: ([], [_notice(7, "Supply of fuel")])
    )
    monkeypatch.setattr(
        pipeline.tender_agent,
        "analyze_tender",
        lambda *a, **k: gemini_calls.append(a) or FakeAIResult(),
    )

    first = _run(pipeline)
    second = _run(pipeline)

    assert first["unreviewed_saved"] == 1
    assert second["unreviewed_saved"] == 0  # not saved twice
    assert gemini_calls == []
    tenders = asyncio.run(_rows(Tender))
    assert len(tenders) == 1
    assert tenders[0].is_unreviewed is True
    assert asyncio.run(_rows(TenderAnalysis)) == []


def test_quota_exhaustion_stops_scoring_but_keeps_the_tenders(pipeline, monkeypatch):
    monkeypatch.setattr(
        pipeline.ungm_collector, "collect", lambda: ([_notice(1), _notice(2)], [])
    )

    def out_of_quota(*args, **kwargs):
        raise QuotaExhaustedError("daily quota gone")

    monkeypatch.setattr(pipeline.tender_agent, "analyze_tender", out_of_quota)

    summary = _run(pipeline)

    assert summary["stopped_reason"] == "quota_exhausted"
    assert summary["analyzed"] == 0
    assert summary["unscored"] == 2
    assert len(asyncio.run(_rows(Tender))) == 2  # saved, to be scored on a later run
    assert asyncio.run(_rows(TenderAnalysis)) == []


def test_rejected_gemini_key_aborts_the_run_after_saving(pipeline, monkeypatch):
    def bad_key(*args, **kwargs):
        raise FatalAgentError("API key rejected")

    monkeypatch.setattr(pipeline.tender_agent, "analyze_tender", bad_key)

    with pytest.raises(FatalAgentError):
        _run(pipeline)

    assert len(asyncio.run(_rows(Tender))) == 1
