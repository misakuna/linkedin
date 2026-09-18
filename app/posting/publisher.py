"""Queueing an approved post for publication.

What: the interface for handing approved post text to a scheduling partner,
plus a Buffer implementation and an offline mock.

Why: this is **the only place in the entire system where anything reaches
LinkedIn without David's hand on it**, and it is allowed only because it goes
through an official LinkedIn API partner, server-to-server, after David has
approved the exact text. Nothing here talks to
linkedin.com. It talks to Buffer, and Buffer talks to LinkedIn's partner API.

How: a `Publisher` protocol with two implementations, selected by
`integrations.buffer` in config.yaml. Everything upstream — the scheduler, the
approval flow, the dashboard — is written against the protocol, so the choice
of partner is a swap of one class.

OPEN DECISION: Buffer versus another official partner is not
settled, and the request shape below is written from Buffer's published v1 API
but has never been run against a live account. Treat `BufferPublisher` as
unverified until someone runs it with real credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.config import AppConfig
from app.logging_setup import get_logger

logger = get_logger(__name__)

BUFFER_API_ROOT = "https://api.bufferapp.com/1"

# Buffer will reject a scheduled_at in the past. If an approval arrives after
# the slot has already gone by, we publish within this grace period instead of
# silently dropping the post.
LATE_APPROVAL_GRACE_MINUTES = 5


@dataclass(frozen=True, slots=True)
class PublishReceipt:
    """Proof that a partner accepted a post for publication."""

    provider: str
    reference: str
    scheduled_for: datetime

    def describe(self) -> str:
        """One human phrase for the log and the dashboard."""
        when = self.scheduled_for.strftime("%A %-d %B at %-I:%M %p")
        return f"{self.provider} has it queued for {when}"


class NotSentError(Exception):
    """The post definitely did not reach the publisher, so retrying is safe.

    The difference from a None return matters more than it looks. None means
    "no receipt", which covers both "the request never went out" and "the
    request went out and something went wrong afterwards" — and those need
    opposite handling: the first should be retried, the second must not be, or
    David's post goes up twice. Raise this only where no request left the
    process, so the retry sweep can tell the two apart.
    """


class Publisher(Protocol):
    """Anything that can schedule an approved post."""

    def queue(self, *, text: str, publish_at: datetime) -> PublishReceipt | None:
        """Schedule a post, or return None if it couldn't be scheduled.

        Raise NotSentError instead when nothing was sent at all — that is the
        signal that a retry cannot duplicate anything.
        """
        ...


class BufferPublisher:
    """Schedules posts through Buffer's API. UNVERIFIED — see module docstring."""

    provider = "Buffer"

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def queue(self, *, text: str, publish_at: datetime) -> PublishReceipt | None:
        """Hand the post to Buffer.

        Two kinds of failure, told apart because they need opposite handling:

        - NotSentError — no request reached Buffer. The draft stays approved and
          the retry sweep picks it up. A Buffer outage should delay a post,
          never lose it.
        - None — Buffer was contacted but gave us no usable receipt. It may have
          accepted the post, so the sweep leaves it alone and David sees it
          still waiting on his dashboard. A post that needs a nudge beats the
          same post going out twice.
        """
        import httpx

        from app import env_secrets

        try:
            token = env_secrets.buffer_token()
            profile_id = env_secrets.buffer_profile_id()
        except env_secrets.MissingSecretError as exc:
            # Nothing was sent — we never got as far as building a request.
            logger.error("Can't schedule the post: %s", exc)
            raise NotSentError(str(exc)) from exc

        when = _not_in_the_past(publish_at)

        try:
            response = httpx.post(
                f"{BUFFER_API_ROOT}/updates/create.json",
                data={
                    "access_token": token,
                    "profile_ids[]": profile_id,
                    "text": text,
                    "scheduled_at": int(when.timestamp()),
                },
                timeout=30.0,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.RequestError as exc:
            # The request never completed — connection refused, DNS failure,
            # timeout before the server answered. Nothing was posted, so this
            # is safe to retry.
            logger.warning(
                "Couldn't reach Buffer to schedule David's post (%s). It stays "
                "approved and will be retried.",
                exc.__class__.__name__,
            )
            raise NotSentError(str(exc)) from exc
        except Exception:
            # Buffer answered and something went wrong reading that answer. The
            # post may well have been accepted, so this must NOT be retried
            # automatically.
            logger.exception(
                "Buffer answered but the response couldn't be read. Not retrying "
                "automatically, in case the post was accepted."
            )
            return None

        reference = _buffer_reference(payload)
        if reference is None:
            logger.error(
                "Buffer accepted the request but didn't return an id for the post. "
                "Not retrying automatically, in case it was scheduled anyway."
            )
            return None

        receipt = PublishReceipt(provider=self.provider, reference=reference, scheduled_for=when)
        logger.info("David's post is scheduled. %s", receipt.describe())
        return receipt


class MockPublisher:
    """Offline stand-in. Records what would have been scheduled, sends nothing.

    Lets the whole pipeline — draft, approve, schedule — run end to end before
    any partner decision has been made or any credential exists.
    """

    provider = "Mock publisher"

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self.queued: list[tuple[str, datetime]] = []

    def queue(self, *, text: str, publish_at: datetime) -> PublishReceipt | None:
        when = _not_in_the_past(publish_at)
        self.queued.append((text, when))

        receipt = PublishReceipt(
            provider=self.provider,
            reference=f"mock-{len(self.queued)}",
            scheduled_for=when,
        )
        logger.info(
            "David's post is scheduled. %s (Mock — nothing was actually sent.)",
            receipt.describe(),
        )
        return receipt


def _not_in_the_past(publish_at: datetime) -> datetime:
    """Nudge a past publish time to just ahead of now.

    Happens when David approves after the slot has passed — he got the text at
    7am and answered at 4pm. Publishing a few minutes late is obviously better
    than not publishing at all.
    """
    from datetime import timedelta, timezone

    now = datetime.now(timezone.utc)
    if publish_at.tzinfo is None:
        publish_at = publish_at.replace(tzinfo=timezone.utc)

    if publish_at > now:
        return publish_at

    logger.info(
        "David approved after the planned time had passed, so the post goes out "
        "in a few minutes instead."
    )
    return now + timedelta(minutes=LATE_APPROVAL_GRACE_MINUTES)


def _buffer_reference(payload: object) -> str | None:
    """Pull the created update's id out of Buffer's response.

    FRAGILE: Buffer's v1 response shape. Handles both the documented
    `{"updates": [{"id": ...}]}` and a bare `{"id": ...}`.
    """
    if not isinstance(payload, dict):
        return None
    updates = payload.get("updates")
    if isinstance(updates, list) and updates and isinstance(updates[0], dict):
        reference = updates[0].get("id")
        if reference:
            return str(reference)
    reference = payload.get("id")
    return str(reference) if reference else None


def build_publisher(config: AppConfig) -> Publisher:
    """Pick the live or mock publisher based on config.yaml."""
    if config.integrations.buffer == "live":
        return BufferPublisher(config)
    return MockPublisher(config)
