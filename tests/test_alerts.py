"""Texting David — the nudges, and the inbound reply that publishes.

Two very different risks are covered here.

The alerts are a product risk: text him too often and he mutes the number, and
a muted number is a cancelled engagement. So most of these tests are about the
system deciding NOT to send.

The webhook is a security risk. It is the only unauthenticated write path in
the app and the only one that can cause a post to go out under David's name.
Its tests are about refusing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from uuid import uuid4

import pytest

from app.approvals.alerts import DAILY_DIGEST, NEW_ITEMS, AlertService, _people_phrase
from app.approvals.webhook import public_url, signature_is_valid
from app.config import AppConfig
from app.db import DraftKind, DraftRepository, DraftStatus
from app.db.alerts_repository import AlertsRepository

# 4pm Pacific — comfortably outside the 21:00-07:00 quiet window.
AFTERNOON = datetime(2026, 8, 4, 23, 0, tzinfo=timezone.utc)
# 2am Pacific — inside it.
MIDDLE_OF_THE_NIGHT = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)


class RecordingSender:
    """Captures texts instead of sending them, and can pretend to fail."""

    def __init__(self, works: bool = True) -> None:
        self.works = works
        self.sent: list[tuple[str, str]] = []

    def send(self, *, to: str, body: str) -> bool:
        if not self.works:
            return False
        self.sent.append((to, body))
        return True


@pytest.fixture
def sender() -> RecordingSender:
    return RecordingSender()


@pytest.fixture
def alerts_repo(db_path) -> AlertsRepository:
    return AlertsRepository(db_path)


@pytest.fixture
def alerts(config: AppConfig, repository: DraftRepository, alerts_repo, sender) -> AlertService:
    return AlertService(config, repository, alerts_repo, sender=sender)


def add_waiting(repository: DraftRepository, name: str) -> None:
    repository.create(
        kind=DraftKind.REPLY,
        body=f"Hello {name.split()[0]}.",
        person_name=name,
        notification_type="message_received",
        linkedin_url="https://www.linkedin.com/messaging/",
    )


# -- how it reads ----------------------------------------------------------


@pytest.mark.parametrize(
    ("names", "total", "expected"),
    [
        (["Karen Mills"], 1, "Karen"),
        (["Karen Mills", "Bernard Lim"], 2, "Karen and Bernard"),
        (["Karen Mills", "Bernard Lim", "Nina Nguyen"], 3, "Karen, Bernard and Nina"),
        (["Karen Mills", "Bernard Lim", "Nina Nguyen"], 6, "Karen, Bernard and 4 more"),
        ([], 3, "3 people"),
        ([], 1, "1 person"),
    ],
)
def test_it_names_people_rather_than_counting_them(names, total, expected) -> None:
    """"Karen is waiting" tells David whether to care. "1 item" doesn't."""
    assert _people_phrase(names, total) == expected


def test_the_text_says_who_is_waiting(
    alerts: AlertService, repository: DraftRepository, sender: RecordingSender,
    config: AppConfig,
) -> None:
    """Asserts the sentence, not the whole body.

    The dashboard link is appended when alerts.dashboard_url is set, so an
    exact-equality assertion here broke the moment the Railway domain was
    configured. Same shape of mistake as hardcoding the phone number.
    """
    add_waiting(repository, "Karen Mills")
    add_waiting(repository, "Bernard Lim")

    assert alerts.notify_new_items(now=AFTERNOON) is True
    _, body = sender.sent[0]
    assert body.splitlines()[0] == "Karen and Bernard are waiting on you on LinkedIn."
    if config.alerts.dashboard_url:
        assert config.alerts.dashboard_url in body


def test_no_jargon_reaches_davids_phone(
    alerts: AlertService, repository: DraftRepository, sender: RecordingSender
) -> None:
    add_waiting(repository, "Karen Mills")
    alerts.notify_new_items(now=AFTERNOON)

    body = sender.sent[0][1].lower()
    for word in ("draft", "pending", "status", "api", "sync", "queue", "item"):
        assert word not in body


