"""Check that the Gmail credentials actually work. Reads nothing into the database.

What: connects to Gmail with whatever is in .env, fetches LinkedIn notification
emails, and reports what the parser makes of them.

Why: the four GMAIL_* values are fiddly to obtain and easy to get subtly wrong —
a token minted for the wrong account, a scope that wasn't granted, a query that
matches nothing. This answers "did that work?" without touching the database or
drafting anything.

Usage:
    .venv/bin/python scripts/check_gmail.py
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import PROJECT_ROOT, load_config  # noqa: E402
from app.inbox.gmail_client import GmailReader  # noqa: E402
from app.inbox.parser import parse_email  # noqa: E402


def _authorized_mailbox() -> str | None:
    """Which mailbox the refresh token actually belongs to, per Google.

    Reuses GmailReader's own service so this tests the shipped auth path rather
    than a parallel one. Returns None and explains itself on the two failures
    that are otherwise cryptic — a disabled API and a rejected token.
    """
    from googleapiclient.errors import HttpError

    try:
        service = GmailReader(load_config(PROJECT_ROOT / "config.yaml"))._get_service()
        profile = service.users().getProfile(userId="me").execute()
    except HttpError as exc:
        status = getattr(exc.resp, "status", None)
        detail = str(exc)
        if status == 403 and "accessNotConfigured" in detail:
            project = "".join(c for c in detail.split("project ")[-1][:20] if c.isdigit())
            print(
                "The Gmail API is not enabled on this Google Cloud project"
                f"{' (' + project + ')' if project else ''}.\n\n"
                "  APIs & Services > Library > search 'Gmail API' > Enable\n\n"
                "Nothing else is wrong — the credentials themselves reached Google.\n"
                "Enabling it takes a minute or two to propagate, then re-run this."
            )
        elif status in (400, 401):
            print(
                "Google rejected the refresh token. Usual causes: it was minted for a\n"
                "different OAuth client than the one now in .env, or it was revoked.\n"
                "Re-run scripts/get_gmail_token.py."
            )
        else:
            print(f"Gmail returned an error checking which mailbox this is: {exc}")
        return None
    except Exception as exc:
        print(f"Couldn't reach Gmail to check which mailbox this is: {exc}")
        return None

    return profile.get("emailAddress")


def main() -> int:
    config = load_config(PROJECT_ROOT / "config.yaml")

    # Check the four values first. The server deliberately swallows this into a
    # log line and carries on, which is right for a scheduled job and useless
    # when someone is sitting here trying to get set up.
    from app import env_secrets

    missing = [
        name
        for name in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_USER")
        if not __import__("os").environ.get(name, "").strip()
    ]
    if missing:
        print("Not set in .env yet:\n")
        for name in missing:
            print(f"  {name}")
        print("\nSee .env.example for where each one comes from.")
        return 1

    creds = env_secrets.gmail_credentials()
    print(f"GMAIL_USER    : {creds.user}   (label only — the code never reads it)")
    print(f"Mailbox query : {config.inbox.gmail_query}")
    print(f"config.yaml   : integrations.gmail = {config.integrations.gmail}")
    if config.integrations.gmail != "live":
        print(
            "\nNote: config.yaml still says 'mock'. Connecting live anyway for this "
            "check —\nflip it to 'live' once this passes."
        )
    print()

    # Ask Google which mailbox the token actually belongs to, before anything
    # else. This is the one question GMAIL_USER cannot answer: every API call
    # uses userId="me", so the mailbox read is decided purely by whose account
    # signed the consent screen. Authorizing the wrong account produces a token
    # that works perfectly against a mailbox nobody intended to read.
    authorized = _authorized_mailbox()
    if not authorized:
        # It already explained itself. Fetching now would fail the same way and
        # end on "no emails came back", which reads like an empty mailbox
        # rather than the real problem.
        return 1

    print(f"Token belongs to: {authorized}")
    if creds.user and authorized.lower() != creds.user.strip().lower():
        print(
            f"  ⚠️  GMAIL_USER says {creds.user}. Only the line above is real.\n"
            "      If that is not the mailbox David forwards INTO, re-run\n"
            "      scripts/get_gmail_token.py signed in as the right account."
        )
    print()

    # Always the real reader here; the point is to test the credentials.
    emails = GmailReader(config).fetch(
        config.inbox.gmail_query, config.inbox.max_messages_per_poll
    )

    if not emails:
        print(
            "\nNo emails came back. Either the credentials aren't working (look for an\n"
            "error above), or the mailbox genuinely has no LinkedIn mail matching\n"
            f"  {config.inbox.gmail_query}\n"
            "Try widening it, e.g. newer_than:30d."
        )
        return 1

    kinds: Counter[str] = Counter()
    people: list[str] = []
    with_template = 0
    forwarded = 0
    for email in emails:
        if email.message.get("X-LinkedIn-Template"):
            with_template += 1
        if email.message.get("X-Forwarded-For") or email.message.get("X-Forwarded-To"):
            forwarded += 1
        for item in parse_email(email.message, source_ref=email.message_id):
            kinds[item.type.value] += 1
            if item.is_actionable and item.person_name:
                people.append(item.person_name)

    # Classification keys off X-LinkedIn-Template. If forwarding strips it,
    # every email silently becomes "other" and David is shown nothing at all —
    # so check for it explicitly rather than inferring from a low card count.
    print(f"\n{forwarded} of {len(emails)} arrived by forwarding.")
    if with_template == 0:
        print(
            "\n!! None of these carry an X-LinkedIn-Template header, which is what\n"
            "   classification depends on. Everything will be treated as noise and\n"
            "   David will see nothing. If these were forwarded, the forwarding is\n"
            "   stripping the header — flag it — the parser needs a fallback."
        )
    elif with_template < len(emails):
        print(
            f"{with_template} of {len(emails)} carry the X-LinkedIn-Template header "
            "(the rest are LinkedIn marketing, which never has one)."
        )
    else:
        print("All of them carry the X-LinkedIn-Template header — classification is solid.")

    print(f"\nRead {len(emails)} email(s). The parser found:\n")
    for kind, count in kinds.most_common():
        print(f"  {count:3d}  {kind}")

    actionable = sum(v for k, v in kinds.items() if k != "other")
    print(f"\n{actionable} would become a card for David.")
    if people:
        print("From: " + ", ".join(dict.fromkeys(people[:10])))

    print("\nGmail is working. Nothing was written to the database.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
