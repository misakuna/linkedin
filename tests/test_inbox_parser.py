"""Parser tests, written against the real .eml fixtures.

These are the regression net for the most brittle part of the system. When
LinkedIn changes an email format, a test here fails and names the selector to
fix — which is the whole reason the fixtures are committed.

The expected classification for each fixture is its filename (see
tests/fixtures/README.md), so a fixture can never grade itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.inbox import NotificationType, parse_email, read_fixture
from app.inbox.links import canonicalize

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _fixtures() -> list[Path]:
    return sorted(p for p in FIXTURE_DIR.glob("*.eml") if not p.name.startswith("._"))


def _expected_type(path: Path) -> NotificationType:
    """The classification declared by the filename prefix."""
    for candidate in NotificationType:
        if path.stem.startswith(candidate.value):
            return candidate
    raise AssertionError(f"{path.name} does not start with a known type name")


def _load(name: str):
    return parse_email(read_fixture(FIXTURE_DIR / name), source_ref=name)


# -- fixtures exist --------------------------------------------------------


def test_fixtures_are_present() -> None:
    """Guard against an empty fixture directory silently passing everything."""
    assert len(_fixtures()) >= 15


# -- classification --------------------------------------------------------


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_every_fixture_classifies_as_its_filename_says(path: Path) -> None:
    items = parse_email(read_fixture(path), source_ref=path.name)
    assert items, f"{path.name} produced no parsed items at all"
    assert all(item.type is _expected_type(path) for item in items)


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_noise_never_becomes_actionable(path: Path) -> None:
    """The worst case: LinkedIn noise promoted into a card David acts on."""
    if not path.stem.startswith("other"):
        pytest.skip("only checks the noise fixtures")
    for item in parse_email(read_fixture(path), source_ref=path.name):
        assert not item.is_actionable


def test_unknown_template_falls_back_to_other() -> None:
    """A LinkedIn format we've never seen must be ignored, not guessed at."""
    raw = read_fixture(FIXTURE_DIR / "message_received_01.eml")
    raw.replace_header("X-LinkedIn-Template", "email_something_brand_new_09")
    items = parse_email(raw, source_ref="synthetic")
    assert [item.type for item in items] == [NotificationType.OTHER]


# -- extraction: messages --------------------------------------------------


def test_message_extracts_sender_and_thread_link() -> None:
    (item,) = _load("message_received_01.eml")
    assert item.person_name == "Ephraim Adeyemo"
    assert "linkedin.com/messaging/thread/" in item.linkedin_url
    assert item.profile_url == "https://ng.linkedin.com/in/ephraimadeyemo"


def test_second_message_fixture_extracts_too() -> None:
    """Two samples of the same template must both work, not just the first."""
    (item,) = _load("message_received_02.eml")
    assert item.person_name == "Usman Gul"
    assert item.headline == "Founder/CEO at Metal"
    assert "linkedin.com/messaging/thread/" in item.linkedin_url


def test_message_has_no_preview_text() -> None:
    """LinkedIn does not include the message body — the drafter depends on this."""
    (item,) = _load("message_received_01.eml")
    assert item.preview is None


# -- extraction: connection requests ---------------------------------------


@pytest.mark.parametrize(
    ("fixture", "name"),
    [
        ("connection_request_01.eml", "Nina Nguyen"),
        ("connection_request_02.eml", "ISAAC OKYERE ASAMOAH"),
        ("connection_request_03.eml", "Mustafa Uğraşkan"),
        ("connection_request_04.eml", "Raymond Obikwelu"),
    ],
)
def test_invite_extracts_person_and_accept_link(fixture: str, name: str) -> None:
    (item,) = _load(fixture)
    assert item.person_name == name
    assert "linkedin.com/mynetwork/invite-accept/" in item.linkedin_url
    assert item.profile_url and "/in/" in item.profile_url


def test_invite_captures_the_note() -> None:
    (item,) = _load("connection_request_01.eml")
    assert item.preview is not None
    assert "join your professional network" in item.preview


def test_invite_captures_headline() -> None:
    (item,) = _load("connection_request_02.eml")
    assert item.headline == "Driver at DPWORLD"


# -- extraction: connections accepted --------------------------------------


def test_single_acceptance_names_the_right_person() -> None:
    """This email is HTML-only and contains five different profile links —
    four of them are 'explore their network' suggestions, not the accepter."""
    (item,) = _load("connection_accepted_01.eml")
    assert item.person_name == "Angie Enriquez"
    assert item.profile_url is not None
    assert "angie-enriquez" in item.profile_url


