"""Draft records and the status machine that governs them.

What: the `Draft` row shape, its `kind`/`status` enums, and the map of legal
status transitions.

Why: the lifecycle is fixed as draft -> pending_approval -> approved ->
done (or rejected). Encoding it as data rather than scattered `if` statements
means an illegal jump (e.g. straight from draft to done, skipping David's
approval) is impossible rather than merely unlikely.

How: `ALLOWED_TRANSITIONS` is checked by `DraftRepository.transition()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class DraftKind(StrEnum):
    """What sort of text this is, which decides how it reaches LinkedIn."""

    POST = "post"        # published by Buffer after approval
    REPLY = "reply"      # copied by David into a LinkedIn message
    COMMENT = "comment"  # copied by David under someone's post


class DraftStatus(StrEnum):
    """Where a draft sits in the approval lifecycle."""

    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    DONE = "done"
    REJECTED = "rejected"


# Posts are the only thing that reaches LinkedIn without David's hand on it, so
# their lifecycle is the strict one: there is NO path from DRAFT to DONE.
# Nothing publishes without passing through approval.
POST_TRANSITIONS: dict[DraftStatus, frozenset[DraftStatus]] = {
    # Written, waiting to be sent for approval (or dropped).
    DraftStatus.DRAFT: frozenset({DraftStatus.PENDING_APPROVAL, DraftStatus.REJECTED}),
    # Sitting in David's texts / dashboard. Back to DRAFT if he asks for a rewrite.
    DraftStatus.PENDING_APPROVAL: frozenset(
        {DraftStatus.APPROVED, DraftStatus.REJECTED, DraftStatus.DRAFT}
    ),
    # David said yes. DONE once the publisher has it queued.
    DraftStatus.APPROVED: frozenset({DraftStatus.DONE, DraftStatus.REJECTED}),
    # Terminal.
    DraftStatus.DONE: frozenset(),
    DraftStatus.REJECTED: frozenset(),
}

# Replies and comments are copied by David and pasted into LinkedIn by his own
# hand. There is no approval step to gate, because he IS the sender — tapping
# "Already handled this" is him telling us he's dealt with it. So DRAFT may go
# straight to DONE here, and only here.
ALLOWED_TRANSITIONS: dict[DraftStatus, frozenset[DraftStatus]] = {
    **POST_TRANSITIONS,
    DraftStatus.DRAFT: frozenset(
        {DraftStatus.PENDING_APPROVAL, DraftStatus.REJECTED, DraftStatus.DONE}
    ),
}


def transitions_for(kind: "DraftKind") -> dict[DraftStatus, frozenset[DraftStatus]]:
    """The legal moves for a given kind of draft.

    Split by kind because the compliance guarantee only concerns posts: a post
    reaches LinkedIn automatically once approved, so it may never skip David.
    A reply cannot reach anyone except by David copying it himself.
    """
    return POST_TRANSITIONS if kind is DraftKind.POST else ALLOWED_TRANSITIONS

# Statuses that still want something from David — drives the Today screen.
OPEN_STATUSES: frozenset[DraftStatus] = frozenset(
    {DraftStatus.DRAFT, DraftStatus.PENDING_APPROVAL, DraftStatus.APPROVED}
)


class InvalidTransitionError(ValueError):
    """Attempted a status change the lifecycle does not allow."""

    def __init__(
        self,
        current: DraftStatus,
        requested: DraftStatus,
        kind: "DraftKind | None" = None,
    ) -> None:
        table = transitions_for(kind) if kind else ALLOWED_TRANSITIONS
        allowed = sorted(s.value for s in table[current])
        subject = f"{kind.value} " if kind else "draft "
        super().__init__(
            f"cannot move a {subject}from {current.value!r} to {requested.value!r} "
            f"(allowed from {current.value!r}: {allowed or 'nothing — it is final'})"
        )
        self.current = current
        self.requested = requested
        self.kind = kind


@dataclass(frozen=True, slots=True)
class Draft:
    """One piece of text the assistant wrote for David."""

    id: int
    kind: DraftKind
    status: DraftStatus
    body: str
    created_at: datetime
    updated_at: datetime
    person_name: str | None = None
    headline: str | None = None
    notification_type: str | None = None
    linkedin_url: str | None = None
    source_ref: str | None = None
    scheduled_for: datetime | None = None

    @property
    def is_open(self) -> bool:
        """True while this draft is still waiting on David or on a job."""
        return self.status in OPEN_STATUSES


@dataclass(frozen=True, slots=True)
class DraftEvent:
    """One recorded status change, for the audit trail and the logs."""

    id: int
    draft_id: int
    from_status: DraftStatus | None
    to_status: DraftStatus
    note: str | None
    created_at: datetime
