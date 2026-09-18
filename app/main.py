"""FastAPI entrypoint.

What: boots the service — config, logging, database, scheduler — and serves
both the JSON API and (from STEP 4) David's server-rendered dashboard.

Why: one long-running process holds the scheduler and the SQLite file, which is
the whole reason this deploys to Railway/Render rather than a serverless host.

How: a lifespan handler does startup and shutdown in order. Boot deliberately
logs a short plain-language status block, because that block is the first thing
the developer reads when a deploy misbehaves.

Run locally:  uvicorn app.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app import __version__, env_secrets
from app.config import AppConfig, ConfigError, get_config
from app.approvals.webhook import router as sms_router
from app.dashboard.auth import LoginGate
from app.dashboard.routes import load_dashboard_html, router as dashboard_router
from app.db import DraftRepository, DraftStatus, init_db
from app.jobs import build_services, register_jobs
from app.logging_setup import get_logger, setup_logging
from app.scheduler import create_scheduler, shutdown_scheduler, start_scheduler

logger = get_logger(__name__)


def _log_boot_summary(config: AppConfig) -> None:
    """Say what this process is configured to do, in sentences.

    Every line here answers a question the developer would otherwise have to SSH in to
    answer: is it live or mocked, when does it post, is the PIN still the
    default, are the open decisions still open?
    """
    logger.info("David's LinkedIn Assistant v%s starting up.", __version__)
    logger.info(
        "Client: %s (times shown in %s).", config.client.name, config.client.timezone
    )
    logger.info(
        "Posting days: %s, between %s and %s.",
        ", ".join(day.capitalize() for day in config.posting.days),
        config.posting.window.start.strftime("%-I:%M %p"),
        config.posting.window.end.strftime("%-I:%M %p"),
    )

    modes = config.integrations
    logger.info(
        "Connections — Gmail: %s, Buffer: %s, Texting: %s.",
        modes.gmail,
        modes.buffer,
        modes.sms,
    )
    logger.info(
        "AI — drafting on %s (%s), chat on %s (%s).",
        config.drafting.provider,
        config.drafting.model,
        config.chat.provider,
        config.chat.model,
    )
    if config.all_mocked:
        logger.info(
            "Everything is mocked, so nothing will leave this server. "
            "Safe for local testing."
        )

    if env_secrets.dashboard_pin_is_default():
        logger.warning(
            "The dashboard PIN is still the default. Set DASHBOARD_PIN before "
            "David gets the link."
        )

    if env_secrets.session_secret_is_default():
        logger.warning(
            "The login cookie is signed with the public development key. Set "
            "SESSION_SECRET before David gets the link, or his session can be forged."
        )

    if not config.open_decisions_resolved:
        logger.warning(
            "Heads up: posting days, the time window, and David's approval phone "
            "number are still placeholders from config.yaml. Confirm them with "
            "David, then set open_decisions_resolved: true."
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start up: config -> database -> scheduler. Shut down in reverse."""
    setup_logging()

    try:
        config = get_config()
    except ConfigError as exc:
        # This is the most likely bad-deploy failure, so make it loud and
        # readable rather than a Pydantic traceback.
        logger.error("Could not start — the config file has a problem:\n%s", exc)
        raise

    db_path = config.database_path()
    init_db(db_path)
    repository = DraftRepository(db_path)

    services = build_services(config, repository)

    scheduler = create_scheduler(config)
    register_jobs(scheduler, config, services)
    start_scheduler(scheduler)

    app.state.config = config
    app.state.repository = repository
    app.state.services = services
    app.state.scheduler = scheduler
    app.state.login_gate = LoginGate(config)
    # Read once at boot: the page is static, and re-reading it per request
    # would put a disk error between David and his dashboard.
    app.state.dashboard_html = load_dashboard_html(config.dashboard.resolved_html_path())

    _log_boot_summary(config)

    waiting = repository.count_by_status().get(DraftStatus.PENDING_APPROVAL, 0)
    logger.info("Ready. %s item(s) are waiting on David right now.", waiting)

    # Nothing moves these on its own, so an operator reading the log needs to
    # know they exist. Silence here would mean a post sitting unpublished for
    # as long as nobody happens to look at the dashboard.
    stuck = repository.list_stuck_publishing()
    if stuck:
        logger.warning(
            "%s post(s) stopped partway through being sent and are waiting on a "
            "decision: %s. David can tap 'Yes, post it' again to send one, or skip "
            "it. Check the partner first — it may already have gone out.",
            len(stuck),
            ", ".join(f"#{draft.id}" for draft in stuck),
        )

    try:
        yield
    finally:
        shutdown_scheduler(scheduler)
        logger.info("Shut down cleanly.")


app = FastAPI(
    title="David's LinkedIn Assistant",
    version=__version__,
    lifespan=lifespan,
)


@app.get("/healthz")
def healthz() -> dict[str, object]:
    """Liveness probe for Railway/Render. Not user-facing."""
    return {"ok": True, "version": __version__}


app.include_router(dashboard_router)
# The only unauthenticated write path in the app. It is guarded by Twilio's
# request signature instead of a session cookie — see app/approvals/webhook.py.
app.include_router(sms_router)
