"""Texting David that something is waiting for him.

What: two nudges — "someone is waiting on you" when new LinkedIn notifications
land, and one summary a day.

Why: the failure mode for this build is David not adopting the habit.
A dashboard he has to remember to open is a dashboard he won't open. A text
arrives on its own, and he already lives in texts all day.

Why so much restraint, then: because the opposite failure is worse and less
recoverable. A tool that buzzes his phone fifteen times on a busy morning gets
muted, and a muted tool is a cancelled engagement. Every path in here can
decline to send, and declining is the default when anything is uncertain.

These are NOT approval requests. Approval texts live in `ApprovalService` and
are the sanctioned path from `pending_approval` to `approved`. Nothing in this
module can change a draft's status, publish anything, or touch LinkedIn — it
reads counts and sends words.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.approvals.sms import SmsSender, build_sms_sender
from app.config import AppConfig
from app.db import DraftKind, DraftRepository
from app.db.alerts_repository import AlertsRepository
from app.logging_setup import get_logger

logger = get_logger(__name__)

NEW_ITEMS = "new_items"
DAILY_DIGEST = "daily_digest"


def _people_phrase(names: list[str], total: int) -> str:
    """"Kristen and Dana" — names beat counts when there are few enough.

    "Kristen Mills is waiting" tells David whether to care. "1 item is waiting"
    is the kind of thing a dashboard says, and he would have to open something
    to find out if it mattered.
    """
    firsts = [name.split()[0] for name in names if name]
    if not firsts:
        return f"{total} {'person' if total == 1 else 'people'}"
    if len(firsts) == 1:
        return firsts[0]
    if len(firsts) == 2:
        return f"{firsts[0]} and {firsts[1]}"
    if len(firsts) >= total:
        return ", ".join(firsts[:-1]) + f" and {firsts[-1]}"
    return ", ".join(firsts[:2]) + f" and {total - 2} more"


class AlertService:
    """Decides whether to text David, and what to say."""

    def __init__(
        self,
        config: AppConfig,
        drafts: DraftRepository,
        alerts: AlertsRepository,
        *,
        sender: SmsSender | None = None,
    ) -> None:
        self._config = config
        self._drafts = drafts
        self._alerts = alerts
        self._sender = sender or build_sms_sender(config)

    # -- the two alerts ----------------------------------------------------

    def notify_new_items(self, *, now: datetime | None = None) -> bool:
        """Text David that new people are waiting. Returns whether it sent.

        Called after an inbox poll finds something new. Reports everything
        currently open rather than only what this poll added — if he was texted
        two hours ago and three more arrived, "5 people are waiting" is the
        useful sentence, not "3 new ones".
        """
        alerts = self._config.alerts
        if not alerts.on_new_items:
            return False

        now = now or datetime.now(timezone.utc)
        if not self._may_send(now):
            return False

        if alerts.new_items_min_minutes:
            last = self._alerts.last_sent(NEW_ITEMS)
            if last and now - last < timedelta(minutes=alerts.new_items_min_minutes):
                logger.info(
                    "New LinkedIn notifications, but David was texted %s minutes ago — "
                    "not texting again yet.",
                    int((now - last).total_seconds() // 60),
                )
                return False

        names, total = self._waiting()
        if not total:
            return False

        who = _people_phrase(names, total)
        verb = "is" if total == 1 else "are"
        return self._send(
            NEW_ITEMS,
            f"{who} {verb} waiting on you on LinkedIn.{self._link()}",
            now=now,
        )

    def send_daily_digest(self, *, now: datetime | None = None) -> bool:
        """One summary a day. Silent when nothing is waiting.

        The safety net: if David ignores every other nudge, this is the one
        that still reaches him. It deliberately does NOT text him to say there
        is nothing to do — a daily "all clear" is the fastest way to teach
        someone to ignore a sender.
        """
        if self._config.alerts.daily_digest_at is None:
            return False

        now = now or datetime.now(timezone.utc)
        if not self._may_send(now):
            return False

        names, total = self._waiting()
        if not total:
            logger.info("Nothing waiting on David this morning, so no digest text.")
            return False

        who = _people_phrase(names, total)
        verb = "is" if total == 1 else "are"
        return self._send(
            DAILY_DIGEST,
            f"Morning David. {who} {verb} waiting for a reply on LinkedIn."
            f"{self._link()}",
            now=now,
        )

    # -- the guards --------------------------------------------------------

    def _may_send(self, now: datetime) -> bool:
        """False inside quiet hours.

        A suppressed alert is dropped rather than queued. Waking David at 7:01am
        with a buzz about something from 2am is worse than the daily digest
        telling him the same thing at 8:30, which it will.
        """
        local = now.astimezone(self._config.client.tz).time()
        if self._config.alerts.quiet_hours.covers(local):
            logger.info(
                "Skipped texting David — it's %s his time, inside quiet hours.",
                local.strftime("%-I:%M %p"),
            )
            return False
        return True

    def _waiting(self) -> tuple[list[str], int]:
        """Who is waiting on a reply, and how many in total.

        Sorted oldest-first by id, deliberately. The repository orders for the
        dashboard, not for this, and an unstable order would reword the same
        text each time it was composed. Naming whoever has been waiting longest
        is also the more useful sentence.
        """
        open_items = sorted(
            (
                draft
                for draft in self._drafts.list_open(limit=100)
                if draft.kind in (DraftKind.REPLY, DraftKind.COMMENT)
            ),
            key=lambda draft: draft.id,
        )
        names = [draft.person_name for draft in open_items if draft.person_name]
        return names, len(open_items)

    def _link(self) -> str:
        url = self._config.alerts.dashboard_url
        return f"\n\n{url}" if url else ""

    def _send(self, kind: str, body: str, *, now: datetime) -> bool:
        """Send, and only record it as sent if it actually went.

        Recording a failed send would start the throttle clock on a text David
        never received, and he would hear nothing for the next four hours.
        """
        sent = self._sender.send(to=self._config.approvals.phone_number, body=body)
        if not sent:
            logger.warning("Couldn't text David (%s). It's still on his dashboard.", kind)
            return False
        self._alerts.record(kind, when=now)
        logger.info("Texted David: %s", body.splitlines()[0])
        return True

    def claim_inbound_message(self, message_sid: str) -> bool:
        """Whether this inbound text is new, and so may be acted on.

        False means we have handled it before. The webhook needs this because a
        Twilio signature never expires: one captured copy of David's "YES" could
        otherwise be replayed to approve every post queued after it.
        """
        return self._alerts.claim_message(message_sid)