# -- when it stays quiet ---------------------------------------------------


def test_nothing_waiting_means_no_text(alerts: AlertService, sender: RecordingSender) -> None:
    assert alerts.notify_new_items(now=AFTERNOON) is False
    assert sender.sent == []


def test_quiet_hours_are_respected(
    alerts: AlertService, repository: DraftRepository, sender: RecordingSender
) -> None:
    """Nothing this system does is worth a 2am buzz."""
    add_waiting(repository, "Karen Mills")
    assert alerts.notify_new_items(now=MIDDLE_OF_THE_NIGHT) is False
    assert sender.sent == []


def test_a_burst_of_notifications_is_one_text(
    alerts: AlertService, repository: DraftRepository, sender: RecordingSender
) -> None:
    """LinkedIn sends in bursts. Without the throttle this is a text each."""
    add_waiting(repository, "Karen Mills")
    assert alerts.notify_new_items(now=AFTERNOON) is True

    add_waiting(repository, "Bernard Lim")
    assert alerts.notify_new_items(now=AFTERNOON + timedelta(minutes=15)) is False

    add_waiting(repository, "Nina Nguyen")
    assert alerts.notify_new_items(now=AFTERNOON + timedelta(minutes=30)) is False

    assert len(sender.sent) == 1


def test_the_throttle_lifts_after_the_configured_gap(
    alerts: AlertService, repository: DraftRepository, sender: RecordingSender, config: AppConfig
) -> None:
    add_waiting(repository, "Karen Mills")
    alerts.notify_new_items(now=AFTERNOON)

    later = AFTERNOON + timedelta(minutes=config.alerts.new_items_min_minutes + 1)
    # Still inside waking hours, so the only thing under test is the throttle.
    assert later.astimezone(config.client.tz).hour < 21
    assert alerts.notify_new_items(now=later) is True
    assert len(sender.sent) == 2


def test_the_throttle_survives_a_restart(
    config: AppConfig, repository: DraftRepository, alerts_repo, sender: RecordingSender
) -> None:
    """A Railway redeploy must not re-text him. This is why it's in the DB."""
    add_waiting(repository, "Karen Mills")
    AlertService(config, repository, alerts_repo, sender=sender).notify_new_items(now=AFTERNOON)

    fresh = AlertService(config, repository, alerts_repo, sender=RecordingSender())
    assert fresh.notify_new_items(now=AFTERNOON + timedelta(minutes=5)) is False


def test_a_failed_text_does_not_start_the_throttle(
    config: AppConfig, repository: DraftRepository, alerts_repo
) -> None:
    """Otherwise a Twilio blip silences him for the whole throttle window."""
    add_waiting(repository, "Karen Mills")
    broken = RecordingSender(works=False)

    assert AlertService(config, repository, alerts_repo, sender=broken).notify_new_items(
        now=AFTERNOON
    ) is False
    assert alerts_repo.last_sent(NEW_ITEMS) is None

    working = RecordingSender()
    assert AlertService(config, repository, alerts_repo, sender=working).notify_new_items(
        now=AFTERNOON + timedelta(minutes=1)
    ) is True


def test_switching_new_item_alerts_off_stops_them(
    config: AppConfig, repository: DraftRepository, alerts_repo, sender: RecordingSender
) -> None:
    add_waiting(repository, "Karen Mills")
    off = config.model_copy(
        update={"alerts": config.alerts.model_copy(update={"on_new_items": False})}
    )
    assert AlertService(off, repository, alerts_repo, sender=sender).notify_new_items(
        now=AFTERNOON
    ) is False


# -- the daily digest ------------------------------------------------------


