"""Repository for drafts and their status history.

What: the only code in the project that writes SQL against the drafts tables.

Why: keeping SQL in one place means the posting, inbox, approvals, and
dashboard modules all move drafts through the same validated lifecycle, and
every status change gets logged in the same plain-language way.

How: each method opens a short-lived connection (see database.connect) and maps
rows to the dataclasses in models.py.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from app.db.database import connect
from app.db.models import (
    OPEN_STATUSES,
    Draft,
    DraftEvent,
    DraftKind,
    DraftStatus,
    InvalidTransitionError,
    transitions_for,
)
from app.logging_setup import get_logger

logger = get_logger(__name__)

# Human sentences for the log, keyed by the status being entered. The developer reads
# these in Railway's log viewer; "Draft #4 moved to pending_approval" is worse
# than "Texted David for approval on post #4".
_TRANSITION_SENTENCES: dict[DraftStatus, str] = {
    DraftStatus.DRAFT: "Sent {kind} #{id} back for a rewrite",
    DraftStatus.PENDING_APPROVAL: "Asked David to approve {kind} #{id}",
    DraftStatus.APPROVED: "David approved {kind} #{id}",
    DraftStatus.DONE: "Finished {kind} #{id}",
    DraftStatus.REJECTED: "David passed on {kind} #{id}",
}


def _now() -> datetime:
    """UTC timestamp. Stored as ISO 8601; displayed in the client's timezone."""
    return datetime.now(timezone.utc)


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_draft(row: sqlite3.Row) -> Draft:
    return Draft(
        id=row["id"],
        kind=DraftKind(row["kind"]),
        status=DraftStatus(row["status"]),
        body=row["body"],
        person_name=row["person_name"],
        headline=row["headline"],
        notification_type=row["notification_type"],
        linkedin_url=row["linkedin_url"],
        source_ref=row["source_ref"],
        scheduled_for=_parse_dt(row["scheduled_for"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_event(row: sqlite3.Row) -> DraftEvent:
    return DraftEvent(
        id=row["id"],
        draft_id=row["draft_id"],
        from_status=DraftStatus(row["from_status"]) if row["from_status"] else None,
        to_status=DraftStatus(row["to_status"]),
        note=row["note"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class DraftNotFoundError(LookupError):
    """No draft with that id."""

    def __init__(self, draft_id: int) -> None:
        super().__init__(f"no draft with id {draft_id}")
        self.draft_id = draft_id


class DraftRepository:
    """Read and write drafts. Construct once, share freely across threads."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    # -- writes ------------------------------------------------------------

    def create(
        self,
        *,
        kind: DraftKind,
        body: str,
        person_name: str | None = None,
        headline: str | None = None,
        notification_type: str | None = None,
        linkedin_url: str | None = None,
        source_ref: str | None = None,
        scheduled_for: datetime | None = None,
        status: DraftStatus = DraftStatus.DRAFT,
    ) -> Draft:
        """Insert a new draft and record its first event.

        `source_ref` (e.g. a Gmail message id) is uniquely indexed, so polling
        the same inbox twice raises rather than duplicating David's work — see
        `create_if_new` for the forgiving version.
        """
        now = _now()
        with connect(self.db_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO drafts (
                    kind, status, body, person_name, headline, notification_type,
                    linkedin_url, source_ref, scheduled_for, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind.value,
                    status.value,
                    body,
                    person_name,
                    headline,
                    notification_type,
                    linkedin_url,
                    source_ref,
                    scheduled_for.isoformat() if scheduled_for else None,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            draft_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO draft_events (draft_id, from_status, to_status, note, created_at)
                VALUES (?, NULL, ?, ?, ?)
                """,
                (draft_id, status.value, "created", now.isoformat()),
            )

        # Deliberately neutral about whether there's drafted text: the inbox
        # stores cards with an empty body when LinkedIn gave us nothing to
        # reply to, and "wrote a draft" would be a lie in that case.
        who = f" for {person_name}" if person_name else ""
        logger.info("Added a new %s to David's list (#%s)%s", kind.value, draft_id, who)
        return self.get(draft_id)

    def create_if_new(self, *, source_ref: str, **fields) -> Draft | None:
        """Create a draft unless one already exists for this source.

        Used by the inbox poller, which re-reads the same LinkedIn notification
        emails on every run. Returns None when the draft already existed.
        """
        existing = self.get_by_source_ref(source_ref)
        if existing is not None:
            return None
        try:
            return self.create(source_ref=source_ref, **fields)
        except sqlite3.IntegrityError:
            # Another thread inserted it between the check and the insert.
            return None

    def transition(
        self,
        draft_id: int,
        new_status: DraftStatus,
        *,
        note: str | None = None,
    ) -> Draft:
        """Move a draft to a new status, refusing illegal jumps.

        Raises InvalidTransitionError if the lifecycle in models.py forbids the
        move — notably, nothing can skip David's approval.

        Read, check and write happen in ONE transaction, and the UPDATE names
        the status it expects to replace. Two callers can genuinely reach the
        same row at the same moment — David taps approve on the dashboard while
        his texted YES arrives at the webhook — and without both of those, each
        would read `pending_approval`, each would pass this check, and both
        would write. The loser of that race raises rather than silently
        overwriting the winner, so `draft_events` keeps a history that happened.
        """
        now = _now()
        with connect(self.db_path) as connection:
            # IMMEDIATE takes the write lock now rather than at the first
            # UPDATE, so a concurrent transition cannot slip between the SELECT
            # below and the UPDATE that follows it.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if row is None:
                raise DraftNotFoundError(draft_id)
            draft = _row_to_draft(row)

            if new_status not in transitions_for(draft.kind)[draft.status]:
                raise InvalidTransitionError(draft.status, new_status, draft.kind)

            cursor = connection.execute(
                "UPDATE drafts SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (new_status.value, now.isoformat(), draft_id, draft.status.value),
            )
            if cursor.rowcount != 1:
                # Someone moved it between our read and our write. Re-read to
                # report the status it actually holds now.
                current = _row_to_draft(
                    connection.execute(
                        "SELECT * FROM drafts WHERE id = ?", (draft_id,)
                    ).fetchone()
                )
                raise InvalidTransitionError(current.status, new_status, current.kind)

            connection.execute(
                """
                INSERT INTO draft_events (draft_id, from_status, to_status, note, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (draft_id, draft.status.value, new_status.value, note, now.isoformat()),
            )

        sentence = _TRANSITION_SENTENCES[new_status].format(kind=draft.kind.value, id=draft_id)
        logger.info("%s%s", sentence, f" ({note})" if note else "")
        return self.get(draft_id)

    def claim_for_publishing(self, draft_id: int) -> bool:
        """Win the exclusive right to hand this post to the publisher.

        True means the caller claimed it and must proceed; False means someone
        else already has and the caller must not call the publisher.

        The whole check lives in the WHERE clause, so SQLite decides the winner
        rather than application code reading and then writing. Without it, the
        30-minute retry sweep can fire while the dashboard's approve request is
        still inside a 30-second Buffer call: both see `approved`, both publish,
        and David's post appears on LinkedIn twice.

        Deliberately not released on failure. A post whose publisher call died
        mid-flight stays claimed, so the sweep leaves it alone rather than
        risking a second copy — a post that needs a human nudge beats a
        duplicate under David's name. `release_publish_claim` exists for the
        one case where we know no call was made.
        """
        with connect(self.db_path) as connection:
            cursor = connection.execute(
                """
                UPDATE drafts SET publish_claimed_at = ?, updated_at = ?
                WHERE id = ? AND status = ? AND kind = ? AND publish_claimed_at IS NULL
                """,
                (
                    _now().isoformat(),
                    _now().isoformat(),
                    draft_id,
                    DraftStatus.APPROVED.value,
                    DraftKind.POST.value,
                ),
            )
            return cursor.rowcount == 1

    def release_publish_claim(self, draft_id: int) -> None:
        """Give the claim back, for when we know the publisher was never called.

        Only safe where no request left the process — a missing credential, a
        refused precondition. Never after a request that may have reached the
        publisher.
        """
        with connect(self.db_path) as connection:
            connection.execute(
                "UPDATE drafts SET publish_claimed_at = NULL, updated_at = ? WHERE id = ?",
                (_now().isoformat(), draft_id),
            )

    def update_body(self, draft_id: int, body: str) -> Draft:
        """Replace a draft's text — David edited it, or Claude rewrote it."""
        with connect(self.db_path) as connection:
            cursor = connection.execute(
                "UPDATE drafts SET body = ?, updated_at = ? WHERE id = ?",
                (body, _now().isoformat(), draft_id),
            )
            if cursor.rowcount == 0:
                raise DraftNotFoundError(draft_id)
        return self.get(draft_id)

    def set_scheduled_for(self, draft_id: int, when: datetime) -> Draft:
        """Record the publish time sampled for a post draft."""
        with connect(self.db_path) as connection:
            cursor = connection.execute(
                "UPDATE drafts SET scheduled_for = ?, updated_at = ? WHERE id = ?",
                (when.isoformat(), _now().isoformat(), draft_id),
            )
            if cursor.rowcount == 0:
                raise DraftNotFoundError(draft_id)
        return self.get(draft_id)

    # -- reads -------------------------------------------------------------

    def get(self, draft_id: int) -> Draft:
        """Fetch one draft. Raises DraftNotFoundError if it isn't there."""
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()
        if row is None:
            raise DraftNotFoundError(draft_id)
        return _row_to_draft(row)

    def get_by_source_ref(self, source_ref: str) -> Draft | None:
        """Find the draft created from a given source (e.g. a Gmail message)."""
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM drafts WHERE source_ref = ?", (source_ref,)
            ).fetchone()
        return _row_to_draft(row) if row else None

    def list_by_status(
        self,
        statuses: Sequence[DraftStatus],
        *,
        kind: DraftKind | None = None,
        limit: int = 100,
    ) -> list[Draft]:
        """Drafts in any of the given statuses, newest first."""
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        params: list[object] = [status.value for status in statuses]
        sql = f"SELECT * FROM drafts WHERE status IN ({placeholders})"
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind.value)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with connect(self.db_path) as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_row_to_draft(row) for row in rows]

    def list_open(self, *, kind: DraftKind | None = None, limit: int = 100) -> list[Draft]:
        """Everything still waiting on David — the dashboard's Today screen."""
        return self.list_by_status(sorted(OPEN_STATUSES), kind=kind, limit=limit)

    def events(self, draft_id: int) -> list[DraftEvent]:
        """Full status history for one draft, oldest first."""
        with connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT * FROM draft_events WHERE draft_id = ? ORDER BY id", (draft_id,)
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def count_by_status(self) -> dict[DraftStatus, int]:
        """How many drafts sit in each status. Used for the boot log line."""
        with connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS n FROM drafts GROUP BY status"
            ).fetchall()
        return {DraftStatus(row["status"]): row["n"] for row in rows}
