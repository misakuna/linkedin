"""Sending David a text message.

What: the interface for sending SMS, plus a live sender and an offline mock.

Why: approval happens by text because that is what David already does all day.
A dashboard he has to remember to open is a dashboard he won't open; a text
arrives on its own.

How: an `SmsSender` protocol with two implementations, selected by
`integrations.sms` in config.yaml. The live one posts to Twilio's REST API with
httpx rather than pulling in the Twilio SDK — one endpoint doesn't justify a
dependency, and the existing approval pattern uses the same shape.
"""

from __future__ import annotations

from typing import Protocol

from app.config import AppConfig
from app.logging_setup import get_logger

logger = get_logger(__name__)

TWILIO_API_ROOT = "https://api.twilio.com/2010-04-01"

# SMS segments at 160 characters. Longer messages still send, but they arrive
# split and out of order on some carriers, which makes an approval text
# genuinely confusing. Drafts are truncated for the text; David sees the whole
# thing on the dashboard.
SMS_SAFE_LENGTH = 320


class SmsSender(Protocol):
    """Anything that can send David a text message."""

    def send(self, *, to: str, body: str) -> bool:
        """Send a message. Returns True if it went out."""
        ...


class TwilioSmsSender:
    """Sends real text messages through Twilio."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def send(self, *, to: str, body: str) -> bool:
        """Send a text, returning False on any failure rather than raising.

        A failed text must not lose the post: the draft stays in
        `pending_approval` and David can still approve it on the dashboard.
        """
        import httpx

        from app import env_secrets

        try:
            creds = env_secrets.sms_credentials()
        except env_secrets.MissingSecretError as exc:
            logger.error("Can't text David: %s", exc)
            return False

        try:
            response = httpx.post(
                f"{TWILIO_API_ROOT}/Accounts/{creds.account_sid}/Messages.json",
                auth=(creds.account_sid, creds.auth_token),
                data={"To": to, "From": creds.from_number, "Body": body},
                timeout=30.0,
            )
            response.raise_for_status()
        except Exception:
            logger.exception(
                "Couldn't text David. He can still approve on the dashboard, and "
                "the text will be retried."
            )
            return False

        logger.info("Texted David.")
        return True


class MockSmsSender:
    """Offline stand-in — prints the text instead of sending it.

    Printing the body matters: it's how the developer eyeballs whether the approval
    message actually reads clearly before David ever gets one.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self.sent: list[tuple[str, str]] = []

    def send(self, *, to: str, body: str) -> bool:
        self.sent.append((to, body))
        logger.info("Would have texted %s (Mock — nothing was sent):\n%s", to, body)
        return True


def build_sms_sender(config: AppConfig) -> SmsSender:
    """Pick the live or mock sender based on config.yaml."""
    if config.integrations.sms == "live":
        return TwilioSmsSender(config)
    return MockSmsSender(config)


def shorten_for_sms(text: str, limit: int = SMS_SAFE_LENGTH) -> str:
    """Trim drafted text to something that survives a text message intact."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"
