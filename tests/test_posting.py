"""Posting scheduler, approval loop, and publishing.

The rules under test are the ones that would embarrass the client if they
broke: a post can't publish without David saying yes, a restart can't move or
duplicate a scheduled post, and a partner outage delays rather than loses.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

from app.approvals.service import ApprovalService
from app.approvals.sms import MockSmsSender, shorten_for_sms
from app.config import AppConfig
from app.db import DraftKind, DraftRepository, DraftStatus
from app.posting.drafting import theme_for
from app.posting.publisher import MockPublisher
from app.posting.schedule import is_posting_day, next_posting_days, publish_time_for
from app.posting.service import PostingService, source_ref_for

# A Tuesday and a Thursday, matching the shipped config's posting days.
TUESDAY = date(2026, 8, 4)
THURSDAY = date(2026, 8, 6)
WEDNESDAY = date(2026, 8, 5)


class StubDrafter:
    """Stands in for the Claude-backed post drafter."""

    def __init__(self, text: str | None = "Had coffee with a client today.") -> None:
        self.text = text
        self.days: list[date] = []

    def draft(self, day: date) -> str | None:
        self.days.append(day)
        return self.text


@pytest.fixture
def sms(config: AppConfig) -> MockSmsSender:
    return MockSmsSender(config)


@pytest.fixture
def publisher(config: AppConfig) -> MockPublisher:
    return MockPublisher(config)


@pytest.fixture
def approvals(config: AppConfig, repository: DraftRepository, sms: MockSmsSender) -> ApprovalService:
    return ApprovalService(config, repository, sender=sms)


@pytest.fixture
def posting(
    config: AppConfig,
    repository: DraftRepository,
    approvals: ApprovalService,
    publisher: MockPublisher,
) -> PostingService:
    return PostingService(
        config, repository, approvals, drafter=StubDrafter(), publisher=publisher
    )


# -- picking the time ------------------------------------------------------


def test_posting_days_come_from_config(config: AppConfig) -> None:
    assert is_posting_day(TUESDAY, config)
    assert is_posting_day(THURSDAY, config)
    assert not is_posting_day(WEDNESDAY, config)


def test_publish_time_is_inside_the_window(config: AppConfig) -> None:
    when = publish_time_for(TUESDAY, config)
    assert when.date() == TUESDAY
    assert config.posting.window.start <= when.time() <= config.posting.window.end
    assert when.tzinfo is not None


def test_same_day_always_gives_the_same_time(config: AppConfig) -> None:
    """A restart must not move a post David has already been texted about."""
    assert publish_time_for(TUESDAY, config) == publish_time_for(TUESDAY, config)


def test_different_days_give_different_times(config: AppConfig) -> None:
    """Otherwise it isn't random, it's just a fixed time with extra steps."""
    times = {publish_time_for(TUESDAY + timedelta(days=7 * n), config).time() for n in range(8)}
    assert len(times) > 5


def test_time_is_stable_across_separate_processes(config: AppConfig) -> None:
    """The real test of the seeding: Python's hash() is salted per process,
    so a naive implementation passes in-process and breaks on redeploy."""
    script = (
        "from datetime import date;"
        "from app.config import load_config, PROJECT_ROOT;"
        "from app.posting.schedule import publish_time_for;"
        "c = load_config(PROJECT_ROOT / 'config.yaml');"
        "print(publish_time_for(date(2026, 8, 4), c).isoformat())"
    )
    runs = {
        subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, check=True
        ).stdout.strip()
        for _ in range(2)
    }
    assert len(runs) == 1
    assert runs.pop() == publish_time_for(TUESDAY, config).isoformat()


def test_next_posting_days_only_returns_posting_days(config: AppConfig) -> None:
    upcoming = next_posting_days(TUESDAY, config, count=4)
    assert len(upcoming) == 4
    assert all(is_posting_day(day, config) for day in upcoming)


def test_theme_rotates_but_is_fixed_per_day(config: AppConfig) -> None:
    assert theme_for(TUESDAY, config) == theme_for(TUESDAY, config)
    assert theme_for(TUESDAY, config) in config.posting.themes
    spread = {theme_for(TUESDAY + timedelta(days=n), config) for n in range(30)}
    assert len(spread) > 1


# -- preparing the post ----------------------------------------------------


def test_prepare_drafts_texts_david_and_waits(
    posting: PostingService, repository: DraftRepository, sms: MockSmsSender
) -> None:
    result = posting.prepare(TUESDAY)

    assert result.prepared
    assert result.draft.kind is DraftKind.POST
    assert result.draft.status is DraftStatus.PENDING_APPROVAL
    assert result.draft.scheduled_for == publish_time_for(TUESDAY, posting._config)
    assert len(sms.sent) == 1


def test_the_text_tells_david_what_to_do(
    posting: PostingService, sms: MockSmsSender, config: AppConfig
) -> None:
    posting.prepare(TUESDAY)
    _, body = sms.sent[0]

    assert "YES" in body and "NO" in body
    assert "Had coffee with a client today." in body
    assert config.dashboard.support_contact in body


def test_preparing_twice_does_not_double_post(
    posting: PostingService, repository: DraftRepository, sms: MockSmsSender
) -> None:
    first = posting.prepare(TUESDAY)
    second = posting.prepare(TUESDAY)

    assert second.draft.id == first.draft.id
    assert second.reason == "already prepared"
    assert len(sms.sent) == 1, "David must not be texted twice about one post"
    assert len(repository.list_by_status([DraftStatus.PENDING_APPROVAL])) == 1


def test_nothing_happens_on_a_non_posting_day(
    posting: PostingService, repository: DraftRepository, sms: MockSmsSender
) -> None:
    result = posting.prepare(WEDNESDAY)

    assert not result.prepared
    assert repository.list_open() == []
    assert sms.sent == []


def test_a_drafting_failure_leaves_nothing_pending(
    config: AppConfig, repository: DraftRepository, approvals: ApprovalService,
    publisher: MockPublisher, sms: MockSmsSender,
) -> None:
    """An empty post reaching David's phone is worse than no post."""
    service = PostingService(
        config, repository, approvals, drafter=StubDrafter(text=None), publisher=publisher
    )
    result = service.prepare(TUESDAY)

    assert not result.prepared
    assert repository.list_open() == []
    assert sms.sent == []


