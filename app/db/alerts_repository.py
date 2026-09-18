"""When David was last texted, per kind of alert.

What: reads and writes one timestamp per alert kind.

Why: throttling has to survive a restart. Holding "last texted at" in memory
means a Railway redeploy resets it, and a deploy during a busy morning would
text David again straight away — exactly the behaviour the throttle exists to
prevent. On a hobby-tier host, restarts are routine.

How: one row per kind, upserted. Kept separate from `DraftRepository` because
it has nothing to do with the approval lifecycle — this is delivery bookkeeping,
not something that can reach LinkedIn.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.db.database import connect


class AlertsRepository:
    """Remembers when each kind of alert last went out."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def last_sent(self, kind: str) -> datetime | None:
        """When this kind of alert last went out, or None if it never has."""
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT sent_at FROM alerts_sent WHERE kind = ?", (kind,)
            ).fetchone()
        if row is None:
            return None
        moment = datetime.fromisoformat(row["sent_at"])
        # Rows written before this was tz-aware would otherwise compare badly.
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)

    def record(self, kind: str, *, when: datetime | None = None) -> datetime:
        """Mark this kind of alert as sent now (or at a given moment)."""
        when = when or datetime.now(timezone.utc)
        with connect(self.db_path) as connection:
            connection.execute(
                "INSERT INTO alerts_sent (kind, sent_at) VALUES (?, ?) "
                "ON CONFLICT(kind) DO UPDATE SET sent_at = excluded.sent_at",
                (kind, when.isoformat()),
            )
        return when

    def clear(self, kind: str | None = None) -> None:
        """Forget the last-sent time. Used by tests and by hand when debugging."""
        with connect(self.db_path) as connection:
            if kind is None:
                connection.execute("DELETE FROM alerts_sent")
            else:
                connection.execute("DELETE FROM alerts_sent WHERE kind = ?", (kind,))

    def claim_message(self, message_sid: str) -> bool:
        """Record an inbound Twilio message id, first caller only.

        True means this message is new and may be acted on. False means it has
        been seen before and must be ignored.

        This is what makes a signed Twilio request single-use. The signature
        covers the URL and parameters with no nonce or timestamp, so a captured
        request stays valid forever, and each replay would approve the next post
        waiting on David. The INSERT either wins or raises on the primary key —
        no read-then-write, so two copies arriving together cannot both win.
        """
        with connect(self.db_path) as connection:
            try:
                connection.execute(
                    "INSERT INTO sms_messages_seen (message_sid, seen_at) VALUES (?, ?)",
                    (message_sid, datetime.now(timezone.utc).isoformat()),
                )
            except sqlite3.IntegrityError:
                return False
        return True
