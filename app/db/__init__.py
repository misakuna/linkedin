"""SQLite persistence — a thin repository layer over the stdlib driver.

Everything the assistant produces (post drafts, reply drafts, comment drafts)
lives here with an explicit status, so the dashboard can always answer "what is
waiting on David?" without calling any external API.
"""

from app.db.database import connect, init_db
from app.db.models import (
    ALLOWED_TRANSITIONS,
    Draft,
    DraftEvent,
    DraftKind,
    DraftStatus,
    InvalidTransitionError,
)
from app.db.repository import DraftRepository

__all__ = [
    "ALLOWED_TRANSITIONS",
    "Draft",
    "DraftEvent",
    "DraftKind",
    "DraftRepository",
    "DraftStatus",
    "InvalidTransitionError",
    "connect",
    "init_db",
]