def test_digest_fans_out_into_one_item_per_person() -> None:
    """One email, three people, three cards. The count is in the email itself."""
    items = _load("connection_accepted_02_digest.eml")
    names = [item.person_name for item in items]
    assert names == ["Monique Ally", "Miriam Hernandez Pintado", "Miriam Buckner"]
    assert all("linkedin.com" in item.linkedin_url for item in items)


def test_digest_items_have_distinct_source_refs() -> None:
    """Otherwise the unique index on source_ref collapses them into one draft."""
    items = _load("connection_accepted_02_digest.eml")
    refs = [item.source_ref for item in items]
    assert len(set(refs)) == len(refs) == 3


# -- extraction: mentions / comments ---------------------------------------


def test_comment_extracts_commenter_text_and_post_link() -> None:
    (item,) = _load("mention_01_comment_on_post.eml")
    assert item.person_name == "Bernard Lim"
    assert item.preview is not None
    assert "federal audits" in item.preview
    assert "linkedin.com/feed/update/" in item.linkedin_url


# -- deep links ------------------------------------------------------------


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_deep_links_carry_no_tracking_or_tokens(path: Path) -> None:
    """David's dashboard must never display or store a signed LinkedIn token."""
    banned = re.compile(r"midToken|midSig|otpToken|loid=|trkEmail|lipi=")
    for item in parse_email(read_fixture(path), source_ref=path.name):
        for url in (item.linkedin_url, item.profile_url):
            if url:
                assert not banned.search(url), f"{path.name}: {url}"


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_deep_links_stay_on_linkedin(path: Path) -> None:
    """A parser bug must never hand David a link to somewhere else entirely."""
    for item in parse_email(read_fixture(path), source_ref=path.name):
        for url in (item.linkedin_url, item.profile_url):
            if url:
                assert re.match(r"https://([a-z]{2,3}\.)?linkedin\.com/", url), url


def test_canonicalize_strips_tracking_but_keeps_the_destination() -> None:
    messy = (
        "https://www.linkedin.com/comm/messaging/thread/2-abc==?"
        "lipi=urn%3Ali%3Apage%3Aemail&midToken=AQH&midSig=1_j&trk=eml-x&otpToken=OGQ"
    )
    assert canonicalize(messy) == "https://www.linkedin.com/messaging/thread/2-abc=="


def test_canonicalize_keeps_parameters_that_identify_content() -> None:
    """connId is the person being messaged — dropping it breaks the link."""
    messy = (
        "https://www.linkedin.com/comm/messaging/compose/?"
        "connId=monique-ally-963272318&midToken=AQH&trk=eml-x"
    )
    assert canonicalize(messy) == (
        "https://www.linkedin.com/messaging/compose/?connId=monique-ally-963272318"
    )


# -- metadata --------------------------------------------------------------


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_every_item_carries_diagnostics(path: Path) -> None:
    """subject + template are what the developer reads when a format change breaks this."""
    for item in parse_email(read_fixture(path), source_ref=path.name):
        assert item.subject
        assert item.received_at is not None


def test_actionable_items_always_have_a_person_and_a_link() -> None:
    """A card with no name or nowhere to go is worse than no card."""
    for path in _fixtures():
        for item in parse_email(read_fixture(path), source_ref=path.name):
            if item.is_actionable:
                assert item.person_name, f"{path.name} actionable but unnamed"
                assert item.linkedin_url, f"{path.name} actionable but no link"


def test_an_acceptance_carries_the_headline() -> None:
    """Without this, 11 of David's 12 cards said "we couldn't write a suggestion"
    on emails where nobody had written anything to read.

    A connection acceptance has no message in it — the event is "Angie accepted
    your invitation". Her headline is all a warm "good to connect" note needs,
    and it is sitting in the HTML directly under her name. The drafter already
    treated a headline as sufficient for this type; the parser simply never
    read it.
    """
    notifications = _load("connection_accepted_01.eml")
    assert len(notifications) == 1

    accepted = notifications[0]
    assert accepted.person_name == "Angie Enriquez"
    assert accepted.headline == "Productora Audiovisual | Marketing | Aviation"


def test_an_acceptance_never_borrows_a_strangers_headline() -> None:
    """The trap in this email, and the reason for the exact-name match.

    "Suggestions from Angie's network" lists four other people, each with their
    name alone on one line and their headline on the next — the identical shape
    we key off. A looser match would return a stranger's job title as Angie's,
    and David would send a warm note referencing the wrong person's work.
    """
    accepted = _load("connection_accepted_01.eml")[0]

    strangers = ("Juan", "Alejandro", "Restaurantes", "mutual connections")
    assert accepted.headline
    for stranger in strangers:
        assert stranger not in accepted.headline
