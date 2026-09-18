"""Turning LinkedIn's tracking URLs into clean deep links.

What: `canonicalize()` — strips LinkedIn's per-recipient tracking parameters
off a URL and returns the plain destination.

Why: two reasons, one of them a security requirement.
  1. The links in LinkedIn's emails carry signed per-recipient tokens
     (`midToken`, `midSig`, `otpToken`, `loid`). `otpToken` in particular will
     sign the bearer straight into David's account. Those must never be stored
     in our database or rendered into a page.
  2. The raw URLs are 600+ characters of noise. David sees a button, but
     the developer reads these in logs.

How: keep the scheme, host, and path (minus LinkedIn's `/comm` tracking
prefix), and keep only the query parameters that identify *what* is being
linked to rather than *who* clicked.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Parameters that name the destination rather than the recipient. Everything
# else is dropped. Allowlisting rather than blocklisting means a tracking
# parameter LinkedIn invents next month is discarded by default.
KEEP_PARAMS = frozenset({"connId", "inviterVanityName", "miniProfileUrn"})

# LinkedIn wraps notification links in a /comm/ path prefix for click
# tracking. The same URL works without it, and reads better in a log line.
_TRACKING_PREFIX = "/comm/"


def canonicalize(url: str) -> str:
    """Strip tracking from a LinkedIn URL, keeping the destination intact.

    Returns the input unchanged if it isn't parseable — a slightly ugly link
    beats crashing the inbox poll over one malformed href.
    """
    if not url:
        return url

    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url

    path = parts.path
    if path.startswith(_TRACKING_PREFIX):
        path = "/" + path[len(_TRACKING_PREFIX) :]

    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if k in KEEP_PARAMS]

    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(kept), ""))


def is_linkedin(url: str | None) -> bool:
    """True when a URL points at linkedin.com (any country subdomain).

    Used as a safety check before anything reaches David's screen: a parser
    bug should never hand him a link to somewhere else entirely.
    """
    if not url:
        return False
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        return False
    return host == "linkedin.com" or host.endswith(".linkedin.com")
