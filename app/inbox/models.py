"""What a parsed LinkedIn notification looks like.

What: `NotificationType` (the five buckets) and `ParsedNotification` (one thing
that happened, ready to become a dashboard card).

Why: the parser's whole job is turning an unpredictable email into this small,
predictable shape. Everything downstream — drafting, storage, the dashboard —
depends only on this, so a LinkedIn format change stops at the parser.

How: one email can yield several notifications. LinkedIn sends digests ("You
have 3 new connections"), so parsing returns a list, never a single item.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class NotificationType(StrEnum):
    """What LinkedIn is telling David.

    MENTION is the broadest bucket: it means "someone engaged with David's own
    content and he should respond" — a comment on his post, or a true @mention.
    Both lead him to the same action, so they share a bucket.
    """

    MESSAGE_RECEIVED = "message_received"
    CONNECTION_REQUEST = "connection_request"
    CONNECTION_ACCEPTED = "connection_accepted"
    MENTION = "mention"
    OTHER = "other"


# The types worth putting in front of David. Everything else is noise, and
# noise reaching the dashboard is the failure mode this build guards against.
ACTIONABLE_TYPES: frozenset[NotificationType] = frozenset(
    {
        NotificationType.MESSAGE_RECEIVED,
        NotificationType.CONNECTION_REQUEST,
        NotificationType.CONNECTION_ACCEPTED,
        NotificationType.MENTION,
    }
)


@dataclass(frozen=True, slots=True)
class ParsedNotification:
    """One thing that happened on LinkedIn, lifted out of one email."""

    type: NotificationType

    # Where this came from. For a digest, the email's id plus an index, so
    # three people in one email produce three distinct rows in the database.
    source_ref: str

    # Diagnostics — what the developer reads when a format change breaks the parser.
    subject: str
    received_at: datetime | None = None
    template: str | None = None

    # Who this is about, as David would say their name.
    person_name: str | None = None

    # Their LinkedIn headline, when the email includes one. Useful context for
    # drafting a reply that sounds like it was written by someone paying
    # attention.
    headline: str | None = None

    # The words that were actually said — a comment, or an invitation note.
    # None when LinkedIn didn't include any (which is the norm for messages).
    preview: str | None = None

    # Where the [Open in LinkedIn] button sends David.
    linkedin_url: str | None = None

    # The person's profile, when it differs from the action link.
    profile_url: str | None = None

    @property
    def is_actionable(self) -> bool:
        """True when this belongs on David's Today screen."""
        return self.type in ACTIONABLE_TYPES
