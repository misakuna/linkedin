"""The inbound half of the text-message approval loop.

What: one endpoint, `POST /sms/incoming`, that Twilio calls when David texts
back. It reads YES or NO and moves the waiting post.

Why this file is the most security-sensitive in the project: it is the only
unauthenticated route in the app, and the only one that can cause something to
be published under David's name. Every other write path sits behind a session
cookie. This one cannot — Twilio has no login — so the request signature is the
authentication, and it is checked before anything is read.

The failure to avoid: an attacker who finds this URL posting `Body=YES` and
publishing whatever is sitting in David's approval queue. That would break the
one guarantee this whole build exists to provide, and
it would look exactly like David having approved it.

So this module fails closed, every time:

  * no signature            -> 403, nothing read
  * bad signature           -> 403, nothing read
  * no auth token to check  -> 503, nothing read
  * not David's number      -> handled inside ApprovalService, which ignores it

How: Twilio signs the exact request URL concatenated with its POST parameters,
sorted by key, using the account auth token as an HMAC-SHA1 key. We recompute
that and compare in constant time. The form body is parsed by hand rather than
via `request.form()` so there is no dependency on python-multipart and so the
bytes used for verification are exactly the bytes that arrived.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request, Response

from app import env_secrets
from app.logging_setup import get_logger

logger = get_logger(__name__)

router = APIRouter()

# Twilio wants TwiML back. An empty <Response/> means "say nothing"; a
# <Message> is texted straight back to David.
TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'


def public_url(request: Request) -> str:
    """The URL as Twilio saw it, which is what Twilio signed.

    Railway terminates TLS and forwards over plain HTTP, so `request.url` reads
    http://<internal-host>/... and would never match the https://<public-host>/
    that Twilio hashed. Rebuilding from the forwarded headers is the only way
    the signature can verify in production.
    """
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    scheme = proto or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    url = f"{scheme}://{host}{request.url.path}"
    return f"{url}?{request.url.query}" if request.url.query else url


def signature_is_valid(auth_token: str, url: str, params: dict[str, str], signature: str) -> bool:
    """Whether this request really came from Twilio.

    Twilio's scheme: take the full URL, append every POST parameter as key then
    value, in order sorted by key, HMAC-SHA1 the result with the account auth
    token, base64 the digest.
    """
    payload = url + "".join(key + params[key] for key in sorted(params))
    digest = hmac.new(
        auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1
    ).digest()
    expected = base64.b64encode(digest).decode("ascii")
    # Constant-time: a naive compare leaks the signature one byte at a time.
    return hmac.compare_digest(expected, signature or "")


def _twiml(message: str = "") -> Response:
    body = f"<Message>{message}</Message>" if message else ""
    return Response(content=TWIML.format(body=body), media_type="application/xml")


@router.post("/sms/incoming", include_in_schema=False)
async def incoming_sms(request: Request) -> Response:
    """David texted back. Approve or skip the post that's waiting on him."""
    try:
        auth_token = env_secrets.sms_credentials().auth_token
    except Exception:
        # Unconfigured means unverifiable, and unverifiable means we must not
        # act. Refusing is the safe direction: the post stays in the queue and
        # David can still approve it on the dashboard.
        logger.error(
            "An inbound text arrived but SMS_AUTH_TOKEN isn't set, so it couldn't be "
            "verified as coming from Twilio. Ignored it."
        )
        return Response(status_code=503)

    raw = await request.body()
    params = dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True))

    signature = request.headers.get("x-twilio-signature", "")
    if not signature_is_valid(auth_token, public_url(request), params, signature):
        logger.warning(
            "Rejected an inbound text whose signature didn't verify. If David's "
            "replies have stopped working, check that the Twilio webhook URL "
            "matches this service's public URL exactly."
        )
        return Response(status_code=403)

    body = params.get("Body", "")
    from_number = params.get("From")

    services = request.app.state.services

    # A verified signature says Twilio sent this once. It does not say we are
    # seeing it for the first time: the signature covers only the URL and the
    # parameters, so the same bytes verify forever. Since `handle_reply` acts on
    # whichever post is oldest, a replayed YES would walk the queue and publish
    # everything waiting. The MessageSid makes each request single-use.
    message_sid = params.get("MessageSid") or params.get("SmsMessageSid")
    if not message_sid:
        logger.warning(
            "An inbound text verified but carried no MessageSid, so it can't be "
            "checked for replay. Ignored it."
        )
        return Response(status_code=400)
    if not services.alerts.claim_inbound_message(message_sid):
        logger.warning(
            "Ignored a repeat of a text already acted on (MessageSid %s). Twilio "
            "retries on timeout, so this is usually harmless — but an unexpected "
            "repeat is what a replayed approval would look like.",
            message_sid,
        )
        # 200 so Twilio stops retrying; the first copy was already handled.
        return _twiml("Already got that one.")

    try:
        outcome = services.approvals.handle_reply(body, from_number=from_number)
        # Deliberately not PostingService.handle_sms_reply: that returns only
        # whether a post got scheduled, and David deserves a different reply for
        # "skipped it" than for "I didn't understand you".
        if outcome.approved and outcome.draft:
            services.posting.publish(outcome.draft.id)
    except Exception:
        logger.exception("Couldn't act on David's text.")
        return _twiml("Something didn't work — text support.")

    if not outcome.understood:
        if outcome.note == "not David's number":
            # Say nothing at all to a stranger. A helpful error is a hint.
            return _twiml()
        return _twiml("Sorry, I didn't catch that. Reply YES to post it, or NO to skip it.")

    if outcome.draft is None:
        return _twiml("Nothing's waiting for your OK right now.")

    if outcome.approved:
        return _twiml("Got it — that's scheduled to go out. Nothing else for you to do.")
    return _twiml("No problem, I've skipped that one.")
