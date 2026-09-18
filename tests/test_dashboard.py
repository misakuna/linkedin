"""David's dashboard: the page, the login, and the JSON behind it.

Two things are being protected here. The obvious one is that the screens show
the right cards. The important one is that nothing David taps can put words on
LinkedIn that he didn't approve.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.config import AppConfig
from app.dashboard.auth import COOKIE_NAME, MAX_ATTEMPTS, LoginGate
from app.dashboard.routes import FRIENDLY_ERROR, load_dashboard_html
from app.dashboard.viewmodels import humanize_age, humanize_when, item_to_json, post_to_json
from app.db import Draft, DraftKind, DraftStatus

PACIFIC = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc)


def make_draft(**overrides) -> Draft:
    fields = {
        "id": 1,
        "kind": DraftKind.REPLY,
        "status": DraftStatus.DRAFT,
        "body": "Good to hear from you, Karen.",
        "created_at": NOW - timedelta(hours=2),
        "updated_at": NOW - timedelta(hours=2),
        "person_name": "Karen Mills",
        "headline": "Founder & Executive Coach",
        "notification_type": "message_received",
        "linkedin_url": "https://www.linkedin.com/messaging/thread/2-abc",
    }
    fields.update(overrides)
    return Draft(**fields)


# -- plain language --------------------------------------------------------


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=20), "just now"),
        (timedelta(minutes=1), "1 minute ago"),
        (timedelta(minutes=40), "40 minutes ago"),
        (timedelta(hours=1), "1 hour ago"),
        (timedelta(hours=5), "5 hours ago"),
        (timedelta(days=1), "yesterday"),
        (timedelta(days=3), "3 days ago"),
    ],
)
def test_ages_read_like_a_person_said_them(delta: timedelta, expected: str) -> None:
    assert humanize_age(NOW - delta, now=NOW) == expected


def test_publish_times_read_like_a_person_said_them() -> None:
    when = datetime(2026, 8, 4, 19, 39, tzinfo=timezone.utc)  # 12:39pm Pacific
    assert humanize_when(when, PACIFIC, now=NOW) == "today at 12:39 PM"
    assert humanize_when(when + timedelta(days=1), PACIFIC, now=NOW) == "tomorrow at 12:39 PM"
    assert humanize_when(when + timedelta(days=2), PACIFIC, now=NOW) == "Thursday at 12:39 PM"


def test_a_card_says_what_happened_in_davids_words() -> None:
    card = item_to_json(make_draft(), now=NOW)
    assert card["summary"] == "Karen Mills sent you a message"
    assert card["detail"] == "Founder & Executive Coach"
    assert card["draft_text"] == "Good to hear from you, Karen."
    assert card["no_draft_reason"] is None
    assert card["waiting_since"] == "2 hours ago"
    assert card["handled"] is False


@pytest.mark.parametrize(
    ("notification_type", "expected"),
    [
        ("connection_request", "Nina Nguyen wants to connect with you"),
        ("connection_accepted", "Nina Nguyen is now connected with you"),
        ("mention", "Nina Nguyen commented on your post"),
    ],
)
def test_each_kind_of_notification_gets_its_own_sentence(
    notification_type: str, expected: str
) -> None:
    card = item_to_json(
        make_draft(person_name="Nina Nguyen", notification_type=notification_type), now=NOW
    )
    assert card["summary"] == expected


def test_a_card_with_no_draft_explains_itself_calmly() -> None:
    """The common case for messages — it must not read as an error."""
    card = item_to_json(make_draft(body=""), now=NOW)
    assert card["draft_text"] is None
    assert "reply in your own words" in card["no_draft_reason"]


def test_handled_items_are_marked_handled() -> None:
    assert item_to_json(make_draft(status=DraftStatus.DONE), now=NOW)["handled"] is True
    assert item_to_json(make_draft(status=DraftStatus.REJECTED), now=NOW)["handled"] is True


@pytest.mark.parametrize("body", ["Good to hear from you, Karen.", ""])
def test_no_jargon_reaches_the_card(body: str) -> None:
    """The database's vocabulary must never surface on David's screen.

    Only the fields that get rendered are checked — a JSON null is not text
    David ever sees, it's the frontend's signal to swap card variants.
    """
    card = item_to_json(make_draft(body=body), now=NOW)
    shown = " ".join(
        str(card[field])
        for field in ("summary", "detail", "draft_text", "no_draft_reason", "waiting_since")
        if card[field] is not None
    ).lower()
    for word in ("pending_approval", "draft", "status", "sqlite", "notification_type"):
        assert word not in shown


def test_post_cards_say_when_not_what_status() -> None:
    post = post_to_json(
        make_draft(
            kind=DraftKind.POST,
            status=DraftStatus.PENDING_APPROVAL,
            body="I sat with a client last week.",
            scheduled_for=datetime(2026, 8, 4, 19, 39, tzinfo=timezone.utc),
        ),
        PACIFIC,
        now=NOW,
    )
    assert post["scheduled_for_human"] == "today at 12:39 PM"
    assert post["status_label"] == "Waiting for your OK"


# -- login -----------------------------------------------------------------


def test_the_right_pin_gets_in_and_the_wrong_one_does_not(config: AppConfig) -> None:
    gate = LoginGate(config)
    assert gate.check_pin("0000") is True
    assert gate.check_pin("9999") is False


def test_a_session_cookie_survives_a_round_trip(config: AppConfig) -> None:
    gate = LoginGate(config)
    assert gate.verify(gate.issue()) is True


def test_a_forged_cookie_is_refused(config: AppConfig) -> None:
    gate = LoginGate(config)
    assert gate.verify("ok") is False
    assert gate.verify(gate.issue()[:-4] + "aaaa") is False
    assert gate.verify(None) is False


def test_guessing_gets_locked_out(config: AppConfig) -> None:
    """10,000 PINs is nothing without a limit."""
    gate = LoginGate(config)
    for _ in range(MAX_ATTEMPTS):
        gate.check_pin("1111")
    assert gate.locked_out
    # Even the correct PIN is refused while locked out.
    assert gate.check_pin("0000") is False


# -- the page --------------------------------------------------------------


def test_the_served_page_uses_the_real_api_not_demo_data(config: AppConfig) -> None:
    """Serving the file with mock data on would show David invented people."""
    html = load_dashboard_html(config.dashboard.resolved_html_path())
    assert html is not None
    assert "const USE_MOCK = false;" in html
    assert "const USE_MOCK = true;" not in html


def test_a_missing_page_still_says_the_human_thing(tmp_path) -> None:
    assert load_dashboard_html(tmp_path / "nope.html") is None


def test_the_dashboard_is_served(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    assert "LinkedIn AI" in response.text
    assert "const USE_MOCK = false;" in response.text


# -- the API ---------------------------------------------------------------


@pytest.fixture
def signed_in(app_client):
    assert app_client.post("/api/login", json={"pin": "0000"}).status_code == 200
    return app_client


@pytest.fixture
def repo(app_client):
    from app.main import app

    return app.state.repository


PROTECTED = [
    ("get", "/api/today"),
    ("get", "/api/messages"),
    ("get", "/api/posts"),
    ("post", "/api/items/1/done"),
    ("post", "/api/posts/1/approve"),
    ("post", "/api/posts/1/skip"),
]


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_everything_needs_a_login(app_client, method: str, path: str) -> None:
    assert getattr(app_client, method)(path).status_code == 401


def test_editing_a_post_needs_a_login(app_client) -> None:
    assert app_client.put("/api/posts/1/text", json={"body": "x"}).status_code == 401


def test_session_reports_signed_out_then_in(app_client) -> None:
    assert app_client.get("/api/session").json() == {"authenticated": False}
    app_client.post("/api/login", json={"pin": "0000"})
    assert app_client.get("/api/session").json() == {"authenticated": True}
    assert COOKIE_NAME in app_client.cookies


def test_a_wrong_pin_gets_nothing(app_client) -> None:
    assert app_client.post("/api/login", json={"pin": "4321"}).status_code == 401
    assert app_client.get("/api/today").status_code == 401


def test_the_session_cookie_is_secure_behind_railways_tls(app_client) -> None:
    """Railway terminates TLS at its edge and forwards to us over plain HTTP.

    So request.url.scheme reads "http" in production and can't be trusted. If
    this regresses, David's login cookie ships without the Secure flag and a
    browser will happily send it over an unencrypted connection.
    """
    response = app_client.post(
        "/api/login", json={"pin": "0000"}, headers={"X-Forwarded-Proto": "https"}
    )
    assert response.status_code == 200
    assert "secure" in response.headers["set-cookie"].lower()


def test_the_session_cookie_is_not_secure_on_plain_local_http(app_client) -> None:
    """The other half: a Secure cookie over local http:// is silently dropped."""
    response = app_client.post("/api/login", json={"pin": "0000"})
    assert response.status_code == 200
    assert "secure" not in response.headers["set-cookie"].lower()


