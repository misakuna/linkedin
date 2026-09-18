"""PIN login for the dashboard.

What: checks a 4-digit PIN and issues a signed session cookie.

Why: anything David has to remember is ruled out. A PIN he types on a
big on-screen keypad is the most he should ever have to do, and the session
lasts a month so most days he taps the bookmark and he's in.

How: `itsdangerous` signs a timestamped cookie. The signature is what makes
this safe — the cookie carries no secret, and a forged one fails verification.
There is exactly one user, so there is no user id to store.

Note on threat model: this is a single-user tool holding drafted text and
LinkedIn deep links — no credentials, no LinkedIn session, nothing that can act
on David's account. A 4-digit PIN plus rate limiting is proportionate. It is
NOT sufficient for anything with real blast radius, so don't extend this to
gate something that matters without revisiting it.
"""

from __future__ import annotations

import hmac
import time
from dataclasses import dataclass, field

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app import env_secrets
from app.config import AppConfig
from app.logging_setup import get_logger

logger = get_logger(__name__)

COOKIE_NAME = "david_session"
_SESSION_VALUE = "ok"

# Brute-forcing 10,000 PINs is trivial without a limit. Five tries then a
# cool-off makes it useless, and David has never mistyped five times.
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60


@dataclass
class LoginGate:
    """Verifies PINs, issues session cookies, and throttles guessing."""

    config: AppConfig
    _failures: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._serializer = URLSafeTimedSerializer(
            env_secrets.session_secret(), salt="david-dashboard-session"
        )

    # -- login -------------------------------------------------------------

    @property
    def locked_out(self) -> bool:
        """True while too many wrong PINs have been tried recently."""
        cutoff = time.monotonic() - LOCKOUT_SECONDS
        self._failures = [at for at in self._failures if at > cutoff]
        return len(self._failures) >= MAX_ATTEMPTS

    def check_pin(self, pin: str) -> bool:
        """Verify a PIN, recording failures for the lockout."""
        if self.locked_out:
            return False

        # Constant-time compare so response timing can't leak the PIN.
        correct = hmac.compare_digest((pin or "").strip(), env_secrets.dashboard_pin())
        if correct:
            self._failures.clear()
            logger.info("David signed in.")
        else:
            self._failures.append(time.monotonic())
            logger.warning(
                "Wrong PIN entered (%s of %s before a cool-off).",
                len(self._failures),
                MAX_ATTEMPTS,
            )
        return correct

    # -- session cookie ----------------------------------------------------

    def issue(self) -> str:
        """The signed value to put in the session cookie."""
        return self._serializer.dumps(_SESSION_VALUE)

    def verify(self, cookie: str | None) -> bool:
        """Whether a cookie is a valid, unexpired session we issued."""
        if not cookie:
            return False
        try:
            value = self._serializer.loads(cookie, max_age=self.max_age_seconds)
        except (BadSignature, SignatureExpired):
            return False
        return value == _SESSION_VALUE

    @property
    def max_age_seconds(self) -> int:
        return self.config.dashboard.session_days * 24 * 60 * 60