def test_the_digest_says_what_is_waiting(
    alerts: AlertService, repository: DraftRepository, sender: RecordingSender
) -> None:
    add_waiting(repository, "Karen Mills")
    add_waiting(repository, "Bernard Lim")

    assert alerts.send_daily_digest(now=AFTERNOON) is True
    assert "Karen and Bernard are waiting" in sender.sent[0][1]


def test_the_digest_stays_silent_when_there_is_nothing_to_say(
    alerts: AlertService, sender: RecordingSender
) -> None:
    """A daily "all clear" is the fastest way to teach someone to ignore you."""
    assert alerts.send_daily_digest(now=AFTERNOON) is False
    assert sender.sent == []


def test_the_digest_ignores_work_david_has_finished(
    alerts: AlertService, repository: DraftRepository, sender: RecordingSender
) -> None:
    add_waiting(repository, "Karen Mills")
    done = repository.create(
        kind=DraftKind.REPLY, body="Thanks.", person_name="Bernard Lim",
        notification_type="mention", linkedin_url="https://www.linkedin.com/x",
    )
    repository.transition(done.id, DraftStatus.DONE, note="handled")

    alerts.send_daily_digest(now=AFTERNOON)
    body = sender.sent[0][1]
    assert "Karen" in body and "Bernard" not in body


def test_the_digest_and_the_nudge_throttle_separately(
    alerts: AlertService, repository: DraftRepository, alerts_repo
) -> None:
    """The digest is the safety net; a recent nudge must not suppress it."""
    add_waiting(repository, "Karen Mills")
    alerts.notify_new_items(now=AFTERNOON)
    assert alerts.send_daily_digest(now=AFTERNOON + timedelta(minutes=1)) is True
    assert alerts_repo.last_sent(NEW_ITEMS) is not None
    assert alerts_repo.last_sent(DAILY_DIGEST) is not None


def test_the_digest_can_be_switched_off(
    config: AppConfig, repository: DraftRepository, alerts_repo, sender: RecordingSender
) -> None:
    add_waiting(repository, "Karen Mills")
    off = config.model_copy(
        update={"alerts": config.alerts.model_copy(update={"daily_digest_at": None})}
    )
    assert AlertService(off, repository, alerts_repo, sender=sender).send_daily_digest(
        now=AFTERNOON
    ) is False


def test_the_dashboard_link_is_included_when_configured(
    config: AppConfig, repository: DraftRepository, alerts_repo, sender: RecordingSender
) -> None:
    add_waiting(repository, "Karen Mills")
    linked = config.model_copy(
        update={
            "alerts": config.alerts.model_copy(
                update={"dashboard_url": "https://linkedai.up.railway.app"}
            )
        }
    )
    AlertService(linked, repository, alerts_repo, sender=sender).send_daily_digest(now=AFTERNOON)
    assert "https://linkedai.up.railway.app" in sender.sent[0][1]


# -- quiet hours arithmetic ------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "quiet"),
    [(22, True), (23, True), (0, True), (3, True), (6, True), (7, False), (12, False), (20, False)],
)
def test_quiet_hours_wrap_around_midnight(config: AppConfig, hour: int, quiet: bool) -> None:
    """21:00-07:00 spans midnight, which naive comparison gets backwards."""
    from datetime import time

    assert config.alerts.quiet_hours.covers(time(hour, 30)) is quiet


# -- the webhook: signature ------------------------------------------------

TOKEN = "test_auth_token"
URL = "https://linkedai.up.railway.app/sms/incoming"


