"""Inbox — read LinkedIn's notification emails and draft replies.

Automation level: draft-only. Nothing here sends anything to anyone. It reads
David's Gmail (read-only scope), works out what LinkedIn is telling him, and
writes a suggested reply for him to send by hand.

This is the most brittle module in the project: LinkedIn changes its email
formats without notice. Selectors are isolated in parser.py and marked
# FRAGILE so a format change is a one-line fix, caught by a fixture test.
"""

from app.inbox.models import (
    ACTIONABLE_TYPES,
    NotificationType,
    ParsedNotification,
)
from app.inbox.parser import classify, parse_email, read_fixture

__all__ = [
    "ACTIONABLE_TYPES",
    "NotificationType",
    "ParsedNotification",
    "classify",
    "parse_email",
    "read_fixture",
]