# -- approval --------------------------------------------------------------


def test_yes_by_text_schedules_the_post(
    posting: PostingService, repository: DraftRepository, publisher: MockPublisher
) -> None:
    draft = posting.prepare(TUESDAY).draft

    assert posting.handle_sms_reply("yes")

    assert repository.get(draft.id).status is DraftStatus.DONE
    assert len(publisher.queued) == 1
    assert publisher.queued[0][0] == draft.body


def test_no_by_text_rejects_and_publishes_nothing(
    posting: PostingService, repository: DraftRepository, publisher: MockPublisher
) -> None:
    draft = posting.prepare(TUESDAY).draft

    assert not posting.handle_sms_reply("No thanks")

    assert repository.get(draft.id).status is DraftStatus.REJECTED
    assert publisher.queued == []


@pytest.mark.parametrize("reply", ["YES", "yes please", "  ok  ", "Y", "approve", "go"])
def test_the_many_ways_david_might_say_yes(
    approvals: ApprovalService, posting: PostingService, reply: str
) -> None:
    posting.prepare(TUESDAY)
    assert approvals.handle_reply(reply).approved


@pytest.mark.parametrize("reply", ["no", "N", "nope", "skip it", "pass"])
def test_the_many_ways_david_might_say_no(
    approvals: ApprovalService, posting: PostingService, reply: str
) -> None:
    posting.prepare(TUESDAY)
    outcome = approvals.handle_reply(reply)
    assert outcome.understood and not outcome.approved


@pytest.mark.parametrize("reply", ["can you change the second line?", "", "hmm", "👍"])
def test_an_unclear_reply_changes_nothing(
    approvals: ApprovalService, posting: PostingService, repository: DraftRepository, reply: str
) -> None:
    """Guessing at an ambiguous reply could publish something David was editing."""
    draft = posting.prepare(TUESDAY).draft
    outcome = approvals.handle_reply(reply)

    assert not outcome.understood
    assert repository.get(draft.id).status is DraftStatus.PENDING_APPROVAL


def test_a_text_from_someone_else_is_ignored(
    approvals: ApprovalService, posting: PostingService, repository: DraftRepository
) -> None:
    draft = posting.prepare(TUESDAY).draft
    outcome = approvals.handle_reply("yes", from_number="+15559999999")

    assert not outcome.understood
    assert repository.get(draft.id).status is DraftStatus.PENDING_APPROVAL


def test_davids_number_matches_despite_formatting(
    approvals: ApprovalService, posting: PostingService, config: AppConfig
) -> None:
    """Twilio reports the sender however the carrier formats it.

    Derived from config rather than hardcoded: this used to assert against the
    placeholder number and broke the moment David's real one was filled in.
    """
    posting.prepare(TUESDAY)
    digits = config.approvals.phone_number.lstrip("+")[-10:]
    pretty = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    assert approvals.handle_reply("yes", from_number=pretty).approved


