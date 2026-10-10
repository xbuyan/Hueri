#!/usr/bin/env python3
"""apply_patch10.py - hotfix found by the first live run of the integrated pipeline.

Run from the ROOT of the integrate-live-collectors worktree
(~/projects/Hueri-full). It only touches app/services/pipeline.py and adds
tests/test_pipeline.py, so it does not matter whether the earlier patches are
committed yet:

    python apply_patch10.py
    python -m pytest -q -p no:warnings 2>&1 | tail -5

To undo:  git checkout -- app/services/pipeline.py ; rm tests/test_pipeline.py

THE BUG
  The live run scored a tender that an earlier run had already saved (without an
  analysis), then crashed while alerting:
      AttributeError: 'NoneType' object has no attribute 'relevance_score'
  Cause: while scoring, the tender was loaded with its analysis (None at that
  time). The session keeps that same object, so when the alert query ran later it
  got the stale "analysis = None" back instead of the row just saved. The tests
  missed it because they only covered brand-new tenders.

THE FIX
  The alert query now uses populate_existing, so objects already in the session
  are refreshed from the database, including their analysis.

NEW TESTS (tests/test_pipeline.py, 6 tests) cover this exact path plus the other
pipeline rules that had no direct test: alerted_at is stamped only after a real
delivery and never alerts twice, log-only runs leave it empty, unreviewed notices
are saved without calling Gemini, quota exhaustion stops scoring but keeps the
tenders, and a rejected Gemini key aborts the run after saving.
"""
import pathlib
import py_compile
import subprocess
import sys

ROOT = pathlib.Path.cwd()
EXPECTED_BRANCH = "integrate-live-collectors"
PIPELINE = ROOT / "app" / "services" / "pipeline.py"
TEST_PIPELINE = ROOT / "tests" / "test_pipeline.py"

OLD = "    return list((await db.execute(stmt)).scalars().all())\n"
NEW = (
    "    # populate_existing: a tender scored earlier in this same run is already in the\n"
    "    # session with its analysis loaded as None; without this that stale value is\n"
    "    # reused instead of the analysis row that was just saved.\n"
    '    result = await db.execute(stmt, execution_options={"populate_existing": True})\n'
    "    return list(result.scalars().all())\n"
)

TEST_SOURCE = r'''"""Pipeline behaviour: resumable scoring, unreviewed notices, quota handling and
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
'''


def fail(msg):
    print(f"ABORTED: {msg}\nNothing was changed.")
    sys.exit(1)


def git(*args):
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError as exc:
        fail(f"git {' '.join(args)} failed: {exc.stderr.strip()}")


def smoke_check():
    proc = subprocess.run(
        [sys.executable, "-c", "import app.services.pipeline"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return True, "pipeline imports"
    if "No module named" in proc.stderr:
        return True, "skipped (project packages are not importable from this interpreter)"
    return False, proc.stderr.strip()[-1500:]


def main():
    if not PIPELINE.exists():
        fail("app/services/pipeline.py not found - run this from ~/projects/Hueri-full")
    original = PIPELINE.read_text()
    if "populate_existing" in original:
        print("pipeline.py already contains the fix - nothing to do.")
        return
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != EXPECTED_BRANCH:
        fail(f"you are on branch '{branch}', expected '{EXPECTED_BRANCH}'")
    if git("status", "--porcelain", "--", "app/services/pipeline.py").stdout.strip():
        fail("app/services/pipeline.py has uncommitted changes; commit or revert them first")
    if TEST_PIPELINE.exists():
        fail("tests/test_pipeline.py already exists; resolve by hand")
    if original.count(OLD) != 1:
        fail(f"pipeline.py: expected exactly one match for the alert query return, found {original.count(OLD)}")

    try:
        PIPELINE.write_text(original.replace(OLD, NEW))
        print("wrote app/services/pipeline.py")
        TEST_PIPELINE.write_text(TEST_SOURCE)
        print("wrote tests/test_pipeline.py")
        for path in (PIPELINE, TEST_PIPELINE):
            py_compile.compile(str(path), doraise=True)
        ok, message = smoke_check()
        if not ok:
            raise RuntimeError("the pipeline failed to import after the patch:\n" + message)
        print(f"smoke check: {message}")
    except Exception as exc:  # roll back on any failure
        print(f"ERROR: {exc}\nRolling back...")
        PIPELINE.write_text(original)
        if TEST_PIPELINE.exists():
            TEST_PIPELINE.unlink()
        print("Rolled back. Nothing was changed.")
        sys.exit(1)

    print("\nDone. Next:")
    print("  python -m pytest -q -p no:warnings 2>&1 | tail -5     # expect all green (66 tests)")
    print("  then restart the server (Ctrl+C, start it again): it has no auto-reload")


if __name__ == "__main__":
    main()
