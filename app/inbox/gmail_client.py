"""Reading LinkedIn's notification emails out of David's Gmail.

What: fetches raw emails matching a search, as `EmailMessage` objects.

Why: this is how the system learns what happened on LinkedIn without ever
touching LinkedIn. LinkedIn emails David; David auto-forwards those emails to
a developer-side mailbox; we read that. The server holds no LinkedIn session, cookie, or
credential, so there is nothing that could ever be flagged against his account
(the compliance rules in README.md: no LinkedIn contact, read-only mailbox).

The mailbox we read is deliberately NOT David's own. It is an address on the
Google Workspace that receives only what he forwards, which means the OAuth app
can be "Internal" — no Google verification, no 7-day refresh-token expiry — and
the server never has access to his personal mail. See .env.example for setup.

How: the Gmail API with **`gmail.readonly` scope and nothing else**. The agent
never sends, deletes, labels, or modifies anything in David's mailbox — the
scope constant below makes that a property of the OAuth grant rather than a
promise about our code. A mock implementation reads the committed .eml fixtures
instead, so the whole pipeline runs offline.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol

from app.config import PROJECT_ROOT, AppConfig
from app.logging_setup import get_logger

logger = get_logger(__name__)

# READ-ONLY, and this is the only scope this project will ever request.
# Anything that needs more than this is out of scope by design.
GMAIL_SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/gmail.readonly",)

FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures"


@dataclass(frozen=True, slots=True)
class FetchedEmail:
    """One email, with the id we use to avoid processing it twice."""

    message_id: str
    message: EmailMessage


class MailReader(Protocol):
    """Anything that can hand us LinkedIn notification emails."""

    def fetch(self, query: str, limit: int) -> list[FetchedEmail]:
        """Return emails matching a Gmail search, newest first."""
        ...


class GmailReader:
    """Reads David's real mailbox over the Gmail API, read-only."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._service = None

    def _get_service(self):
        if self._service is None:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            from app import env_secrets

            creds = env_secrets.gmail_credentials()
            credentials = Credentials(
                token=None,
                refresh_token=creds.refresh_token,
                client_id=creds.client_id,
                client_secret=creds.client_secret,
                token_uri="https://oauth2.googleapis.com/token",
                scopes=list(GMAIL_SCOPES),
            )
            self._service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        return self._service

    def fetch(self, query: str, limit: int) -> list[FetchedEmail]:
        """Fetch matching emails, degrading to an empty list on any failure.

        An empty list means "nothing new to show", which leaves the dashboard
        displaying whatever was already stored — the graceful-degradation
        read-only requirement.
        """
        try:
            service = self._get_service()
            listing = (
                service.users()
                .messages()
                .list(userId="me", q=query, maxResults=limit)
                .execute()
            )
        except Exception:
            logger.exception(
                "Couldn't reach Gmail to look for new LinkedIn emails. "
                "David's dashboard will show what we already had."
            )
            return []

        found: list[FetchedEmail] = []
        for stub in listing.get("messages", []):
            message_id = stub["id"]
            try:
                payload = (
                    service.users()
                    .messages()
                    .get(userId="me", id=message_id, format="raw")
                    .execute()
                )
                raw = base64.urlsafe_b64decode(payload["raw"])
                found.append(
                    FetchedEmail(
                        message_id=message_id,
                        message=message_from_bytes(raw, policy=policy.default),
                    )
                )
            except (KeyError, binascii.Error, ValueError):
                logger.warning("Skipped a Gmail message (%s) I couldn't decode.", message_id)
            except Exception:
                logger.exception("Skipped a Gmail message (%s) after an error.", message_id)

        logger.info("Read %s LinkedIn email(s) from Gmail.", len(found))
        return found


class MockMailReader:
    """Reads the committed .eml fixtures instead of a real mailbox.

    Makes no network calls and needs no credentials, so the entire inbox
    pipeline — parse, draft, store, display — runs end to end on a laptop
    against real LinkedIn emails.
    """

    def __init__(self, config: AppConfig, fixture_dir: Path | None = None) -> None:
        self._config = config
        self._fixture_dir = fixture_dir or FIXTURE_DIR

    def fetch(self, query: str, limit: int) -> list[FetchedEmail]:
        """Return every fixture, newest-first by filename, ignoring the query."""
        from app.inbox.parser import read_fixture

        paths = sorted(
            (p for p in self._fixture_dir.glob("*.eml") if not p.name.startswith("._")),
            reverse=True,
        )[:limit]

        found = [
            FetchedEmail(message_id=f"fixture:{path.stem}", message=read_fixture(path))
            for path in paths
        ]
        logger.info(
            "Read %s LinkedIn email(s) from saved test files. (Mock — Gmail was not "
            "contacted.)",
            len(found),
        )
        return found


def build_mail_reader(config: AppConfig) -> MailReader:
    """Pick the live or mock reader based on config.yaml."""
    if config.integrations.gmail == "live":
        return GmailReader(config)
    return MockMailReader(config)
