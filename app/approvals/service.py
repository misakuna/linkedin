"""The text-message approval loop.

What: texts David a drafted post and interprets his reply.

Why: nothing gets published under David's name that he hasn't read and agreed
to. This module is where that guarantee is operationalised — it is the only
path from `pending_approval` to `approved`, and it needs a human to say yes.

How: `request()` sends the text. `handle_reply()` reads what came back and
moves the draft. `expire_stale()` closes out requests David never answered, so
a post from three weeks ago can't suddenly publish itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.approvals.sms import SmsSender, build_sms_sender, shorten_for_sms
from app.config import AppConfig
from app.db import Draft, DraftRepository, DraftStatus, InvalidTransitionError
from app.logging_setup import get_logger

logger = get_logger(__name__)

# What counts as yes and no in a reply. Deliberately forgiving about
# punctuation and case, deliberately strict about anything else: an
# unrecognised reply is left unanswered rather than guessed at.
#
# "STOP" is absent on purpose — carriers treat it as a network-level opt-out,
# so it never reaches us and must not be advertised as a way to decline.
YES_WORDS = frozenset({"yes", "y", "yep", "yeah", "ok", "okay", "approve", "approved", "send", "go"})
NO_WORDS = frozenset({"no", "n", "nope", "reject", "pass", "skip", "cancel", "don't", "dont"})


@dataclass(frozen=True, slots=True)
class ReplyOutcome:
    """What we did with an inbound text."""

    understood: bool
    draft: Draft | None = None
    approved: bool = False
    note: str = ""


class ApprovalService:
    """Texts David for approval and acts on what he says back."""

    def __init__(
        self,
        config: AppConfig,
        repository: DraftRepository,
        *,
        sender: SmsSender | None = None,
    ) -> None:
        self._config = config
        self._repository = repository
        self._sender = sender or build_sms_sender(config)

    # -- outbound ----------------------------------------------------------

    def request(self, draft: Draft) -> bool:
        """Text David a draft and ask him to approve it."""
        body = self._compose(draft)
        sent = self._sender.send(to=self._config.approvals.phone_number, body=body)
        if not sent:
            logger.warning(
                "Couldn't text David about post #%s. It's still waiting for him on "
                "the dashboard.",
                draft.id,
            )
        return sent

    def _compose(self, draft: Draft) -> str:
        """The text David actually receives.

        Written to be answerable without opening anything: what it is, when it
        would go out, the words themselves, and how to say yes or no.
        """
        when = (
            draft.scheduled_for.astimezone(self._config.client.tz).strftime("%A at %-I:%M %p")
            if draft.scheduled_for
            else "soon"
        )
        return (
            f"Here's your LinkedIn post for {when}:\n\n"
            f"{shorten_for_sms(draft.body)}\n\n"
            "Reply YES to post it, or NO to skip it. "
            f"Any changes, text {self._config.dashboard.support_contact}."
        )

    # -- inbound -----------------------------------------------------------

    def handle_reply(self, text: str, *, from_number: str | None = None) -> ReplyOutcome:
        """Interpret a text from David and move the waiting draft.

        Only ever touches the oldest post awaiting approval, so a stray "yes"
        can't approve something David wasn't looking at.
        """
        if from_number and not _same_number(from_number, self._config.approvals.phone_number):
            logger.warning("Ignored a text from a number that isn't David's.")
            return ReplyOutcome(understood=False, note="not David's number")

        answer = _read_answer(text)
        if answer is None:
            logger.info("Got a text from David I couldn't read as yes or no: %r", text.strip()[:80])
            return ReplyOutcome(understood=False, note="couldn't tell yes from no")

        waiting = self._oldest_waiting()
        if waiting is None:
            logger.info("David replied %s, but nothing is waiting on him.", answer)
            return ReplyOutcome(understood=True, note="nothing was waiting")

        if answer == "yes":
            draft = self._repository.transition(
                waiting.id, DraftStatus.APPROVED, note="David replied YES by text"
            )
            return ReplyOutcome(understood=True, draft=draft, approved=True)

        draft = self._repository.transition(
            waiting.id, DraftStatus.REJECTED, note="David replied NO by text"
        )
        return ReplyOutcome(understood=True, draft=draft, approved=False)

    def _oldest_waiting(self) -> Draft | None:
        waiting = self._repository.list_by_status([DraftStatus.PENDING_APPROVAL])
        return min(waiting, key=lambda d: d.id) if waiting else None

    # -- housekeeping ------------------------------------------------------

    def expire_stale(self, *, now: datetime | None = None) -> int:
        """Reject approval requests David never answered.

        Without this, a post drafted for a Tuesday three weeks ago would still
        be sitting in `pending_approval`, ready to publish the moment somebody
        pressed a button. Returns how many were closed out.
        """
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=self._config.approvals.expires_after_hours)

        expired = 0
        for draft in self._repository.list_by_status([DraftStatus.PENDING_APPROVAL]):
            if draft.updated_at > cutoff:
                continue
            try:
                self._repository.transition(
                    draft.id, DraftStatus.REJECTED, note="expired — David never replied"
                )
            except InvalidTransitionError:
                continue
            expired += 1

        if expired:
            logger.info(
                "Closed out %s post(s) David never got back to. Nothing was published.",
                expired,
            )
        return expired


def _read_answer(text: str) -> str | None:
    """Turn a text message into "yes", "no", or None if it's neither."""
    words = re.findall(r"[a-z']+", (text or "").lower())
    for word in words:
        if word in YES_WORDS:
            return "yes"
        if word in NO_WORDS:
            return "no"
    return None


def _same_number(a: str, b: str) -> bool:
    """Compare phone numbers ignoring formatting differences."""
    return re.sub(r"\D", "", a)[-10:] == re.sub(r"\D", "", b)[-10:]
