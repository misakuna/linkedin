"""Parsing LinkedIn's notification emails.

What: turns one email into a list of `ParsedNotification`s.

Why: this is the only place in the system that knows what a LinkedIn email
looks like, and it is the most brittle module in the project — LinkedIn changes
these formats without notice. Isolating that knowledge here means a format
change is a one-selector fix, caught by a failing fixture test, shipped in one
deploy, with David's machine never involved.

How, in two steps:

  1. **Classify** by the `X-LinkedIn-Template` header. LinkedIn stamps a
     template name on its notification mail, which is far sturdier than
     matching subject lines (those vary by locale, A/B cohort, and sender).
     Anything unrecognised is `other` — noise, shown to nobody — and logged at
     WARNING so the developer sees a new format before David notices anything.

  2. **Extract** with one selector function per template, each marked
     ``# FRAGILE``. When LinkedIn changes a format, exactly one of these
     functions needs editing.

Every ``# FRAGILE`` marker below is a place where we depend on LinkedIn's
current formatting. Grep for it.
"""

from __future__ import annotations

import re
from datetime import datetime
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path

from app.inbox import html_text
from app.inbox.links import canonicalize, is_linkedin
from app.inbox.models import NotificationType, ParsedNotification
from app.logging_setup import get_logger

logger = get_logger(__name__)

# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

# FRAGILE: LinkedIn's template names, taken from the X-LinkedIn-Template header
# of real notification emails (see tests/fixtures/README.md for which fixture
# proves each row). A template missing from this map is treated as noise.
TEMPLATE_TYPES: dict[str, NotificationType] = {
    "email_member_message_v2": NotificationType.MESSAGE_RECEIVED,
    "email_m2m_invite_single_01": NotificationType.CONNECTION_REQUEST,
    "email_accept_invite_single_01": NotificationType.CONNECTION_ACCEPTED,
    "email_accept_invite_digest_02": NotificationType.CONNECTION_ACCEPTED,
    "email_notification_single_commented_on_your_update_01": NotificationType.MENTION,
}

# FRAGILE + UNVERIFIED: no fixture exists for a true @mention. These names are
# inferred from LinkedIn's naming convention, not observed. A mention may not
# arrive as a standalone email at all — it plausibly rolls up into
# `email_notification_digest_01`. Delete or correct these the first time a real
# one lands in tests/fixtures/.
UNVERIFIED_TEMPLATES: dict[str, NotificationType] = {
    "email_notification_single_mentioned_in_update_01": NotificationType.MENTION,
    "email_notification_single_mentioned_in_comment_01": NotificationType.MENTION,
}

# Templates we've seen and deliberately classify as noise. Listing them keeps
# the "unrecognised template" warning meaningful — it fires only for formats
# nobody has looked at yet.
KNOWN_NOISE_TEMPLATES: frozenset[str] = frozenset(
    {
        "email_editorial_suggested_top_conversations_01",
        "email_series_follow_newsletter_02",
        "email_notification_digest_01",
        "email_pymk_02",
        "email_notification_single_search_appearance_01",
        "email_weekly_analytics_recap_v2",
        "email_groups_recommended_by_admin_01",
        "email_next_best_action_digest_01",
    }
)


def classify(message: EmailMessage) -> tuple[NotificationType, str | None]:
    """Decide what an email is, and return the template name for diagnostics.

    Unknown templates return `other`. That default is deliberate: showing
    David nothing is a recoverable failure, showing him a generic AI reply
    under his own name is not.
    """
    template = message.get("X-LinkedIn-Template")
    template = str(template).strip() if template else None

    if template is None:
        # LinkedIn's marketing mail (Premium upsells, "your profile is getting
        # hits") carries no X-LinkedIn-* headers at all. It is always noise.
        return NotificationType.OTHER, None

    if template in TEMPLATE_TYPES:
        return TEMPLATE_TYPES[template], template

    if template in UNVERIFIED_TEMPLATES:
        logger.warning(
            "Classified a LinkedIn email using an unverified template guess (%s). "
            "Save it as a fixture so this stops being a guess.",
            template,
        )
        return UNVERIFIED_TEMPLATES[template], template

    if template not in KNOWN_NOISE_TEMPLATES:
        logger.warning(
            "Saw a LinkedIn email format I don't recognise (template: %s, subject: %r). "
            "Ignored it. If David is missing something, this is why.",
            template,
            _subject(message),
        )

    return NotificationType.OTHER, template