def test_today_shows_open_items_and_hides_finished_ones(signed_in, repo) -> None:
    waiting = repo.create(
        kind=DraftKind.REPLY, body="Hello Karen.", person_name="Karen Mills",
        notification_type="message_received",
        linkedin_url="https://www.linkedin.com/messaging/thread/2-a",
    )
    finished = repo.create(
        kind=DraftKind.COMMENT, body="Thanks Bernard.", person_name="Bernard Lim",
        notification_type="mention",
        linkedin_url="https://www.linkedin.com/feed/update/urn:li:activity:1",
    )
    repo.transition(finished.id, DraftStatus.DONE, note="handled")

    items = signed_in.get("/api/today").json()["items"]
    assert [i["person_name"] for i in items] == ["Karen Mills"]

    # Messages keeps the finished one, dimmed.
    everything = signed_in.get("/api/messages").json()["items"]
    handled = {i["person_name"]: i["handled"] for i in everything}
    assert handled == {"Karen Mills": False, "Bernard Lim": True}


def test_posts_are_grouped_the_way_the_screen_shows_them(signed_in, repo) -> None:
    awaiting = repo.create(kind=DraftKind.POST, body="Post one.", source_ref="post:2026-08-04")
    repo.transition(awaiting.id, DraftStatus.PENDING_APPROVAL)

    done = repo.create(kind=DraftKind.POST, body="Post two.", source_ref="post:2026-07-30")
    repo.transition(done.id, DraftStatus.PENDING_APPROVAL)
    repo.transition(done.id, DraftStatus.APPROVED)
    repo.transition(done.id, DraftStatus.DONE, note="queued")

    groups = signed_in.get("/api/posts").json()
    assert [p["body"] for p in groups["awaiting"]] == ["Post one."]
    assert [p["body"] for p in groups["past"]] == ["Post two."]
    assert groups["scheduled"] == []


