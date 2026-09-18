"""The posting pipeline.

What: draft → pending_approval → (David says yes) → approved → queued → done.

Why: this is the one automated path to LinkedIn in the whole system, so every
step of it is written to fail closed. Anything that goes wrong leaves the post
sitting in a status that needs a human, rather than guessing and publishing.

How: three entry points, all safe to call repeatedly.

  `prepare(day)`      writes the draft and texts David. Idempotent by date.
  `publish(draft_id)` hands an approved post to the publisher.
  `retry_unqueued()`  picks up approvals whose queueing failed.

The status transitions are enforced by the repository, so a bug here cannot
skip David's approval — it can only fail to reach it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from app.approvals.service import ApprovalService
from app.config import AppConfig
from app.db import Draft, DraftKind, DraftRepository, DraftStatus, InvalidTransitionError
from app.logging_setup import get_logger
from app.posting.drafting import PostDrafter
from app.posting.publisher import NotSentError, Publisher, build_publisher
from app.posting.schedule import is_posting_day, publish_time_for

logger = get_logger(__name__)


def source_ref_for(day: date) -> str:
    """One post per posting day, enforced by the unique index on source_ref."""
    return f"post:{day.isoformat()}"


@dataclass(frozen=True, slots=True)
class PrepareResult:
    """What `prepare()` did, for the log and for tests."""

    draft: Draft | None
    reason: str

    @property
    def prepared(self) -> bool:
        return self.draft is not None


class PostingService:
    """Drafts, gets approval for, and schedules David's posts."""

    def __init__(
        self,
        config: AppConfig,
        repository: DraftRepository,
        approvals: ApprovalService,
        *,
        drafter: PostDrafter | None = None,
        publisher: Publisher | None = None,
    ) -> None:
        self._config = config
        self._repository = repository
        self._approvals = approvals
        self._drafter = drafter or PostDrafter(config)
        self._publisher = publisher or build_publisher(config)

    # -- step one: draft and ask -------------------------------------------

    def prepare(self, day: date) -> PrepareResult:
        """Draft the post for a date and text David for approval.

        Safe to run twice. The publish time is derived from the date, and the
        draft is keyed on the date, so a restart or a duplicate job run finds
        the existing post rather than creating a second one.
        """
        if not is_posting_day(day, self._config):
            return PrepareResult(None, f"{day} isn't one of David's posting days")

        existing = self._repository.get_by_source_ref(source_ref_for(day))
        if existing is not None:
            logger.info("Today's post is already taken care of (#%s).", existing.id)
            return PrepareResult(existing, "already prepared")

        publish_at = publish_time_for(day, self._config)
        body = self._drafter.draft(day)
        if not body:
            logger.error(
                "Couldn't write David's post for %s, so there's nothing to approve. "
                "Nothing was published.",
                day,
            )
            return PrepareResult(None, "drafting failed")

        draft = self._repository.create(
            kind=DraftKind.POST,
            body=body,
            source_ref=source_ref_for(day),
            scheduled_for=publish_at,
        )
        draft = self._repository.transition(
            draft.id,
            DraftStatus.PENDING_APPROVAL,
            note=f"planned for {publish_at.strftime('%-I:%M %p')}",
        )
        self._approvals.request(draft)
        return PrepareResult(draft, "prepared")

    # -- step two: publish what David approved ------------------------------

    def publish(self, draft_id: int) -> bool:
        """Hand an approved post to the publisher and mark it done.

        Only ever acts on a post already in `approved`, so this cannot be the
        thing that skips David.
        """
        draft = self._repository.get(draft_id)

        if draft.kind is not DraftKind.POST:
            logger.error("Refused to publish #%s — it isn't a post.", draft_id)
            return False
        if draft.status is not DraftStatus.APPROVED:
            logger.error(
                "Refused to publish #%s — David hasn't approved it (it's %s).",
                draft_id,
                draft.status.value,
            )
            return False

        # Claim it before calling out. The status check above is necessary but
        # not sufficient: the publisher call below can take 30s, and the retry
        # sweep, the SMS webhook and the dashboard button can all be inside this
        # method for the same post at once. Whoever wins the claim publishes;
        # everyone else stops here.
        if not self._repository.claim_for_publishing(draft_id):
            logger.info(
                "Post #%s is already being scheduled by another caller. Leaving it alone.",
                draft_id,
            )
            return False

        publish_at = draft.scheduled_for or datetime.now(timezone.utc)
        try:
            receipt = self._publisher.queue(text=draft.body, publish_at=publish_at)
        except NotSentError:
            # Nothing reached the publisher, so a retry cannot duplicate
            # anything. Hand the claim back and let the sweep try again.
            self._repository.release_publish_claim(draft_id)
            logger.warning(
                "Post #%s didn't reach the publisher. It stays approved and will "
                "be retried.",
                draft_id,
            )
            return False
        except Exception:
            # Anything else leaves us unable to say whether the post went out,
            # so the claim stays held rather than risking a second copy.
            logger.exception(
                "Scheduling post #%s failed in a way we can't interpret. It stays "
                "on David's list rather than being retried automatically.",
                draft_id,
            )
            return False
        if receipt is None:
            # The claim stays held on purpose. `queue()` returns None for both
            # "never reached Buffer" and "Buffer answered but gave us no id",
            # and from here those look identical — so retrying could post a
            # second copy. The post stays approved and visible on the dashboard
            # for David to handle, which beats a duplicate under his name.
            logger.warning(
                "Post #%s is approved but couldn't be scheduled, and is not safe to "
                "retry automatically — it may or may not have reached the publisher. "
                "It stays on David's list.",
                draft_id,
            )
            return False

        try:
            self._repository.transition(draft_id, DraftStatus.DONE, note=receipt.describe())
        except InvalidTransitionError:
            logger.exception("Post #%s was scheduled but its status couldn't be updated.", draft_id)
            return False

        # The claim did its job. Clearing it keeps `publish_claimed_at` meaning
        # exactly one thing — "this post is stuck" — so the dashboard and the
        # boot summary can trust it. `done` is terminal, so nothing can be
        # republished by this.
        self._repository.release_publish_claim(draft_id)
        return True

    def handle_sms_reply(self, text: str, *, from_number: str | None = None) -> bool:
        """Act on a text from David: approve and schedule, or reject.

        This is the whole SMS round trip in one call — the thing an inbound-SMS
        webhook or the dashboard's "he texted back" path would invoke. Returns
        True only when a post ended up scheduled.
        """
        outcome = self._approvals.handle_reply(text, from_number=from_number)
        if not (outcome.approved and outcome.draft):
            return False
        return self.publish(outcome.draft.id)

    def approve_and_publish(self, draft_id: int) -> bool:
        """Move a post from awaiting-approval to scheduled, in one step.

        Used by the dashboard's approve button. The SMS path arrives at the
        same place via `ApprovalService.handle_reply` plus `publish`.
        """
        self._repository.transition(
            draft_id, DraftStatus.APPROVED, note="David approved on the dashboard"
        )
        return self.publish(draft_id)

    # -- step three: don't lose anything -----------------------------------

    def retry_unqueued(self) -> int:
        """Re-attempt posts David approved that never reached the publisher.

        Buffer being down at the moment David replies YES must delay the post,
        not lose it. Returns how many got scheduled this time.
        """
        stuck = [
            draft
            for draft in self._repository.list_by_status([DraftStatus.APPROVED])
            if draft.kind is DraftKind.POST
        ]
        if not stuck:
            return 0

        logger.info("Retrying %s approved post(s) that weren't scheduled yet.", len(stuck))
        return sum(1 for draft in stuck if self.publish(draft.id))
