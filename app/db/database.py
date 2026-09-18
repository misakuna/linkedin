"""SQLite connection handling.

What: opens connections with the pragmas this app needs and applies schema.sql.

Why: two things touch the database — the FastAPI request thread and the
APScheduler worker thread. Rather than share one connection across threads
(which sqlite3 forbids by default), every operation opens its own short-lived
connection. At this scale (one user, a handful of writes a day) that costs
nothing and removes a whole class of threading bugs.

How: `connect()` is a context manager that commits on success and rolls back on
error. `init_db()` runs the idempotent schema at boot.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.logging_setup import get_logger

logger = get_logger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on clean exit, roll back on exception."""
    db_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(db_path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    # ON DELETE CASCADE for draft_events is off by default in sqlite3.
    connection.execute("PRAGMA foreign_keys = ON")
    # WAL lets the dashboard read while a scheduler job is writing.
    connection.execute("PRAGMA journal_mode = WAL")

    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


# Columns added after the first release. CREATE TABLE IF NOT EXISTS won't add a
# column to a table that already exists, so a redeploy against the mounted
# volume needs these applied by hand. Keep this list append-only.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("drafts", "headline", "TEXT"),
    ("drafts", "notification_type", "TEXT"),
)


def init_db(db_path: Path) -> None:
    """Create tables and indexes if they don't exist yet, then patch columns.

    Safe to run on every boot — a redeploy against an existing volume leaves
    the data untouched and only adds what's missing.
    """
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with connect(db_path) as connection:
        connection.executescript(schema)
        _add_missing_columns(connection)
    logger.info("Database ready at %s", db_path)


def _add_missing_columns(connection: sqlite3.Connection) -> None:
    """Add any column introduced after this database was first created."""
    for table, column, column_type in _ADDED_COLUMNS:
        existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column in existing:
            continue
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
        logger.info("Added the %s.%s column to the existing database.", table, column)