def sign(url: str, params: dict[str, str], token: str = TOKEN) -> str:
    payload = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(token.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def test_a_genuine_twilio_signature_verifies() -> None:
    params = {"Body": "YES", "From": "+15555550100", "MessageSid": "SM123"}
    assert signature_is_valid(TOKEN, URL, params, sign(URL, params)) is True


def test_a_tampered_body_fails() -> None:
    """The attack: intercept a real NO and change it to YES."""
    params = {"Body": "NO", "From": "+15555550100"}
    signature = sign(URL, params)
    assert signature_is_valid(TOKEN, URL, {**params, "Body": "YES"}, signature) is False


def test_a_forged_signature_fails() -> None:
    params = {"Body": "YES", "From": "+15555550100"}
    assert signature_is_valid(TOKEN, URL, params, "not-a-signature") is False
    assert signature_is_valid(TOKEN, URL, params, "") is False


def test_the_wrong_auth_token_fails() -> None:
    params = {"Body": "YES", "From": "+15555550100"}
    assert signature_is_valid(TOKEN, URL, params, sign(URL, params, "other_token")) is False


def test_a_signature_for_a_different_url_fails() -> None:
    """Replaying a signed request against another deployment must not work."""
    params = {"Body": "YES", "From": "+15555550100"}
    other = "https://someone-elses-app.up.railway.app/sms/incoming"
    assert signature_is_valid(TOKEN, URL, params, sign(other, params)) is False


def test_parameter_order_does_not_matter() -> None:
    """Twilio sorts by key; a dict in another order must still verify."""
    params = {"Body": "YES", "From": "+1555", "AccountSid": "AC1"}
    signature = sign(URL, params)
    shuffled = {"From": "+1555", "AccountSid": "AC1", "Body": "YES"}
    assert signature_is_valid(TOKEN, URL, shuffled, signature) is True


# -- the webhook: URL reconstruction ---------------------------------------


class FakeRequest:
    def __init__(self, headers: dict[str, str], scheme="http", netloc="internal:8080",
                 path="/sms/incoming", query=""):
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.url = type("U", (), {
            "scheme": scheme, "netloc": netloc, "path": path, "query": query
        })()


def test_the_signed_url_is_rebuilt_from_the_proxy_headers() -> None:
    """Railway forwards over http, so request.url alone never matches."""
    request = FakeRequest(
        {"x-forwarded-proto": "https", "host": "linkedai.up.railway.app"}
    )
    assert public_url(request) == "https://linkedai.up.railway.app/sms/incoming"


def test_the_query_string_is_part_of_the_signed_url() -> None:
    request = FakeRequest(
        {"x-forwarded-proto": "https", "host": "linkedai.up.railway.app"}, query="a=1"
    )
    assert public_url(request) == "https://linkedai.up.railway.app/sms/incoming?a=1"


def test_without_proxy_headers_it_falls_back_to_the_request() -> None:
    request = FakeRequest({}, scheme="http", netloc="127.0.0.1:8000")
    assert public_url(request) == "http://127.0.0.1:8000/sms/incoming"


# -- the webhook: end to end -----------------------------------------------


@pytest.fixture
def twilio(app_client, monkeypatch):
    """An app_client whose SMS auth token is known, so requests can be signed."""
    from app import env_secrets

    monkeypatch.setattr(
        env_secrets,
        "sms_credentials",
        lambda: env_secrets.SmsCredentials(
            account_sid="AC_test", auth_token=TOKEN, from_number="+15550000000"
        ),
    )
    return app_client


def post_sms(
    client, body: str, from_number: str, *, signature: str | None = None,
    message_sid: str | None = None,
):
    """Post a form-encoded message the way Twilio does.

    The signature is computed over the DECODED values — that is what Twilio
    hashes and what parse_qsl hands the endpoint. Signing the percent-encoded
    form instead produces a valid-looking signature that never verifies.

    Each call gets a fresh MessageSid unless one is passed, because the endpoint
    refuses ids it has already acted on. Pass the same id twice to exercise that
    deliberately.
    """
    params = {
        "Body": body,
        "From": from_number,
        "MessageSid": message_sid or f"SM_test_{uuid4().hex}",
    }
    url = "http://testserver/sms/incoming"
    return client.post(
        "/sms/incoming",
        content=urlencode(params),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Twilio-Signature": signature if signature is not None else sign(url, params),
        },
    )


