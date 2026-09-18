"""Secrets loaded from the environment (.env locally, host env vars in prod).

What: one place that reads API keys and the dashboard PIN.

Why: secrets stay out of config.yaml and out of git. Just as
importantly, there is no LinkedIn credential here and there never will be
(Hard Rule 4) — the assistant does not log into LinkedIn.

How: python-dotenv loads .env if present, then each accessor reads os.environ
on demand. Missing keys raise a message naming the exact variable to set,
because that error is read remotely from a log viewer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from app.config import PROJECT_ROOT

# Load .env once at import. override=False so real host env vars (Railway,
# Render) always beat a stale local file.
load_dotenv(PROJECT_ROOT / ".env", override=False)

# Used when DASHBOARD_PIN is unset, so a fresh clone boots. Logged loudly.
DEV_FALLBACK_PIN = "0000"

# Likewise for cookie signing. Public by definition — anything signed with it
# is forgeable, which is exactly why boot warns when it's still in use.
DEV_FALLBACK_SESSION_SECRET = "development-only-not-a-secret"


class MissingSecretError(RuntimeError):
    """A required environment variable is unset."""

    def __init__(self, variable: str, purpose: str) -> None:
        super().__init__(
            f"{variable} is not set — needed for {purpose}. "
            f"Add it to .env (see .env.example) or to the host's environment variables."
        )
        self.variable = variable


def _required(variable: str, purpose: str) -> str:
    value = os.environ.get(variable, "").strip()
    if not value:
        raise MissingSecretError(variable, purpose)
    return value


@dataclass(frozen=True)
class GmailCredentials:
    """OAuth material for READ-ONLY Gmail access to David's mailbox."""

    client_id: str
    client_secret: str
    refresh_token: str
    user: str


def anthropic_api_key() -> str:
    """Claude API key, used for drafting posts, replies, and comments."""
    return _required("ANTHROPIC_API_KEY", "drafting text with the Claude API")


def gmail_credentials() -> GmailCredentials:
    """Gmail OAuth credentials. Read-only scope; the agent never sends email."""
    purpose = "reading LinkedIn notification emails (read-only)"
    return GmailCredentials(
        client_id=_required("GMAIL_CLIENT_ID", purpose),
        client_secret=_required("GMAIL_CLIENT_SECRET", purpose),
        refresh_token=_required("GMAIL_REFRESH_TOKEN", purpose),
        user=_required("GMAIL_USER", purpose),
    )


def gemini_api_key() -> str:
    """Gemini API key, used only if chat.provider is set to gemini."""
    return _required("GEMINI_API_KEY", "answering David's questions with Gemini")


def buffer_token() -> str:
    """Buffer access token — the only sanctioned LinkedIn automation path."""
    return _required("BUFFER_TOKEN", "scheduling approved posts through Buffer")


def buffer_profile_id() -> str:
    """Buffer's id for David's LinkedIn profile."""
    return _required("BUFFER_PROFILE_ID", "telling Buffer which profile to post to")


@dataclass(frozen=True)
class SmsCredentials:
    """Credentials for the SMS provider that carries approval requests."""

    account_sid: str
    auth_token: str
    from_number: str


def sms_credentials() -> SmsCredentials:
    """SMS provider credentials for texting David approval requests."""
    purpose = "texting David approval requests"
    return SmsCredentials(
        account_sid=_required("SMS_ACCOUNT_SID", purpose),
        auth_token=_required("SMS_AUTH_TOKEN", purpose),
        from_number=_required("SMS_FROM_NUMBER", purpose),
    )


def dashboard_pin() -> str:
    """David's 4-digit dashboard PIN.

    Falls back to a well-known dev PIN so a fresh clone boots without setup.
    Callers are expected to warn when `dashboard_pin_is_default()` is true.
    """
    return os.environ.get("DASHBOARD_PIN", "").strip() or DEV_FALLBACK_PIN


def dashboard_pin_is_default() -> bool:
    """True when nobody has set a real PIN yet — unsafe for David's live URL."""
    return dashboard_pin() == DEV_FALLBACK_PIN


def session_secret() -> str:
    """Key used to sign David's dashboard session cookie.

    Falls back to a fixed development value so a fresh clone boots. It must be
    a real secret in production, and it must be STABLE across deploys — a value
    generated at boot would sign David out on every redeploy.
    """
    return os.environ.get("SESSION_SECRET", "").strip() or DEV_FALLBACK_SESSION_SECRET


def session_secret_is_default() -> bool:
    """True when the cookie-signing key is still the public development one."""
    return session_secret() == DEV_FALLBACK_SESSION_SECRET
