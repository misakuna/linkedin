"""APScheduler wiring.

What: creates and owns the background scheduler.

Why: this service exists partly because posting has to happen at a specific,
randomly-chosen minute on specific weekdays — that needs a long-running process
with a clock, which is exactly why serverless hosting is ruled out.

How: a BackgroundScheduler (thread pool, not asyncio) because the jobs it will
run make blocking HTTP calls to Claude, Buffer, and the SMS provider. It is
created with the client's timezone so "Tuesday 9:14am" means Pacific, not UTC.

No jobs are registered yet — those arrive in STEP 3.
"""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import AppConfig
from app.logging_setup import get_logger

logger = get_logger(__name__)


def create_scheduler(config: AppConfig) -> BackgroundScheduler:
    """Build a scheduler in the client's timezone, with no jobs attached.

    `coalesce` and `misfire_grace_time` matter on a hobby-tier host: if the
    container is briefly restarted over a scheduled minute, we want one late
    run rather than a burst of catch-up runs or a silently skipped post.
    """
    scheduler = BackgroundScheduler(
        timezone=config.client.tz,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 60 * 30,  # 30 minutes
        },
    )
    return scheduler


def start_scheduler(scheduler: BackgroundScheduler) -> None:
    """Start the scheduler and say so in plain language."""
    scheduler.start()
    job_count = len(scheduler.get_jobs())
    if job_count == 0:
        logger.info("Scheduler is running. No scheduled jobs yet.")
    else:
        logger.info("Scheduler is running with %s scheduled job(s).", job_count)


def shutdown_scheduler(scheduler: BackgroundScheduler) -> None:
    """Stop the scheduler without waiting on in-flight jobs to finish."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")