def test_yes_with_nothing_waiting_is_harmless(approvals: ApprovalService) -> None:
    outcome = approvals.handle_reply("yes")
    assert outcome.understood and outcome.draft is None


# -- publishing ------------------------------------------------------------


def test_publishing_is_refused_without_approval(
    posting: PostingService, repository: DraftRepository, publisher: MockPublisher
) -> None:
    """The core compliance guarantee, checked at the publishing seam too."""
    draft = posting.prepare(TUESDAY).draft
    assert repository.get(draft.id).status is DraftStatus.PENDING_APPROVAL

    assert not posting.publish(draft.id)
    assert publisher.queued == []


def test_a_publisher_outage_delays_rather_than_loses(
    config: AppConfig, repository: DraftRepository, approvals: ApprovalService
) -> None:
    class BrokenPublisher:
        def queue(self, *, text, publish_at):
            return None

    service = PostingService(
        config, repository, approvals, drafter=StubDrafter(), publisher=BrokenPublisher()
    )
    draft = service.prepare(TUESDAY).draft
    assert not service.handle_sms_reply("yes")

    # Still approved, not lost, and not marked done.
    assert repository.get(draft.id).status is DraftStatus.APPROVED


def test_the_retry_sweep_picks_up_a_stuck_post(
    config: AppConfig, repository: DraftRepository, approvals: ApprovalService,
    publisher: MockPublisher,
) -> None:
    class BrokenPublisher:
        def queue(self, *, text, publish_at):
            return None

    broken = PostingService(
        config, repository, approvals, drafter=StubDrafter(), publisher=BrokenPublisher()
    )
    draft = broken.prepare(TUESDAY).draft
    broken.handle_sms_reply("yes")

    recovered = PostingService(
        config, repository, approvals, drafter=StubDrafter(), publisher=publisher
    )
    assert recovered.retry_unqueued() == 1
    assert repository.get(draft.id).status is DraftStatus.DONE


def test_late_approval_still_goes_out(config: AppConfig, publisher: MockPublisher) -> None:
    """David approves at 4pm a post planned for 9am — publish, don't drop it."""
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    receipt = publisher.queue(text="Morning thought.", publish_at=yesterday)

    assert receipt is not None
    assert receipt.scheduled_for > datetime.now(timezone.utc)


def test_dashboard_approval_reaches_the_same_place(
    posting: PostingService, repository: DraftRepository, publisher: MockPublisher
) -> None:
    draft = posting.prepare(TUESDAY).draft
    assert posting.approve_and_publish(draft.id)
    assert repository.get(draft.id).status is DraftStatus.DONE
    assert len(publisher.queued) == 1


# -- expiry ----------------------------------------------------------------


def test_unanswered_requests_expire_instead_of_lingering(
    posting: PostingService, approvals: ApprovalService, repository: DraftRepository,
    config: AppConfig,
) -> None:
    """A three-week-old post must not be one button press from publishing."""
    draft = posting.prepare(TUESDAY).draft
    later = datetime.now(timezone.utc) + timedelta(
        hours=config.approvals.expires_after_hours + 1
    )

    assert approvals.expire_stale(now=later) == 1
    assert repository.get(draft.id).status is DraftStatus.REJECTED


def test_a_fresh_request_is_left_alone(
    posting: PostingService, approvals: ApprovalService, repository: DraftRepository
) -> None:
    draft = posting.prepare(TUESDAY).draft
    assert approvals.expire_stale() == 0
    assert repository.get(draft.id).status is DraftStatus.PENDING_APPROVAL


# -- the full audit trail --------------------------------------------------


def test_every_step_is_recorded(
    posting: PostingService, repository: DraftRepository
) -> None:
    """Weeks later, "what happened to that post?" has an answer."""
    draft = posting.prepare(TUESDAY).draft
    posting.handle_sms_reply("yes")

    history = [(e.to_status, e.note) for e in repository.events(draft.id)]
    assert [status for status, _ in history] == [
        DraftStatus.DRAFT,
        DraftStatus.PENDING_APPROVAL,
        DraftStatus.APPROVED,
        DraftStatus.DONE,
    ]
    assert "David replied YES by text" in history[2][1]
    assert "queued for" in history[3][1]


def test_source_ref_is_one_per_day() -> None:
    assert source_ref_for(TUESDAY) != source_ref_for(THURSDAY)


def test_sms_shortening_keeps_texts_readable() -> None:
    assert shorten_for_sms("a\n\n  b") == "a b"
    assert len(shorten_for_sms("x" * 900)) <= 320
    assert shorten_for_sms("x" * 900).endswith("…")
