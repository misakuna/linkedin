"""Turning database rows into the JSON the dashboard renders.

What: `Draft` rows in, plain-language dictionaries out.

Why: David reads sentences, not statuses. The database says
`status='pending_approval'`; the screen says "Waiting for your OK". Keeping
that translation here means the words David sees are in one file that can be
edited without touching any logic — and no jargon can leak into the UI by
accident, because the UI is only ever handed these dictionaries.

How: pure functions, no database access, no I/O. Easy to test, and impossible
for them to fail in a way that takes the dashboard down.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.db import Draft, DraftKind, DraftStatus
from app.inbox.models import NotificationType

# The summary line at the top of each card. Written as David would say it.
_SUMMARIES: dict[str, str] = {
    NotificationType.MESSAGE_RECEIVED.value: "{name} sent you a message",
    NotificationType.CONNECTION_REQUEST.value: "{name} wants to connect with you",
    NotificationType.CONNECTION_ACCEPTED.value: "{name} is now connected with you",
    NotificationType.MENTION.value: "{name} commented on your post",
}

# Why there's no suggested reply. Only ever shown when we genuinely couldn't
# write one — see the blind-reply note in inbox/drafting.py.
_NO_DRAFT_REASONS: dict[str, str] = {
    NotificationType.MESSAGE_RECEIVED.value: (
        "LinkedIn doesn't show us what they wrote, so have a read and reply in "
        "your own words."
    ),
    NotificationType.MENTION.value: (
        "We couldn't read their comment, so have a look and reply in your own words."
    ),
}

_DEFAULT_NO_DRAFT_REASON = (
    "We couldn't write a suggestion for this one, so have a look and reply in "
    "your own words."
)

_POST_STATUS_LABELS: dict[DraftStatus, str] = {
    DraftStatus.DRAFT: "Being written",
    DraftStatus.PENDING_APPROVAL: "Waiting for your OK",
    DraftStatus.APPROVED: "Scheduled",
    DraftStatus.DONE: "Posted",
    DraftStatus.REJECTED: "Skipped",
}


def humanize_age(moment: datetime, *, now: datetime | None = None) -> str:
    """How long ago something happened, the way a person would say it."""
    now = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    seconds = max(0, int((now - moment).total_seconds()))
    minutes, hours, days = seconds // 60, seconds // 3600, seconds // 86400

    if minutes < 1:
        return "just now"
    if minutes == 1:
        return "1 minute ago"
    if minutes < 60:
        return f"{minutes} minutes ago"
    if hours == 1:
        return "1 hour ago"
    if hours < 24:
        return f"{hours} hours ago"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    return moment.strftime("%-d %B")


def humanize_when(moment: datetime, tz: ZoneInfo, *, now: datetime | None = None) -> str:
    """When something is going out, in David's timezone and his words."""
    now = now or datetime.now(timezone.utc)
    local = moment.astimezone(tz)
    today = now.astimezone(tz).date()

    clock = local.strftime("%-I:%M %p")
    if local.date() == today:
        return f"today at {clock}"
    if (local.date() - today).days == 1:
        return f"tomorrow at {clock}"
    if 0 < (local.date() - today).days < 7:
        return f"{local.strftime('%A')} at {clock}"
    return f"{local.strftime('%A %-d %B')} at {clock}"


def item_to_json(draft: Draft, *, now: datetime | None = None) -> dict:
    """One reply or comment, as the Today and Messages screens want it."""
    name = draft.person_name or "Someone"
    summary_template = _SUMMARIES.get(draft.notification_type or "", "{name} needs a reply")
    body = draft.body.strip()

    return {
        "id": draft.id,
        "kind": draft.kind.value,
        "person_name": name,
        "summary": summary_template.format(name=name),
        "detail": draft.headline,
        "draft_text": body or None,
        "no_draft_reason": None
        if body
        else _NO_DRAFT_REASONS.get(draft.notification_type or "", _DEFAULT_NO_DRAFT_REASON),
        "linkedin_url": draft.linkedin_url,
        "waiting_since": humanize_age(draft.created_at, now=now),
        # DONE and REJECTED both mean David is finished with it; the card shows
        # dimmed with a "Done" pill either way.
        "handled": draft.status in (DraftStatus.DONE, DraftStatus.REJECTED),
    }


def post_to_json(draft: Draft, tz: ZoneInfo, *, now: datetime | None = None) -> dict:
    """One post, as the My Posts screen wants it."""
    return {
        "id": draft.id,
        "body": draft.body,
        "scheduled_for_human": humanize_when(draft.scheduled_for, tz, now=now)
        if draft.scheduled_for
        else "soon",
        "status": draft.status.value,
        "status_label": "Didn't finish sending"
        if draft.is_stuck_publishing
        else _POST_STATUS_LABELS.get(draft.status, "Waiting for your OK"),
        # A stuck post looks approved and behaves like nothing. Saying so is the
        # difference between David waiting for a post that will never go out and
        # David tapping the button to send it.
        "needs_another_try": draft.is_stuck_publishing,
    }


def is_item(draft: Draft) -> bool:
    """True for the things that belong on Today and Messages."""
    return draft.kind in (DraftKind.REPLY, DraftKind.COMMENT)
