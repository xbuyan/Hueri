"""The tender collection + analysis + alerting pipeline.

Lives in its own module so it can run inside the arq worker process (queued
and cron runs) as well as inline (fallback when Redis is unavailable).

Collection: the UNGM and World Bank collectors are synchronous (requests plus
politeness sleeps), so each one runs in its own worker thread via
asyncio.to_thread and the two sources run concurrently. PPIP is not collected
for now.

Each source returns (candidates, unreviewed):
  candidates  passed every filter; scored by Gemini.
  unreviewed  passed country/deadline but failed the keyword match; saved with
              no Gemini spend so nothing is silently lost.

Scoring is resumable: a saved tender without an analysis is scored when a
collector returns it again, so tenders that missed out because Gemini's quota
ran dry are picked up on a later run.

Alerting is based on the database, not on this run: every scored, real,
reviewed tender at or above the alert threshold (profile.yaml, or ALERT_MIN_SCORE if set) with no
alerted_at is alerted.
alerted_at is stamped only after a real delivery (an email or SMS actually
sent), so dev log-only runs and failed sends are retried next time.
"""

import asyncio
import datetime
import logging
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.profile import profile, alert_min_score
from app import db as app_db  # lazy attribute access keeps tests patchable
from app.models import Notification, Tender, TenderAnalysis, TenderStatus
from app.collectors.ungm import ungm_collector
from app.collectors.worldbank import worldbank_collector
from app.services.ai_agent import (
    tender_agent,
    TenderAgentError,
    QuotaExhaustedError,
    FatalAgentError,
)
from app.services.notifier import notifier

logger = logging.getLogger("pipeline")

# Who the company is and what it does, sent to Gemini with every tender.
# Comes from profile.yaml (see app/profile.py).
COMPANY_PROFILE = profile.company_dict()

STATUS_FAILED = "failed"
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"

# Only these notice keys are stored. Collectors also return helper keys
# (published_str, country, notice_type) that are not Tender columns.
_NOTICE_FIELDS = ("external_id", "source", "title", "buyer", "url", "deadline_str", "raw_summary")


def _tender_from_notice(notice: Dict[str, Any], **extra: Any) -> Tender:
    return Tender(**{key: notice.get(key) for key in _NOTICE_FIELDS}, **extra)


def _alert_dict(tender: Tender) -> dict:
    """Plain-dict snapshot for the notifier.

    Built while the session is open from a tender loaded with its analysis, so
    nothing lazy-loads later (that would raise MissingGreenlet outside the
    async session context).
    """
    analysis = tender.analysis
    return {
        "id": tender.id,
        "external_id": tender.external_id,
        "source": tender.source,
        "title": tender.title,
        "buyer": tender.buyer,
        "url": tender.url,
        "deadline_str": tender.deadline_str,
        "analysis": {
            "relevance_score": analysis.relevance_score,
            "executive_summary": analysis.executive_summary,
            "eligibility_gaps": analysis.eligibility_gaps,
        },
    }


async def _pending_alerts(db) -> List[Tender]:
    """Real, reviewed, scored tenders at or above the alert threshold that have
    not been alerted yet, best match first."""
    stmt = (
        select(Tender)
        .join(TenderAnalysis, TenderAnalysis.tender_id == Tender.id)
        .options(selectinload(Tender.analysis))
        .where(
            Tender.alerted_at.is_(None),
            Tender.is_unreviewed.is_(False),
            Tender.is_mock.is_(False),
            TenderAnalysis.relevance_score >= alert_min_score(),
        )
        .order_by(TenderAnalysis.relevance_score.desc())
    )
    # populate_existing: a tender scored earlier in this same run is already in the
    # session with its analysis loaded as None; without this that stale value is
    # reused instead of the analysis row that was just saved.
    result = await db.execute(stmt, execution_options={"populate_existing": True})
    return list(result.scalars().all())