DAVIDS_NUMBER = "+18056519665"


def test_an_unsigned_request_is_refused(twilio) -> None:
    """The whole point. Without this, anyone with the URL can publish."""
    assert post_sms(twilio, "YES", "+15555550100", signature="").status_code == 403


def test_a_forged_request_is_refused(twilio) -> None:
    assert post_sms(twilio, "YES", "+15555550100", signature="AAAA").status_code == 403


def test_a_forged_yes_cannot_publish_a_waiting_post(twilio) -> None:
    """The attack in full: a stranger approving David's queued post."""
    from app.main import app

    repo = app.state.repository
    post = repo.create(kind=DraftKind.POST, body="Not yet approved.", source_ref="post:x")
    repo.transition(post.id, DraftStatus.PENDING_APPROVAL)

    assert post_sms(twilio, "YES", "+15555550100", signature="AAAA").status_code == 403
    assert repo.get(post.id).status is DraftStatus.PENDING_APPROVAL


def test_an_unreadable_reply_asks_him_to_try_again(twilio) -> None:
    response = post_sms(twilio, "maybe later", DAVIDS_NUMBER)
    assert response.status_code == 200
    assert "YES" in response.text and "NO" in response.text


def _waiting_post(status=DraftStatus.PENDING_APPROVAL):
    from app.main import app

    repo = app.state.repository
    post = repo.create(kind=DraftKind.POST, body="Had coffee with a client.", source_ref="post:y")
    repo.transition(post.id, status)
    return repo, post


def test_a_signed_yes_from_david_publishes_the_post(twilio) -> None:
    """The happy path, end to end over HTTP — the reason this endpoint exists."""
    repo, post = _waiting_post()

    response = post_sms(twilio, "YES", DAVIDS_NUMBER)
    assert response.status_code == 200
    assert "scheduled" in response.text

    # Approved and handed to the publisher, not left waiting.
    assert repo.get(post.id).status is DraftStatus.DONE


def test_a_signed_no_from_david_skips_it(twilio) -> None:
    repo, post = _waiting_post()

    response = post_sms(twilio, "No thanks", DAVIDS_NUMBER)
    assert response.status_code == 200
    assert "skipped" in response.text.lower()
    assert repo.get(post.id).status is DraftStatus.REJECTED


def test_a_signed_yes_from_a_stranger_publishes_nothing(twilio) -> None:
    """A correct signature is not enough — Twilio would sign any inbound text.

    Anyone who texts the Twilio number produces a genuinely signed request. The
    sender check is what stops a stranger approving David's queued post.
    """
    repo, post = _waiting_post()

    response = post_sms(twilio, "YES", "+15550009999")
    assert response.status_code == 200
    assert "scheduled" not in response.text
    assert repo.get(post.id).status is DraftStatus.PENDING_APPROVAL


def test_a_reply_with_nothing_waiting_says_so(twilio) -> None:
    response = post_sms(twilio, "yes", DAVIDS_NUMBER)
    assert response.status_code == 200
    assert "Nothing's waiting" in response.text


# -- Gemini transient failures ---------------------------------------------
#
# The Claude SDK retries these for us. The Gemini path is a hand-rolled REST
# call, and 503 "high demand" turned up twice while measuring chat latency —
# common enough that without a retry David would periodically get
# "Something didn't work" for a blip that cleared in a second.


def _gemini(config: AppConfig, monkeypatch, responses):
    """A GeminiChatModel whose HTTP calls return a scripted list of responses."""
    import httpx

    from app import env_secrets, llm

    monkeypatch.setattr(env_secrets, "gemini_api_key", lambda: "test-key")
    # Both backoffs, or the 429 test really waits out the rate-limit window —
    # 20s then 40s, which took the whole suite from 2s to 62s.
    monkeypatch.setattr(llm, "GEMINI_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(llm, "GEMINI_RATE_LIMIT_BACKOFF_SECONDS", 0)

    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, int):
            request = httpx.Request("POST", url)
            return httpx.Response(item, json={"error": {"message": "nope"}}, request=request)
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": item}]}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    return llm.GeminiChatModel(config), calls


