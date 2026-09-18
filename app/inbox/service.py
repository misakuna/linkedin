"""The inbox poll: read mail, work out what happened, draft, store.

What: `InboxService.poll()` — one pass over David's LinkedIn notification
emails, producing draft rows for the dashboard.

Why: this is the seam where the read-only, draft-only nature of the system is
enforced in one place. Nothing here sends, accepts, replies, or posts. It
writes rows to SQLite and stops.

How: Gmail (or fixtures) -> parser -> drafter -> repository. Idempotent by
`source_ref`, so polling every ten minutes doesn't pile up duplicate cards for
the same email.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.llm import TextWriter
from app.config import AppConfig
from app.db import Draft, DraftRepository, DraftStatus
from app.inbox.drafting import ReplyDrafter, draft_kind_for
from app.inbox.gmail_client import MailReader, build_mail_reader
from app.inbox.models import ParsedNotification
from app.inbox.parser import parse_email
from app.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PollResult:
    """What one poll did — the numbers behind the summary log line."""

    emails_read: int
    actionable: int
    created: int
    drafted: int
    already_seen: int

    def summary(self) -> str:
        """One plain-language sentence for the log."""
        if self.created == 0:
            return f"Checked {self.emails_read} LinkedIn email(s). Nothing new for David."
        return (
            f"Checked {self.emails_read} LinkedIn email(s) and found {self.created} "
            f"new thing(s) for David, {self.drafted} with a suggested reply."
        )


class InboxService:
    """Reads LinkedIn notification emails and turns them into drafts."""

    def __init__(
        self,
        config: AppConfig,
        repository: DraftRepository,
        *,
        reader: MailReader | None = None,
        writer: TextWriter | None = None,
    ) -> None:
        self._config = config
        self._repository = repository
        self._reader = reader or build_mail_reader(config)
        self._drafter = ReplyDrafter(config, writer=writer)

    def poll(self) -> PollResult:
        """Do one pass over the inbox. Safe to run on a schedule."""
        inbox = self._config.inbox
        emails = self._reader.fetch(inbox.gmail_query, inbox.max_messages_per_poll)

        actionable = created = drafted = already_seen = calls = 0
        budget = self._config.drafting.max_drafts_per_poll

        for email in emails:
            for notification in parse_email(email.message, source_ref=email.message_id):
                if not notification.is_actionable:
                    continue
                actionable += 1

                existing = self._repository.get_by_source_ref(notification.source_ref)
                if existing is not None:
                    already_seen += 1
                    if self._backfill(existing, notification, budget):
                        drafted += 1
                        budget -= 1
                    continue

                # Space out the model calls. Everything above this line is
                # local work; _store is the only thing that leaves the machine,
                # and firing a dozen in a row is what the provider rate-limits.
                will_draft = budget > 0 and self._drafter.should_draft(notification)
                if will_draft and calls:
                    self._pace()

                draft = self._store(notification, write=will_draft)
                if draft is None:
                    continue
                created += 1
                if will_draft:
                    calls += 1
                    budget -= 1
                if draft.body:
                    drafted += 1

        result = PollResult(
            emails_read=len(emails),
            actionable=actionable,
            created=created,
            drafted=drafted,
            already_seen=already_seen,
        )
        logger.info(result.summary())
        return result

    def _pace(self) -> None:
        """Wait between drafting calls so a burst doesn't get rate-limited.

        Deliberately dumb. A token bucket would be tidier, but this runs in a
        background job every 15 minutes where a slow pass is free, and the
        failure it prevents is silent: a rejected draft looks identical to
        "LinkedIn told us nothing" on David's screen.
        """
        gap = self._config.drafting.seconds_between_calls
        if gap > 0:
            time.sleep(gap)

    def _backfill(
        self, existing: Draft, notification: ParsedNotification, budget: int
    ) -> bool:
        """Fill in a suggested reply that a card should have but doesn't.

        `create_if_new` skips any source_ref it has seen, so a card created
        while the model was rate-limited would stay blank forever — and on
        David's screen a blank card is indistinguishable from "LinkedIn told us
        nothing", which is a sentence we'd be telling him falsely.

        Cheap to do here: we re-read the same week of mail every poll, so the
        freshly parsed notification is already in hand, complete with the
        invitation note that was never stored on the row.
        """
        if budget <= 0 or existing.body or not existing.is_open:
            return False
        if not self._drafter.should_draft(notification):
            return False

        self._pace()
        body = self._drafter.draft(notification)
        if not body:
            return False

        self._repository.update_body(existing.id, body)
        logger.info(
            "Filled in the suggested reply for %s that an earlier run couldn't write.",
            existing.person_name or "someone",
        )
        return True

    def _store(self, notification: ParsedNotification, *, write: bool = True) -> Draft | None:
        """Draft a reply if we can, and save the row. Returns None on a race.

        `write=False` skips the model entirely — used once a poll has spent its
        drafting budget. The card is still created, so David sees the person;
        the suggested reply arrives on a later run.
        """
        body = self._drafter.draft(notification) if write else None

        return self._repository.create_if_new(
            source_ref=notification.source_ref,
            kind=draft_kind_for(notification),
            # An empty body is meaningful: the dashboard shows the card with
            # its [Open in LinkedIn] button and no suggested text.
            body=body or "",
            person_name=notification.person_name,
            headline=notification.headline,
            # What actually happened, kept for the dashboard's summary line —
            # `kind` only records how David will send his answer.
            notification_type=notification.type.value,
            linkedin_url=notification.linkedin_url,
            # Nothing reaches David for approval automatically. He sees these
            # on the dashboard, and everything leaves the system by his hand.
            status=DraftStatus.DRAFT,
        )