def test_marking_an_item_done_takes_it_off_today(signed_in, repo) -> None:
    item = repo.create(
        kind=DraftKind.REPLY, body="Hello.", person_name="Karen Mills",
        notification_type="message_received", linkedin_url="https://www.linkedin.com/x",
    )
    assert signed_in.post(f"/api/items/{item.id}/done").json() == {"ok": True}
    assert repo.get(item.id).status is DraftStatus.DONE
    assert signed_in.get("/api/today").json()["items"] == []


def test_tapping_done_twice_is_harmless(signed_in, repo) -> None:
    item = repo.create(
        kind=DraftKind.REPLY, body="Hello.", person_name="Karen Mills",
        notification_type="message_received", linkedin_url="https://www.linkedin.com/x",
    )
    signed_in.post(f"/api/items/{item.id}/done")
    assert signed_in.post(f"/api/items/{item.id}/done").json() == {"ok": True}


def test_approving_a_post_schedules_it(signed_in, repo) -> None:
    post = repo.create(
        kind=DraftKind.POST, body="A short post.", source_ref="post:2026-08-04",
        scheduled_for=datetime.now(timezone.utc) + timedelta(hours=3),
    )
    repo.transition(post.id, DraftStatus.PENDING_APPROVAL)

    assert signed_in.post(f"/api/posts/{post.id}/approve").json() == {"ok": True}
    assert repo.get(post.id).status is DraftStatus.DONE


def test_skipping_a_post_publishes_nothing(signed_in, repo) -> None:
    post = repo.create(kind=DraftKind.POST, body="Nope.", source_ref="post:2026-08-06")
    repo.transition(post.id, DraftStatus.PENDING_APPROVAL)

    assert signed_in.post(f"/api/posts/{post.id}/skip").json() == {"ok": True}
    assert repo.get(post.id).status is DraftStatus.REJECTED


def test_david_can_reword_a_post_before_approving(signed_in, repo) -> None:
    post = repo.create(kind=DraftKind.POST, body="First words.", source_ref="post:2026-08-04")
    repo.transition(post.id, DraftStatus.PENDING_APPROVAL)

    assert signed_in.put(f"/api/posts/{post.id}/text", json={"body": "Better words."}).status_code == 200
    assert repo.get(post.id).body == "Better words."


# -- the guarantees --------------------------------------------------------


def test_the_item_endpoint_cannot_publish_a_post(signed_in, repo) -> None:
    """`items/done` moves things straight to done — a post must not be reachable."""
    post = repo.create(kind=DraftKind.POST, body="Unapproved.", source_ref="post:2026-08-04")
    repo.transition(post.id, DraftStatus.PENDING_APPROVAL)

    assert signed_in.post(f"/api/items/{post.id}/done").status_code == 400
    assert repo.get(post.id).status is DraftStatus.PENDING_APPROVAL


def test_a_post_can_never_skip_approval(repo) -> None:
    """The lifecycle rule, re-checked now that replies may go draft to done."""
    from app.db import InvalidTransitionError

    post = repo.create(kind=DraftKind.POST, body="Straight out?", source_ref="post:2026-08-04")
    with pytest.raises(InvalidTransitionError):
        repo.transition(post.id, DraftStatus.DONE)
    with pytest.raises(InvalidTransitionError):
        repo.transition(post.id, DraftStatus.APPROVED)


def test_words_cannot_be_changed_after_david_approved_them(signed_in, repo) -> None:
    """Editing a queued post would publish text he never read."""
    post = repo.create(
        kind=DraftKind.POST, body="Approved words.", source_ref="post:2026-08-04",
        scheduled_for=datetime.now(timezone.utc) + timedelta(hours=3),
    )
    repo.transition(post.id, DraftStatus.PENDING_APPROVAL)
    signed_in.post(f"/api/posts/{post.id}/approve")

    response = signed_in.put(f"/api/posts/{post.id}/text", json={"body": "Sneaky rewrite."})
    assert response.status_code == 409
    assert repo.get(post.id).body == "Approved words."


def test_the_friendly_error_carries_no_technical_detail() -> None:
    assert FRIENDLY_ERROR == "Something didn't work — text support."
    for word in ("error", "500", "exception", "traceback", "failed"):
        assert word not in FRIENDLY_ERROR.lower()