def test_a_transient_503_is_retried(config: AppConfig, monkeypatch) -> None:
    model, calls = _gemini(config, monkeypatch, [503, "Karen is waiting."])
    answer = model.reply(system="s", messages=[{"role": "user", "content": "hi"}])

    assert answer == "Karen is waiting."
    assert len(calls) == 2, "should have retried once and succeeded"


def test_a_persistent_503_gives_up_and_returns_none(config: AppConfig, monkeypatch) -> None:
    """Giving up must be quiet, not a crash — the caller shows the fallback."""
    from app.llm import GEMINI_ATTEMPTS

    model, calls = _gemini(config, monkeypatch, [503])
    assert model.reply(system="s", messages=[{"role": "user", "content": "hi"}]) is None
    assert len(calls) == GEMINI_ATTEMPTS


def test_a_rate_limit_is_retried(config: AppConfig, monkeypatch) -> None:
    model, calls = _gemini(config, monkeypatch, [429, 429, "Fine."])
    assert model.reply(system="s", messages=[{"role": "user", "content": "hi"}]) == "Fine."
    assert len(calls) == 3


def test_a_bad_key_is_not_retried(config: AppConfig, monkeypatch) -> None:
    """401 will never clear on its own; retrying just delays the log line."""
    model, calls = _gemini(config, monkeypatch, [401])
    assert model.reply(system="s", messages=[{"role": "user", "content": "hi"}]) is None
    assert len(calls) == 1


def test_a_retired_model_is_not_retried(config: AppConfig, monkeypatch) -> None:
    """404 means the config.yaml model name is stale. Retrying hides it."""
    model, calls = _gemini(config, monkeypatch, [404])
    assert model.reply(system="s", messages=[{"role": "user", "content": "hi"}]) is None
    assert len(calls) == 1


def test_a_replayed_yes_cannot_approve_a_second_post(twilio) -> None:
    """The attack the signature alone does not stop.

    Twilio signs the URL and the parameters — no nonce, no timestamp — so one
    captured request stays valid forever. Since a reply always acts on whichever
    post is oldest, replaying a single YES would walk the queue and publish
    everything waiting on David.
    """
    from app.main import app

    repo = app.state.repository
    posts = []
    for index in range(3):
        post = repo.create(
            kind=DraftKind.POST, body=f"Post {index}", source_ref=f"post:replay-{index}"
        )
        repo.transition(post.id, DraftStatus.PENDING_APPROVAL)
        posts.append(post)

    captured_sid = "SM_captured_once"
    first = post_sms(twilio, "YES", DAVIDS_NUMBER, message_sid=captured_sid)
    assert first.status_code == 200

    # The same signed bytes, sent again and again.
    for _ in range(2):
        replay = post_sms(twilio, "YES", DAVIDS_NUMBER, message_sid=captured_sid)
        assert replay.status_code == 200
        assert "Already" in replay.text

    statuses = [repo.get(post.id).status for post in posts]
    assert statuses[0] is not DraftStatus.PENDING_APPROVAL, "the real reply should land"
    assert statuses[1] is DraftStatus.PENDING_APPROVAL
    assert statuses[2] is DraftStatus.PENDING_APPROVAL


def test_a_text_without_a_message_id_is_refused(twilio) -> None:
    """No id means no way to tell a first delivery from a replay."""
    url = "http://testserver/sms/incoming"
    params = {"Body": "YES", "From": DAVIDS_NUMBER}
    response = twilio.post(
        "/sms/incoming",
        content=urlencode(params),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Twilio-Signature": sign(url, params),
        },
    )
    assert response.status_code == 400
