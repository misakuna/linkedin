"""One-time script: mint the Gmail refresh token.

What: opens Google's consent screen in a browser, and prints the refresh token
to paste into .env as GMAIL_REFRESH_TOKEN.

Why: the server never does OAuth at runtime — it holds a long-lived refresh
token and exchanges it for access tokens as needed. That token is created once,
by the person whose mailbox it is, in their own browser. This script is that
one time.

Who runs it: whoever owns the mailbox being read. If the inbox is David's, he
runs it (or does it on a screen-share). If you move to the forwarding model,
you run it against the developer's own mailbox and David never touches OAuth at all.

Usage:
    GMAIL_CLIENT_ID=... GMAIL_CLIENT_SECRET=... .venv/bin/python scripts/get_gmail_token.py

or just put those two in .env first and run:
    .venv/bin/python scripts/get_gmail_token.py

The scope requested is gmail.readonly and nothing else — the same constant the
server uses, imported rather than retyped so the two can't drift apart.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from app.inbox.gmail_client import GMAIL_SCOPES  # noqa: E402

# The loopback port the consent screen redirects back to. For a "Web
# application" client this exact URL must be registered in Google Cloud as an
# authorized redirect URI. For a "Desktop app" client, nothing to register.
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/"


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

    client_id = os.environ.get("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        print(
            "GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET must be set — put them in .env\n"
            "or pass them on the command line. Both come from the OAuth client you\n"
            "created in Google Cloud Console."
        )
        return 1

    from google_auth_oauthlib.flow import InstalledAppFlow

    # "installed" is the Desktop-app shape. A Web-application client works the
    # same way as long as REDIRECT_URI is registered against it.
    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }
    }

    print(f"Asking Google for: {', '.join(GMAIL_SCOPES)}")
    print("A browser window will open. Sign in as the person whose mailbox this is.\n")

    flow = InstalledAppFlow.from_client_config(client_config, scopes=list(GMAIL_SCOPES))

    try:
        credentials = flow.run_local_server(
            port=REDIRECT_PORT,
            # Both matter. Without access_type=offline Google returns no refresh
            # token at all; without prompt=consent it returns one only on the
            # very first authorization, so a second run would print None.
            access_type="offline",
            prompt="consent",
            open_browser=True,
        )
    except Exception as exc:
        print(f"\nThat didn't work: {exc}\n")
        print(
            "If the error mentions redirect_uri_mismatch, your OAuth client is the\n"
            f'"Web application" type and needs {REDIRECT_URI} added under\n'
            '"Authorized redirect URIs". A "Desktop app" client needs nothing added.'
        )
        return 1

    if not credentials.refresh_token:
        print(
            "\nGoogle didn't return a refresh token. That usually means this account\n"
            "has already authorized this client. Remove it at\n"
            "https://myaccount.google.com/permissions and run this again."
        )
        return 1

    env_path = Path(__file__).resolve().parent.parent / ".env"
    if _write_refresh_token(env_path, credentials.refresh_token):
        print(f"\nWrote GMAIL_REFRESH_TOKEN into {env_path}.")
        print("Check it with:  .venv/bin/python scripts/check_gmail.py")
        print("Then set integrations.gmail to 'live' in config.yaml.")
        return 0

    # Couldn't write it — fall back to printing, but say what it is first.
    print(
        f"\nCouldn't update {env_path}, so here it is. This is a long-lived\n"
        "credential: treat it like a password, and don't leave it in scrollback.\n"
    )
    print(f"GMAIL_REFRESH_TOKEN={credentials.refresh_token}")
    return 0


def _write_refresh_token(env_path: Path, token: str) -> bool:
    """Put the token straight into .env rather than through a copy-paste.

    Printing it would leave a long-lived credential in terminal scrollback and
    in whatever captures that terminal. Writing it also removes the transcription
    step, which is the likeliest place for a working token to get subtly mangled.

    Rewrites the existing GMAIL_REFRESH_TOKEN line in place so the file's
    comments and ordering survive. Returns False if the file can't be read or
    written, so the caller can fall back to printing.
    """
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return False

    replacement = f'GMAIL_REFRESH_TOKEN="{token}"\n'
    for index, line in enumerate(lines):
        if line.lstrip().startswith("GMAIL_REFRESH_TOKEN="):
            lines[index] = replacement
            break
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(replacement)

    try:
        env_path.write_text("".join(lines), encoding="utf-8")
    except OSError:
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
