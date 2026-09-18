"""Wiring the services together and putting them on the scheduler.

What: builds the inbox, posting, and approval services, and registers the four
recurring jobs that make the assistant run on its own.

Why: kept in one file so there is a single place to answer "what does this
system do while nobody is watching?" — which is the question the developer will be
asked, and the question a client asks about anything automated.

The honest answer, in full:

  every 15 minutes   read David's Gmail for LinkedIn notifications, draft replies
                     (and text him if people are waiting, at most one text
                     every alerts.new_items_min_minutes, never in quiet hours)
  Tue/Thu 7:00am     write David's post and text it to him for approval
  daily 8:30am       text him one summary of what's waiting, if anything is
  every 30 minutes   retry approved posts that couldn't reach Buffer
  hourly             close out approval requests David never answered

Nothing on that list contacts linkedin.com, and nothing on it publishes
anything David hasn't approved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.approvals.alerts import AlertService
from app.approvals.service import ApprovalService
from app.chat.service import ChatService
from app.config import AppConfig
from app.db import DraftRepository
from app.db.alerts_repository import AlertsRepository
from app.db.chat_repository import ChatRepository
from app.inbox.service import InboxService
from app.logging_setup import get_logger
from app.posting.service import PostingService

logger = get_logger(__name__)

RETRY_QUEUE_MINUTES = 30
EXPIRE_APPROVALS_HOURS = 1

# Long enough that the first inbox check happens after startup has finished and
# the health check has passed, short enough that a deploy is never followed by a
# visibly empty dashboard.
POLL_ON_BOOT_DELAY_SECONDS = 10


@dataclass(frozen=True, slots=True)
class Services:
    """Every service, built once and shared by the jobs and the dashboard.

    Only the first three have scheduled jobs; chat runs only when David asks
    something, so it never appears in the list of things that happen unattended.
    """

    inbox: InboxService
    posting: PostingService
    approvals: ApprovalService
    chat: ChatService
    alerts: AlertService


def build_services(config: AppConfig, repository: DraftRepository) -> Services:
    """Construct every service, with live-or-mock clients chosen by config."""
    approvals = ApprovalService(config, repository)
    return Services(
        inbox=InboxService(config, repository),
        posting=PostingService(config, repository, approvals),
        approvals=approvals,
        chat=ChatService(config, repository, ChatRepository(repository.db_path)),
        alerts=AlertService(config, repository, AlertsRepository(repository.db_path)),
    )


# --------------------------------------------------------------------------
# The jobs themselves. Each one catches its own exceptions: an unhandled error
# in a scheduled job kills that job for the life of the process, which would
# mean silent failure — the worst outcome for a system nobody is watching.
# --------------------------------------------------------------------------


def poll_inbox(services: Services) -> None:
    """Read LinkedIn notification emails, draft replies, and nudge David."""
    try:
        result = services.inbox.poll()
    except Exception:
        logger.exception("The inbox check didn't finish. It'll run again shortly.")
        return

    if result.created == 0:
        return

    # Separate try: a failed text must not make the poll look like it failed.
    # The cards are already saved and showing on his dashboard either way.
    try:
        services.alerts.notify_new_items()
    except Exception:
        logger.exception("Couldn't text David about the new cards. They're on his dashboard.")


def send_daily_digest(services: Services) -> None:
    """Text David one summary of what's waiting on him."""
    try:
        services.alerts.send_daily_digest()
    except Exception:
        logger.exception("Couldn't send David's daily summary text.")


def prepare_todays_post(services: Services, config: AppConfig) -> None:
    """Write today's post and text David for approval."""
    today = datetime.now(config.client.tz).date()
    try:
        services.posting.prepare(today)
    except Exception:
        logger.exception(
            "Couldn't get today's post ready. Nothing was published — text David "
            "if he's expecting one."
        )


def retry_unqueued_posts(services: Services) -> None:
    """Re-attempt approved posts that couldn't reach the publisher."""
    try:
        services.posting.retry_unqueued()
    except Exception:
        logger.exception("Couldn't retry scheduling approved posts. Will try again.")


def expire_stale_approvals(services: Services) -> None:
    """Close out approval requests David never answered."""
    try:
        services.approvals.expire_stale()
    except Exception:
        logger.exception("Couldn't tidy up old approval requests.")


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


def _cron_days(config: AppConfig) -> str:
    """config.yaml's weekday names in the form APScheduler wants."""
    return ",".join(day[:3] for day in config.posting.days)


def register_jobs(scheduler: BaseScheduler, config: AppConfig, services: Services) -> None:
    """Put every recurring job on the scheduler and say so in the log."""
    tz = config.client.tz

    scheduler.add_job(
        poll_inbox,
        IntervalTrigger(minutes=config.inbox.poll_minutes, timezone=tz),
        args=[services],
        id="poll_inbox",
        name="Check Gmail for LinkedIn notifications",
        replace_existing=True,
        # Run once shortly after boot, then on the interval. An IntervalTrigger
        # alone first fires a full interval later, so a fresh deploy left
        # David's dashboard empty for up to poll_minutes with nothing to
        # explain why — and every redeploy restarted that wait. The few seconds
        # of delay keep it off the startup path, so a slow Gmail call can't
        # stall the health check Railway is waiting on.
        next_run_time=datetime.now(tz) + timedelta(seconds=POLL_ON_BOOT_DELAY_SECONDS),
    )

    scheduler.add_job(
        prepare_todays_post,
        CronTrigger(
            day_of_week=_cron_days(config),
            hour=config.posting.prepare_at.hour,
            minute=config.posting.prepare_at.minute,
            timezone=tz,
        ),
        args=[services, config],
        id="prepare_todays_post",
        name="Write David's post and text him for approval",
        replace_existing=True,
    )

    digest_at = config.alerts.daily_digest_at
    if digest_at is not None:
        scheduler.add_job(
            send_daily_digest,
            CronTrigger(hour=digest_at.hour, minute=digest_at.minute, timezone=tz),
            args=[services],
            id="send_daily_digest",
            name="Text David a summary of what's waiting",
            replace_existing=True,
        )

    scheduler.add_job(
        retry_unqueued_posts,
        IntervalTrigger(minutes=RETRY_QUEUE_MINUTES, timezone=tz),
        args=[services],
        id="retry_unqueued_posts",
        name="Retry scheduling approved posts",
        replace_existing=True,
    )

    scheduler.add_job(
        expire_stale_approvals,
        IntervalTrigger(hours=EXPIRE_APPROVALS_HOURS, timezone=tz),
        args=[services],
        id="expire_stale_approvals",
        name="Close out unanswered approval requests",
        replace_existing=True,
    )

    logger.info(
        "Checking David's email for LinkedIn notifications every %s minutes, "
        "starting %s seconds from now.",
        config.inbox.poll_minutes,
        POLL_ON_BOOT_DELAY_SECONDS,
    )
    logger.info(
        "Writing David's post and texting him for approval at %s on %s.",
        config.posting.prepare_at.strftime("%-I:%M %p"),
        ", ".join(day.capitalize() for day in config.posting.days),
    )
    if digest_at is not None:
        logger.info("Texting David a summary of what's waiting at %s.", digest_at.strftime("%-I:%M %p"))
    else:
        logger.info("Daily summary text is switched off.")
    quiet = config.alerts.quiet_hours
    logger.info(
        "Never texting David between %s and %s his time.",
        quiet.start.strftime("%-I:%M %p"),
        quiet.end.strftime("%-I:%M %p"),
    )
