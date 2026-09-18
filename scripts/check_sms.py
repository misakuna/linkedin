"""Check the Twilio setup without texting anybody.

What: verifies the credentials, the sending number, and — the part that is
otherwise invisible — whether the inbound webhook is pointed at this service.

Why: SMS is the only integration that costs money and interrupts a real person,
so "just try it and see" is the wrong debugging loop. Everything here is a read.
Sending an actual text needs --send and is never the default.

The webhook check is the valuable one. If David's "YES" isn't working there are
two indistinguishable causes — the number has no webhook configured, or it has
one pointing somewhere else — and neither produces an error anywhere David or
the developer would see. Twilio just posts into the void, or into the wrong service.

Usage:
    .venv/bin/python scripts/check_sms.py           # read-only
    .venv/bin/python scripts/check_sms.py --send    # actually texts David
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from app import env_secrets  # noqa: E402
from app.approvals.sms import TWILIO_API_ROOT  # noqa: E402
from app.config import PROJECT_ROOT, load_config  # noqa: E402

WEBHOOK_PATH = "/sms/incoming"


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    import httpx

    config = load_config(PROJECT_ROOT / "config.yaml")

    try:
        creds = env_secrets.sms_credentials()
    except env_secrets.MissingSecretError as exc:
        print(f"{exc}\n\nSet SMS_ACCOUNT_SID, SMS_AUTH_TOKEN and SMS_FROM_NUMBER in .env.")
        return 1

    auth = (creds.account_sid, creds.auth_token)
    print(f"Account SID   : {creds.account_sid[:8]}…")
    print(f"Sending from  : {creds.from_number}")
    print(f"Texting David : {config.approvals.phone_number}")
    print(f"config.yaml   : integrations.sms = {config.integrations.sms}")
    print()

    # 1. Do the credentials work at all? Fetching the account sends nothing.
    try:
        response = httpx.get(
            f"{TWILIO_API_ROOT}/Accounts/{creds.account_sid}.json", auth=auth, timeout=30.0
        )
    except Exception as exc:
        print(f"Couldn't reach Twilio: {exc}")
        return 1

    if response.status_code == 401:
        print("Twilio rejected the credentials. Check SMS_ACCOUNT_SID and SMS_AUTH_TOKEN.")
        return 1
    if response.status_code != 200:
        print(f"Twilio returned {response.status_code} for the account lookup.")
        return 1

    account = response.json()
    status = account.get("status", "?")
    print(f"Credentials work. Account is '{account.get('friendly_name', '?')}' ({status}).")

    trial = "trial" in str(account.get("type", "")).lower() or status == "trial"
    if trial:
        print(
            "\n  ⚠️  This is a TRIAL account. It can only text numbers you have\n"
            "      verified in the console, and every message is prefixed with\n"
            "      'Sent from your Twilio trial account'. That prefix will appear\n"
            "      in David's texts. Upgrade before he relies on this."
        )

    # 2. Is the sending number real, and does it have a webhook?
    try:
        numbers = httpx.get(
            f"{TWILIO_API_ROOT}/Accounts/{creds.account_sid}/IncomingPhoneNumbers.json",
            auth=auth,
            params={"PhoneNumber": creds.from_number},
            timeout=30.0,
        ).json()
    except Exception as exc:
        print(f"\nCouldn't list your Twilio numbers: {exc}")
        return 1

    owned = numbers.get("incoming_phone_numbers") or []
    if not owned:
        print(
            f"\n{creds.from_number} isn't a number on this account. Check "
            "SMS_FROM_NUMBER matches one you bought."
        )
        return 1

    number = owned[0]
    capabilities = number.get("capabilities") or {}
    print(f"\nNumber found. SMS capable: {capabilities.get('sms', False)}")
    if not capabilities.get("sms"):
        print("  ⚠️  This number can't send SMS. Buy one with SMS capability.")

    sms_url = (number.get("sms_url") or "").strip()
    print(f"Inbound webhook: {sms_url or '(none set)'}")

    if not sms_url:
        print(
            "\n  ⚠️  No inbound webhook, so David's YES will go nowhere. The text\n"
            "      becomes notify-only and he'd have to open the dashboard.\n"
            "      Twilio console > Phone Numbers > this number > Messaging >\n"
            "      'A MESSAGE COMES IN':  POST  https://<your-domain>/sms/incoming"
        )
    elif not sms_url.endswith(WEBHOOK_PATH):
        print(
            f"\n  ⚠️  That webhook doesn't end in {WEBHOOK_PATH}, so it isn't pointing\n"
            "      at this service. David's replies are going somewhere else."
        )
    elif not sms_url.startswith("https://"):
        print(
            "\n  ⚠️  The webhook is http, not https. The signature is computed over\n"
            "      the exact URL Twilio calls, so it won't verify and every reply\n"
            "      will be rejected with a 403."
        )
    else:
        print("  Webhook looks right.")

    method = (number.get("sms_method") or "").upper()
    if method and method != "POST":
        print(f"\n  ⚠️  The webhook method is {method}; the endpoint expects POST.")

    # 3. Only now, and only if asked.
    if "--send" in sys.argv:
        print(f"\nTexting {config.approvals.phone_number} for real…")
        from app.approvals.sms import TwilioSmsSender

        ok = TwilioSmsSender(config).send(
            to=config.approvals.phone_number,
            body="Test from your LinkedIn assistant. Nothing to do — just checking it works.",
        )
        print("  sent." if ok else "  failed — see the logged reason above.")
        return 0 if ok else 1

    print("\nNothing was sent. Re-run with --send to text David for real.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
