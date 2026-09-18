"""Smoke tests: the app boots, the config validates, and drafts obey the rules.

These are the tests that catch a broken deploy before Railway does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import AppConfig, ConfigError, load_config
from app.db import (
    Draft,
    DraftKind,
    DraftRepository,
    DraftStatus,
    InvalidTransitionError,
)


# -- config ----------------------------------------------------------------


def test_shipped_config_is_valid(config: AppConfig) -> None:
    """config.yaml as committed must load and validate."""
    assert config.client.timezone == "America/Los_Angeles"
    assert config.posting.days, "at least one posting day is required"
    assert config.posting.window.start < config.posting.window.end
    assert config.voice.notes.strip(), "drafts are only as good as the voice notes"


def test_going_live_is_visible_in_the_config(config: AppConfig) -> None:
    """This config is David's live deployment, not a safe template any more.

    It used to assert everything was mocked, which is what a fresh clone wants.
    That stopped being true the day Gmail and Gemini were switched on, and the
    honest thing is to say so rather than keep a green test that describes an
    earlier version of the project.

    What actually protects a fresh clone is that no secret is committed: with
    gmail live and no .env, the first poll raises MissingSecretError, logs it
    in plain language, and returns no email. Loud, and nothing leaves.
    """
    assert not config.all_mocked, "expected a live deployment config"
    # Whatever is live must at least be fully specified.
    if config.drafting.provider != "mock":
        assert config.drafting.model
    if config.chat.provider != "mock":
        assert config.chat.model


def test_no_secret_is_committed(config: AppConfig) -> None:
    """The real guard on a fresh clone, now that config.yaml is live.

    .env.example is committed and documents every key. If a value ever gets
    filled in there — the easiest mistake to make while writing setup notes —
    a clone would ship with working credentials.
    """
    from app.config import PROJECT_ROOT

    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    for line in example.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        # DASHBOARD_PIN ships as the documented 0000 placeholder on purpose.
        if key.strip() == "DASHBOARD_PIN":
            continue
        assert not value, f"{key.strip()} has a value in .env.example"


def test_the_suite_cannot_see_real_credentials() -> None:
    """Tests must not depend on — or be able to spend — the developer's .env.

    app/env_secrets.py calls load_dotenv() at import, so without the isolation
    in conftest the suite reads whatever is configured locally. That already
    bit once: every login test broke the day a real DASHBOARD_PIN was set,
    because they type "0000".

    The money side matters more. A test that builds a live client by hand would
    otherwise spend against a real account, and it would pass, so nobody would
    look.
    """
    import os

    for name in (
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GMAIL_REFRESH_TOKEN",
        "GMAIL_CLIENT_SECRET",
        "SMS_AUTH_TOKEN",
        "BUFFER_TOKEN",
    ):
        assert not os.environ.get(name), f"{name} leaked from .env into the tests"

    assert os.environ.get("DASHBOARD_PIN") == "0000"


def test_the_inbox_query_matches_a_label_not_a_sender(config: AppConfig) -> None:
    """David's LinkedIn mail is forwarded to an alias on the developer's mailbox.

    That mailbox receives its own LinkedIn notifications too, and a sender-based
    query cannot tell the two apart — his contacts would surface on David's
    dashboard as people waiting on a reply, silently and with no error. Only
    mail delivered to the alias carries the label, which is what makes it
    unambiguous. Reverting this to "from:(linkedin.com)" must fail loudly.
    """
    query = config.inbox.gmail_query
    assert "label:davids-linkedin" in query
    assert "from:" not in query, "a sender query mixes the developer's LinkedIn mail into David's"


def test_bad_posting_window_is_rejected(tmp_path: Path) -> None:
    """A window that ends before it starts fails at boot, not at post time."""
    broken = _config_text().replace('start: "08:00"', 'start: "20:00"')
    path = tmp_path / "config.yaml"
    path.write_text(broken, encoding="utf-8")

    with pytest.raises(ConfigError, match="must be before"):
        load_config(path)


def test_bad_phone_number_is_rejected(tmp_path: Path, config: AppConfig) -> None:
    """Approvals go nowhere if the number isn't in E.164 form.

    The real number is read from config rather than hardcoded — the literal
    placeholder was here once and silently stopped matching, which turned this
    into a test that passed without asserting anything.
    """
    broken = _config_text().replace(f'"{config.approvals.phone_number}"', '"555-0100"')
    assert '"555-0100"' in broken, "the phone number in config.yaml moved; fix this test"
    path = tmp_path / "config.yaml"
    path.write_text(broken, encoding="utf-8")

    with pytest.raises(ConfigError, match="E.164"):
        load_config(path)


def test_unknown_config_key_is_rejected(tmp_path: Path) -> None:
    """A typo'd key must fail loudly rather than being silently ignored."""
    path = tmp_path / "config.yaml"
    path.write_text(_config_text() + "\nposting_days: [monday]\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(path)


def _config_text() -> str:
    from app.config import PROJECT_ROOT

    return (PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8")


# -- app boot --------------------------------------------------------------


def test_app_boots_and_is_healthy(app_client) -> None:
    """Startup runs end to end: config, database, scheduler."""
    response = app_client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_home_page_speaks_plain_english(app_client) -> None:
    """No jargon, no stack traces — and it tells David who to text."""
    response = app_client.get("/")
    assert response.status_code == 200
    assert "Text support" in response.text or "text support" in response.text


def test_scheduler_is_running_with_the_expected_jobs(app_client) -> None:
    """The full answer to 'what does this do while nobody is watching?'"""
    from app.main import app

    assert app.state.scheduler.running
    assert {job.id for job in app.state.scheduler.get_jobs()} == {
        "poll_inbox",
        "prepare_todays_post",
        "retry_unqueued_posts",
        "expire_stale_approvals",
        "send_daily_digest",
    }


def test_the_posting_job_only_runs_on_davids_posting_days(app_client) -> None:
    from app.main import app

    job = app.state.scheduler.get_job("prepare_todays_post")
    days = str(job.trigger)
    for weekday in app.state.config.posting.days:
        assert weekday[:3] in days


# -- draft lifecycle -------------------------------------------------------


def test_draft_moves_through_the_full_lifecycle(repository: DraftRepository) -> None:
    draft = repository.create(kind=DraftKind.POST, body="A short post about Tuesday.")
    assert draft.status is DraftStatus.DRAFT

    draft = repository.transition(draft.id, DraftStatus.PENDING_APPROVAL)
    draft = repository.transition(draft.id, DraftStatus.APPROVED, note="replied YES")
    draft = repository.transition(draft.id, DraftStatus.DONE, note="queued in Buffer")

    assert draft.status is DraftStatus.DONE
    assert not draft.is_open

    history = [event.to_status for event in repository.events(draft.id)]
    assert history == [
        DraftStatus.DRAFT,
        DraftStatus.PENDING_APPROVAL,
        DraftStatus.APPROVED,
        DraftStatus.DONE,
    ]


def test_nothing_can_skip_davids_approval(repository: DraftRepository) -> None:
    """The core compliance guarantee, enforced in the data layer."""
    draft = repository.create(kind=DraftKind.POST, body="Straight to LinkedIn?")

    with pytest.raises(InvalidTransitionError):
        repository.transition(draft.id, DraftStatus.DONE)

    with pytest.raises(InvalidTransitionError):
        repository.transition(draft.id, DraftStatus.APPROVED)

    assert repository.get(draft.id).status is DraftStatus.DRAFT


def test_finished_drafts_are_final(repository: DraftRepository) -> None:
    draft = repository.create(kind=DraftKind.REPLY, body="Good to hear from you, Karen.")
    repository.transition(draft.id, DraftStatus.PENDING_APPROVAL)
    repository.transition(draft.id, DraftStatus.APPROVED)
    repository.transition(draft.id, DraftStatus.DONE)

    with pytest.raises(InvalidTransitionError):
        repository.transition(draft.id, DraftStatus.PENDING_APPROVAL)


def test_same_email_never_creates_two_drafts(repository: DraftRepository) -> None:
    """Re-polling Gmail must not double up David's work."""
    first = repository.create_if_new(
        source_ref="gmail-msg-abc123",
        kind=DraftKind.REPLY,
        body="Thanks for reaching out.",
        person_name="Karen Mills",
    )
    second = repository.create_if_new(
        source_ref="gmail-msg-abc123",
        kind=DraftKind.REPLY,
        body="Thanks for reaching out.",
        person_name="Karen Mills",
    )

    assert isinstance(first, Draft)
    assert second is None
    assert len(repository.list_open()) == 1


def test_list_open_shows_only_unfinished_work(repository: DraftRepository) -> None:
    waiting = repository.create(kind=DraftKind.POST, body="Still going.")
    finished = repository.create(kind=DraftKind.POST, body="All done.")
    repository.transition(finished.id, DraftStatus.REJECTED, note="David passed")

    open_ids = [draft.id for draft in repository.list_open()]
    assert open_ids == [waiting.id]
