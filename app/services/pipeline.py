"""The tender collection + analysis + alerting pipeline.

Lives in its own module so it can run inside the arq worker process
(queues) as well as inline (fallback when Redis is unavailable).

All network I/O is non-blocking: collectors use httpx.AsyncClient and
run concurrently on the event loop; the Gemini SDK call (blocking) runs
in a worker thread via asyncio.to_thread.
"""

import asyncio
import logging
from typing import Any, Dict, List

from sqlalchemy import select

from app.config import settings
from app import db as app_db  # lazy attribute access keeps tests patchable
from app.models import Notification, Tender, TenderAnalysis, TenderStatus
from app.collectors.ungm import ungm_collector
from app.collectors.ppip import ppip_collector
from app.collectors.worldbank import worldbank_collector
from app.services.ai_agent import tender_agent
from app.services.notifier import notifier

logger = logging.getLogger("pipeline")

HUERI_PROFILE = {
    "company_name": "HUERI Limited",
    "description": "Environmental, social, and sustainability engineering consultancy based in Nairobi, Kenya.",
    "core_services": ["Environmental Impact Assessment (EIA)", "ESIA", "Environmental Audits", "Resettlement Action Plans (RAP)", "M&E"],
    "target_geographies": ["Kenya", "Uganda", "Tanzania", "East Africa"],
}

STATUS_FAILED = "failed"
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"


def _tender_alert_dict(tender: Tender, ai_eval) -> dict:
    """Plain-dict snapshot for the notifier.

    The notifier must never touch ORM relationship attributes
    (e.g. tender.analysis): lazy-loading them outside the async session
    context raises MissingGreenlet. Column values on `tender` are already
    loaded, so they are safe.
    """
    return {
        "id": tender.id,
        "external_id": tender.external_id,
        "source": tender.source,
        "title": tender.title,
        "buyer": tender.buyer,
        "url": tender.url,
        "deadline_str": tender.deadline_str,
        "analysis": {
            "relevance_score": ai_eval.relevance_score,
            "executive_summary": ai_eval.executive_summary,
            "eligibility_gaps": ai_eval.eligibility_gaps,
        },
    }


async def run_collection_pipeline(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Collect notices, evaluate them with the AI agent, alert management.

    Returns a summary dict with counters stored as the job result.
    """
    all_notices: List[Dict[str, Any]] = []

    # Collectors are fully async (httpx.AsyncClient) - run them concurrently.
    collectors = [
        ("UNGM", ungm_collector.fetch_recent_notices()),
        ("PPIP", ppip_collector.fetch_recent_notices()),
        ("WB", worldbank_collector.fetch_recent_notices_async()),
    ]
    results = await asyncio.gather(
        *(coro for _, coro in collectors), return_exceptions=True
    )
    for (name, _), result in zip(collectors, results):
        if isinstance(result, Exception):
            logger.error("Collector %s failed: %s", name, result)
        else:
            logger.info("Collector %s returned %d notices", name, len(result))
            all_notices.extend(result)

    new_tenders = 0
    analyzed = 0
    analysis_errors = 0
    high_fit: List[Dict[str, Any]] = []

    async with app_db.async_session() as db:
        for notice in all_notices:
            stmt = select(Tender).where(Tender.external_id == notice["external_id"])
            res = await db.execute(stmt)
            if res.scalar_one_or_none():
                continue

            tender = Tender(**notice, is_mock=False)
            db.add(tender)
            await db.flush()
            new_tenders += 1

            try:
                # The Gemini SDK call is blocking - keep it off the loop.
                ai_eval = await asyncio.to_thread(
                    tender_agent.analyze_tender,
                    tender.title,
                    tender.buyer or "",
                    tender.raw_summary or "",
                    HUERI_PROFILE,
                )
                db.add(TenderAnalysis(
                    tender_id=tender.id,
                    relevance_score=ai_eval.relevance_score,
                    is_fit=ai_eval.is_fit,
                    executive_summary=ai_eval.executive_summary,
                    matched_services=ai_eval.matched_services,
                    eligibility_gaps=ai_eval.eligibility_gaps,
                    suggested_next_steps=ai_eval.suggested_next_steps,
                    status=TenderStatus.NEW,
                ))
                analyzed += 1
                if ai_eval.relevance_score >= settings.ALERT_MIN_SCORE:
                    high_fit.append(_tender_alert_dict(tender, ai_eval))
            except Exception as eval_err:
                analysis_errors += 1
                logger.error("AI analysis failed for tender %s: %s", tender.external_id, eval_err)

        await db.commit()

        # --- Management alerts (email + SMS) ---
        alert_summary: Dict[str, int] = {}
        if settings.ALERTS_ENABLED and high_fit:
            try:
                alert_summary = await notifier.send_tender_alert(high_fit)
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
                for t in high_fit:
                    if email_status != "skipped":
                        db.add(Notification(
                            tender_id=t["id"], channel="email",
                            recipient=", ".join(settings.alert_email_recipients_list()) or "management",
                            status=email_status,
                        ))
                    if sms_status != "skipped":
                        db.add(Notification(
                            tender_id=t["id"], channel="sms",
                            recipient=", ".join(settings.alert_sms_recipients_list()) or "management",
                            status=sms_status,
                        ))
                await db.commit()
            except Exception:
                logger.exception("Alerting failed for %d tenders", len(high_fit))

    summary = {
        "notices": len(all_notices),
        "new_tenders": new_tenders,
        "analyzed": analyzed,
        "analysis_errors": analysis_errors,
        "high_fit": len(high_fit),
        "emails_sent": alert_summary.get("emails_sent", 0) + alert_summary.get("emails_logged", 0),
        "sms_sent": alert_summary.get("sms_sent", 0) + alert_summary.get("sms_logged", 0),
    }
    logger.info("Pipeline run complete: %s", summary)
    return summary