# --------------------------------------------------------------------------
# Reading the email
# --------------------------------------------------------------------------


def read_fixture(path: Path) -> EmailMessage:
    """Load a .eml file from disk as an EmailMessage (used by tests and mocks)."""
    from email import policy, message_from_binary_file

    with path.open("rb") as handle:
        return message_from_binary_file(handle, policy=policy.default)


def _subject(message: EmailMessage) -> str:
    return " ".join(str(message.get("Subject", "")).split())


def _received_at(message: EmailMessage) -> datetime | None:
    raw = message.get("Date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None


def _sender_name(message: EmailMessage) -> str | None:
    """The From display name, with LinkedIn's decoration removed.

    LinkedIn puts the acting person in the From display name — "Ephraim Adeyemo
    via LinkedIn", "Nina Nguyen" — which is the single most reliable place to
    find a name. When LinkedIn itself is the sender ("LinkedIn", "LinkedIn
    News") there is no person and this returns None.
    """
    raw = str(message.get("From", ""))
    display = raw.split("<")[0].strip().strip('"').strip()

    # FRAGILE: LinkedIn's From-line decorations.
    for suffix in (" via LinkedIn", " on LinkedIn"):
        if display.endswith(suffix):
            display = display[: -len(suffix)].strip()

    if not display or display.lower().startswith("linkedin"):
        return None
    return display


def _plain_lines(message: EmailMessage) -> list[str]:
    """Non-empty lines of the text/plain body, footer removed."""
    body = message.get_body(preferencelist=("plain",))
    if body is None:
        return []
    try:
        text = body.get_content()
    except (LookupError, UnicodeDecodeError):
        return []
    return _strip_footer(text)


def _strip_footer(text: str) -> list[str]:
    """Drop LinkedIn's boilerplate footer and return non-empty lines.

    FRAGILE: the footer starts with a long rule of hyphens. Everything after it
    is unsubscribe links and legal text — never content, and full of URLs that
    would otherwise confuse the link selectors.
    """
    head = re.split(r"\n-{10,}\n", text)[0]
    return [line.strip() for line in head.splitlines() if line.strip()]


def _html_content(message: EmailMessage) -> html_text.HtmlContent:
    body = message.get_body(preferencelist=("html",))
    if body is None:
        return html_text.HtmlContent(text="", links=[])
    try:
        return html_text.extract(body.get_content())
    except (LookupError, UnicodeDecodeError):
        return html_text.HtmlContent(text="", links=[])


def _first_link(lines: list[str], label: str) -> str | None:
    """The URL on the first line beginning with `label`.

    LinkedIn's plain-text bodies label their links ("Accept:", "View
    message:", "View profile:"), which makes them addressable without
    depending on ordering.
    """
    pattern = re.compile(rf"^{re.escape(label)}\s*(https?://\S+)", re.IGNORECASE)
    for line in lines:
        match = pattern.match(line)
        if match:
            return canonicalize(match.group(1))
    return None


def _safe(url: str | None) -> str | None:
    """Pass a URL through only if it points at LinkedIn."""
    return url if is_linkedin(url) else None


# --------------------------------------------------------------------------
# Selectors — one per template. Each is FRAGILE by construction.
# --------------------------------------------------------------------------

# FRAGILE: "Ephraim Adeyemo (I help Spas, Clinics & Home Service Owners…)"
_NAME_AND_HEADLINE = re.compile(r"^(?P<name>[^(][^\n(]*?)\s*\((?P<headline>[^()]*)\)$")

# FRAGILE: the bare profile URL line under a name, "(https://ng.linkedin.com/in/x)"
_BARE_URL_LINE = re.compile(r"^\((?P<url>https?://[^\s)]+)\)$")


def _select_messages(message: EmailMessage, lines: list[str]) -> list[dict]:
    """`email_member_message_v2` — "X just messaged you".

    FRAGILE. Shape of each block in the plain-text body:

        Ephraim Adeyemo (I help Spas, Clinics & Home Service Owners capture…)
        (https://ng.linkedin.com/in/ephraimadeyemo)
        View message: https://www.linkedin.com/comm/messaging/thread/…

    Note what is NOT here: the message text. LinkedIn does not include it, so
    `preview` stays None and the drafter must not pretend to know what was
    said. This email arrives through LinkedIn's *digest* pipeline
    (messaging-digest-noreply@), so a busy day yields one email with the block
    above repeated — hence the fan-out.
    """
    found: list[dict] = []
    for index, line in enumerate(lines):
        if not line.lower().startswith("view message:"):
            continue
        link = _first_link([line], "View message:")

        name = headline = profile = None
        # Walk back over the block that belongs to this link.
        for previous in reversed(lines[max(0, index - 4) : index]):
            if profile is None and (bare := _BARE_URL_LINE.match(previous)):
                profile = canonicalize(bare.group("url"))
                continue
            if pair := _NAME_AND_HEADLINE.match(previous):
                name = pair.group("name").strip()
                headline = pair.group("headline").strip() or None
                break

        found.append(
            {
                "person_name": name or _sender_name(message),
                "headline": headline,
                "linkedin_url": _safe(link),
                "profile_url": _safe(profile),
            }
        )
    return found


def _select_invite(message: EmailMessage, lines: list[str]) -> list[dict]:
    """`email_m2m_invite_single_01` — "You have an invitation".

    FRAGILE. Plain-text shape:

        Nina, Sales Manager from Viethair_factory is waiting for your response
        Hi David, I'd like to join your professional network      <- the note
        Nina Nguyen                                                <- the name
        VIET_HAIR FACTORY                                          <- headline
        …
        Accept:https://www.linkedin.com/comm/mynetwork/invite-accept/…
        View profile:https://vn.linkedin.com/comm/in/…

    The name comes from the From header rather than the body — it is cleaner
    there and survives the body reflowing. The headline is the line directly
    after the name, which is the fragile part: an inviter whose headline wraps
    onto several lines yields only the first.
    """
    name = _sender_name(message)

    headline = None
    if name:
        for index, line in enumerate(lines[:-1]):
            if line == name:
                headline = lines[index + 1]
                break

    # FRAGILE: LinkedIn's default note, or the inviter's custom one, addressed
    # to David by first name.
    note = next((line for line in lines if re.match(r"^Hi [A-Z][\w'’-]*,", line)), None)

    return [
        {
            "person_name": name,
            "headline": headline,
            "preview": note,
            # The accept link is where David goes to act. He clicks it himself,
            # from his own browser — the server never follows it.
            "linkedin_url": _safe(_first_link(lines, "Accept:")),
            "profile_url": _safe(_first_link(lines, "View profile:")),
        }
    ]


# Lines that sit exactly where a headline would in the acceptance layout but
# aren't one — LinkedIn's buttons. A person whose headline is literally
# "Message" loses their headline; that is the right way round to fail.
_NOT_A_HEADLINE = frozenset({"message", "connect", "follow", "view profile"})


def _acceptance_headline(name: str | None, content: html_text.HtmlContent) -> str | None:
    """The accepter's headline, which sits directly under their name.

    FRAGILE. The HTML shape:

        See Angie Enriquez's connections, experience, and more
        Angie has accepted your invitation
        Angie Enriquez                                  <- exact full name
        Productora Audiovisual | Marketing | Aviation   <- headline
        Message                                         <- a button
        Suggestions from Angie's network                <- four strangers follow

    Exact-match on the full name is what keeps those suggestions out. Their
    names sit alone on their own lines too, so a looser match would cheerfully
    return a stranger's headline as the accepter's — and David would send a
    warm note referencing the wrong person's job. Bailing at the suggestions
    header means a layout change that moves the name can't reach them either.
    """
    if not name:
        return None

    lines = [line.strip() for line in content.text.splitlines() if line.strip()]
    for index, line in enumerate(lines[:-1]):
        if line.lower().startswith("suggestions from"):
            return None
        if line != name:
            continue
        candidate = lines[index + 1]
        if candidate.lower() in _NOT_A_HEADLINE or len(candidate) < 3:
            return None
        return candidate
    return None


def _select_acceptance(message: EmailMessage, lines: list[str]) -> list[dict]:
    """`email_accept_invite_single_01` — "X accepted your invitation".

    FRAGILE, and the odd one out: this email's text/plain part contains only
    the footer, so everything comes from the HTML. The HTML holds five or more
    /in/ profile links — the accepter plus four "explore their network"
    suggestions — so taking the first one names the wrong person. We match the
    link whose slug contains the accepter's name instead.
    """
    name = _sender_name(message)
    content = _html_content(message)

    profile = None
    if name:
        tokens = [t for t in re.split(r"\W+", name.lower()) if len(t) > 2]
        for href in content.links:
            clean = canonicalize(href)
            if "/in/" not in clean:
                continue
            slug = clean.rsplit("/in/", 1)[-1].lower()
            if tokens and all(token in slug for token in tokens):
                profile = clean
                break

    return [
        {
            "person_name": name,
            # Without this the card reads "we couldn't write a suggestion" on an
            # email where nothing was written to read — and a headline is all a
            # warm "good to connect" note needs.
            "headline": _acceptance_headline(name, content),
            # Send David to their profile — from there he can say hello. There
            # is no better action link in this email.
            "linkedin_url": _safe(profile),
            "profile_url": _safe(profile),
        }
    ]


def _select_acceptance_digest(message: EmailMessage, lines: list[str]) -> list[dict]:
    """`email_accept_invite_digest_02` — "You have N new connections".

    FRAGILE, and the reason parsing returns a list. Each person is a
    four-line block in the plain-text body:

        Monique Ally                          <- name
        Afro Hairdresser/stylist              <- headline
        View profile: https://…/in/monique-ally-963272318
        Message: https://…/messaging/compose/?connId=monique-ally-963272318

    Position-based rather than label-based, because the name and headline
    carry no labels. A wrapped URL from the previous block can land between
    two blocks; it sits *before* the next name, so it doesn't shift these.
    """
    found: list[dict] = []
    for index, line in enumerate(lines):
        if not line.lower().startswith("view profile:") or index < 2:
            continue
        found.append(
            {
                "person_name": lines[index - 2],
                "headline": lines[index - 1],
                # David's action here is to send a welcome note, so the
                # compose link beats the profile link.
                "linkedin_url": _safe(_first_link(lines[index:], "Message:"))
                or _safe(_first_link([line], "View profile:")),
                "profile_url": _safe(_first_link([line], "View profile:")),
            }
        )
    return found


# FRAGILE: "Bernard Lim commented on your post."
_COMMENT_SUBJECT = re.compile(r"^(?P<name>.+?)\s+commented on\b")


def _select_comment(message: EmailMessage, lines: list[str]) -> list[dict]:
    """`email_notification_single_commented_on_your_update_01`.

    FRAGILE. The From header is LinkedIn itself, so the commenter's name comes
    from the subject line. The comment text is wrapped in curly quotes at the
    top of the body, followed by an excerpt of David's own post:

        Bernard Lim commented on your post.
        “Sam, This approach could really change the game for federal audits…
        Connect with me and drop me a hi”
        I've spent a lot of time thinking about why federal schedule audits…
        7 reactions~1 comment
        Join the conversation https://www.linkedin.com/feed/update/urn:li:activity:…
    """
    subject = _subject(message)
    match = _COMMENT_SUBJECT.match(subject)

    # FRAGILE: the comment sits between LinkedIn's curly quotation marks.
    body = "\n".join(lines)
    quoted = re.search(r"[“\"](?P<text>.+?)[”\"]", body, re.DOTALL)

    return [
        {
            "person_name": match.group("name").strip() if match else _sender_name(message),
            "preview": " ".join(quoted.group("text").split()) if quoted else None,
            "linkedin_url": _safe(_first_link(lines, "Join the conversation")),
        }
    ]


SELECTORS = {
    "email_member_message_v2": _select_messages,
    "email_m2m_invite_single_01": _select_invite,
    "email_accept_invite_single_01": _select_acceptance,
    "email_accept_invite_digest_02": _select_acceptance_digest,
    "email_notification_single_commented_on_your_update_01": _select_comment,
    "email_notification_single_mentioned_in_update_01": _select_comment,
    "email_notification_single_mentioned_in_comment_01": _select_comment,
}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def parse_email(message: EmailMessage, *, source_ref: str) -> list[ParsedNotification]:
    """Turn one LinkedIn notification email into zero or more notifications.

    `source_ref` identifies the email (a Gmail message id in production). A
    digest yields several notifications, each given a distinct `source_ref` by
    appending an index — otherwise the unique index on the drafts table would
    collapse three new connections into one card.

    Never raises on a malformed email: an unparseable notification degrades to
    `other`, which shows David nothing rather than showing him nonsense.
    """
    notification_type, template = classify(message)
    subject = _subject(message)
    received_at = _received_at(message)

    base = {
        "type": notification_type,
        "subject": subject,
        "received_at": received_at,
        "template": template,
    }

    if notification_type is NotificationType.OTHER:
        return [ParsedNotification(source_ref=source_ref, **base)]

    selector = SELECTORS.get(template or "")
    if selector is None:
        return [ParsedNotification(source_ref=source_ref, **{**base, "type": NotificationType.OTHER})]

    lines = _plain_lines(message)
    try:
        extracted = selector(message, lines)
    except Exception:
        # A selector blowing up means LinkedIn changed a format. Log it loudly
        # and treat the email as noise rather than failing the whole poll.
        logger.exception(
            "Could not read a LinkedIn %s email (template: %s). Treating it as noise — "
            "this usually means LinkedIn changed the email format.",
            notification_type.value,
            template,
        )
        return [ParsedNotification(source_ref=source_ref, **{**base, "type": NotificationType.OTHER})]

    # A selector that found nothing means the format moved under us.
    if not extracted:
        logger.warning(
            "A LinkedIn %s email (template: %s) had nothing I could read in it. "
            "Subject was %r.",
            notification_type.value,
            template,
            subject,
        )
        return [ParsedNotification(source_ref=source_ref, **{**base, "type": NotificationType.OTHER})]

    results: list[ParsedNotification] = []
    multiple = len(extracted) > 1
    for index, fields in enumerate(extracted):
        ref = f"{source_ref}#{index}" if multiple else source_ref
        results.append(ParsedNotification(source_ref=ref, **base, **fields))

    # An actionable card with no name or nowhere to go is worse than no card.
    usable = [item for item in results if not item.is_actionable or (item.person_name and item.linkedin_url)]
    dropped = len(results) - len(usable)
    if dropped:
        logger.warning(
            "Ignored %s item(s) from a LinkedIn %s email — no name or no link to open. "
            "Template: %s.",
            dropped,
            notification_type.value,
            template,
        )
    return usable or [ParsedNotification(source_ref=source_ref, **{**base, "type": NotificationType.OTHER})]
