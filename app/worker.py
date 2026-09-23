"""arq worker: queued collection jobs + cron scheduling.

Run locally:   arq app.worker.WorkerSettings
Compose runs the same command in the `worker` service.

Jobs:
- run_collection_job  -> the full pipeline, result stored in Redis
- collect_and_notify  -> cron job: pipeline, then auto-clear the job
                         status key so the next manual run starts fresh

The status key ("collection:job") lets the API report queued/running/
complete/failed to the frontend while a manual run is in flight.
"""

import logging
from typing import Any, Dict

from arq import cron as arq_cron
from arq.connections import RedisSettings

from app.config import settings
from app.services.pipeline import (
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_RUNNING,
    run_collection_pipeline,
)

logger = logging.getLogger("worker")

JOB_STATUS_KEY = "collection:job"
JOB_ID = "manual_collect"


async def run_collection_job(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Queued manual collection run: pipeline, then publish the result."""
    await ctx["redis"].set(
        JOB_STATUS_KEY, '{"status": "running", "trigger": "manual"}'
    )
    try:
        summary = await run_collection_pipeline(ctx)
        await ctx["redis"].set(
            JOB_STATUS_KEY, '{"status": "complete", "trigger": "manual"}'
        )
        return summary
    except Exception:
        logger.exception("Manual collection job failed")
        await ctx["redis"].set(
            JOB_STATUS_KEY, '{"status": "failed", "trigger": "manual"}'
        )
        raise


async def collect_and_notify(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Scheduled cron run: same pipeline, but never leaves a stale
    "running" status behind for the API to report."""
    summary = await run_collection_pipeline(ctx)
    # Clear the manual-run status so a stale "running" never lingers.
    await ctx["redis"].delete(JOB_STATUS_KEY)
    return summary


async def on_startup(ctx: Dict[str, Any]) -> None:
    logger.info("Worker starting (redis=%s)", settings.REDIS_URL or "localhost")


async def on_shutdown(ctx: Dict[str, Any]) -> None:
    logger.info("Worker stopped")


def _cron_minutes() -> set:
    """Parse COLLECT_CRON_MINUTES (e.g. "0,30") into a set of minutes."""
    try:
        return {int(m) for m in settings.COLLECT_CRON_MINUTES.split(",") if m.strip()}
    except ValueError:
        logger.warning(
            "Invalid COLLECT_CRON_MINUTES=%r; falling back to 0,30",
            settings.COLLECT_CRON_MINUTES,
        )
        return {0, 30}


class WorkerSettings:
    """arq configuration: Redis connection, jobs, cron schedule."""

    functions = [run_collection_job, collect_and_notify]

    cron_jobs = [
        arq_cron(
            collect_and_notify,
            hour=None,
            minute=_cron_minutes(),
            run_at_startup=False,
            unique=True,
        )
    ]

    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL or "redis://localhost:6379/0")

    # Bounded retries; a failed run is visible via the status key.
    max_tries = 3
    job_timeout = 900  # 15 min: collectors + N Gemini calls can be slow

    on_startup = on_startup
    on_shutdown = on_shutdown