async def run_collection_pipeline(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Collect notices, score them with the AI agent, alert management.

    Returns a summary dict with counters, stored as the job result.
    """
    sources = [("UNGM", ungm_collector), ("World Bank", worldbank_collector)]
    results = await asyncio.gather(
        *(asyncio.to_thread(collector.collect) for _, collector in sources),
        return_exceptions=True,
    )

    candidates: List[Dict[str, Any]] = []
    unreviewed: List[Dict[str, Any]] = []
    sources_failed: List[str] = []
    for (name, _), result in zip(sources, results):
        if isinstance(result, Exception):
            logger.error("Collector %s failed: %s", name, result)
            sources_failed.append(name)
            continue
        got, got_unreviewed = result
        logger.info(
            "Collector %s returned %d candidates, %d unreviewed",
            name, len(got), len(got_unreviewed),
        )
        candidates.extend(got)
        unreviewed.extend(got_unreviewed)

    saved_unreviewed = 0
    new_tenders = 0
    scored = 0
    unscored = 0
    analysis_errors = 0
    stop_reason = None
    alert_summary: Dict[str, int] = {}
    pending: List[Tender] = []

    async with app_db.async_session() as db:
        # --- 1. Save notices that failed only the keyword match (no Gemini spend) ---
        if unreviewed:
            ids = [n["external_id"] for n in unreviewed]
            res = await db.execute(select(Tender.external_id).where(Tender.external_id.in_(ids)))
            existing_ids = set(res.scalars().all())
            for notice in unreviewed:
                if notice["external_id"] in existing_ids:
                    continue
                db.add(_tender_from_notice(notice, is_mock=False, is_unreviewed=True))
                saved_unreviewed += 1
            await db.commit()

        # --- 2. Score the candidates ---
        for notice in candidates:
            res = await db.execute(
                select(Tender)
                .options(selectinload(Tender.analysis))
                .where(Tender.external_id == notice["external_id"])
            )
            tender = res.scalar_one_or_none()

            if tender is not None and tender.analysis is not None:
                continue  # already scored on an earlier run

            if tender is None:
                tender = _tender_from_notice(notice, is_mock=False)
                db.add(tender)
                await db.flush()
                new_tenders += 1
            elif tender.is_unreviewed:
                tender.is_unreviewed = False  # now passes the keyword filter

            # Quota is gone for this run: keep the tender saved, score it later.
            if stop_reason:
                unscored += 1
                await db.commit()
                continue

            try:
                # The Gemini SDK call blocks and sleeps for pacing: keep it off the loop.
                ai_eval = await asyncio.to_thread(
                    tender_agent.analyze_tender,
                    tender.title,
                    tender.buyer or "",
                    tender.raw_summary or "",
                    COMPANY_PROFILE,
                )
            except FatalAgentError:
                await db.commit()  # keep everything saved so far
                logger.error("Aborting run: Gemini rejected the API key")
                raise
            except QuotaExhaustedError as exc:
                logger.warning("Quota exhausted, stopping scoring for this run: %s", exc)
                stop_reason = "quota_exhausted"
                unscored += 1
                await db.commit()
                continue
            except Exception as exc:  # noqa: BLE001 - one bad tender must not sink the run
                logger.error("AI analysis failed for tender %s: %s", tender.external_id, exc)
                analysis_errors += 1
                unscored += 1  # no analysis row: retried on the next run
                await db.commit()
                continue

            db.add(
                TenderAnalysis(
                    tender_id=tender.id,
                    relevance_score=ai_eval.relevance_score,
                    is_fit=ai_eval.is_fit,
                    executive_summary=ai_eval.executive_summary,
                    matched_services=ai_eval.matched_services,
                    eligibility_gaps=ai_eval.eligibility_gaps,
                    suggested_next_steps=ai_eval.suggested_next_steps,
                    status=TenderStatus.NEW,
                )
            )
            scored += 1
            await db.commit()  # per tender, so a later failure loses nothing

        await db.commit()

        # --- 3. Management alerts (email + SMS) for everything not yet alerted ---
        if settings.ALERTS_ENABLED:
            pending = await _pending_alerts(db)
            if pending:
                try:
                    alert_summary = await notifier.send_tender_alert(
                        [_alert_dict(t) for t in pending]
                    )
                    logger.info("Alert run summary: %s", alert_summary)

                    email_status = (
                        "sent" if alert_summary.get("emails_sent")
                        else "logged" if alert_summary.get("emails_logged")
                        else "skipped"
                    )
                    sms_status = (
                        "sent" if alert_summary.get("sms_sent")
                        else "logged" if alert_summary.get("sms_logged")
                        else "skipped"
                    )
                    for t in pending:
                        if email_status != "skipped":
                            db.add(Notification(
                                tender_id=t.id, channel="email",
                                recipient=", ".join(settings.alert_email_recipients_list()) or "management",
                                status=email_status,
                            ))
                        if sms_status != "skipped":
                            db.add(Notification(
                                tender_id=t.id, channel="sms",
                                recipient=", ".join(settings.alert_sms_recipients_list()) or "management",
                                status=sms_status,
                            ))

                    # Stamp alerted_at only after a REAL delivery. Log-only (dev)
                    # runs and failed sends leave it empty so they are retried.
                    if alert_summary.get("emails_sent", 0) + alert_summary.get("sms_sent", 0) > 0:
                        now = datetime.datetime.utcnow()
                        for t in pending:
                            t.alerted_at = now
                    await db.commit()
                except Exception:
                    logger.exception("Alerting failed for %d tenders", len(pending))

    summary = {
        "notices": len(candidates),
        "unreviewed_saved": saved_unreviewed,
        "new_tenders": new_tenders,
        "analyzed": scored,
        "analysis_errors": analysis_errors,
        "unscored": unscored,
        "stopped_reason": stop_reason or "none",
        "sources_failed": sources_failed,
        "high_fit": len(pending),
        "emails_sent": alert_summary.get("emails_sent", 0) + alert_summary.get("emails_logged", 0),
        "sms_sent": alert_summary.get("sms_sent", 0) + alert_summary.get("sms_logged", 0),
    }
    logger.info("Pipeline run complete: %s", summary)
    return summary
