"""Storage for David's conversation with the assistant.

What: appends and reads chat messages.

Why: kept separate from `DraftRepository` because it has nothing to do with the
approval lifecycle. Drafts are things that might reach LinkedIn and are governed
by a status machine; chat is a conversation and governed by nothing.

How: same short-lived-connection pattern as the draft repository.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from app.db.database import connect

Role = Literal["user", "assistant"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One turn of the conversation."""

    id: int
    role: Role
    content: str
    created_at: datetime


def _row_to_message(row: sqlite3.Row) -> ChatMessage:
    return ChatMessage(
        id=row["id"],
        role=row["role"],
        content=row["content"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class ChatRepository:
    """Read and append David's chat history."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def append(self, role: Role, content: str) -> ChatMessage:
        """Add one message to the end of the conversation."""
        now = datetime.now(timezone.utc).isoformat()
        with connect(self.db_path) as connection:
            cursor = connection.execute(
                "INSERT INTO chat_messages (role, content, created_at) VALUES (?, ?, ?)",
                (role, content, now),
            )
            message_id = int(cursor.lastrowid)
            row = connection.execute(
                "SELECT * FROM chat_messages WHERE id = ?", (message_id,)
            ).fetchone()
        return _row_to_message(row)

    def recent(self, limit: int = 40) -> list[ChatMessage]:
        """The last N messages, oldest first — the order a chat renders in."""
        with connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT * FROM (SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?) "
                "ORDER BY id ASC",
                (limit,),
            ).fetchall()
        return [_row_to_message(row) for row in rows]

    def clear(self) -> int:
        """Delete the whole conversation. Returns how many messages went."""
        with connect(self.db_path) as connection:
            cursor = connection.execute("DELETE FROM chat_messages")
            return cursor.rowcount
