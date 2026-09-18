"""Drafting and end-to-end inbox tests.

The rules being protected here are the compliance rules that would be
expensive to get wrong in front of a client: nothing gets sent, nothing gets
drafted from thin air, and the voice notes actually reach the model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.llm import MockTextWriter, build_text_writer
from app.config import AppConfig
from app.db import DraftKind, DraftRepository, DraftStatus
from app.inbox import NotificationType, parse_email, read_fixture
from app.inbox.drafting import ReplyDrafter, build_prompt, build_system_prompt
from app.inbox.gmail_client import GMAIL_SCOPES, MockMailReader, build_mail_reader
from app.inbox.service import InboxService

FIXTURE_DIR = Path(__file__).parent / "fixtures"


class RecordingWriter:
    """Captures what would have been sent to Claude, and returns fixed text."""

    def __init__(self, reply: str | None = "Good to hear from you, Karen.") -> None:
        self.reply = reply
        self.calls: list[dict[str, str]] = []

    def write(self, *, system: str, prompt: str, label: str) -> str | None:
        self.calls.append({"system": system, "prompt": prompt, "label": label})
        return self.reply


def _first(name: str):
    return parse_email(read_fixture(FIXTURE_DIR / name), source_ref=name)[0]


# -- the system prompt carries David's voice --------------------------------


def test_system_prompt_includes_the_voice_notes(config: AppConfig) -> None:
    system = build_system_prompt(config)
    assert config.voice.notes.strip()[:40] in system
    for rule in config.voice.never:
        assert rule in system


def test_system_prompt_forbids_pitching_and_sets_a_length(config: AppConfig) -> None:
    system = build_system_prompt(config)
    assert str(config.drafting.max_words) in system
    assert "meeting" in system.lower()


def test_prompt_quotes_what_the_person_actually_said(config: AppConfig) -> None:
    prompt = build_prompt(_first("mention_01_comment_on_post.eml"))
    assert "Bernard Lim" in prompt
    assert "federal audits" in prompt


def test_prompt_says_so_when_there_is_nothing_to_reply_to(config: AppConfig) -> None:
    prompt = build_prompt(_first("message_received_01.eml"))
    assert "did not include what they said" in prompt


# -- the refuse-to-guess rule ----------------------------------------------


def test_no_draft_for_a_message_we_cannot_read(config: AppConfig) -> None:
    """LinkedIn never includes message text, so there is nothing to reply to."""
    writer = RecordingWriter()
    drafter = ReplyDrafter(config, writer=writer)

    assert drafter.draft(_first("message_received_01.eml")) is None
    assert writer.calls == [], "should not have called Claude at all"


def test_blind_replies_can_be_switched_on_in_config(config: AppConfig) -> None:
    """The refuse-to-guess default is a config choice, not a hard-coded one."""
    permissive = config.model_copy(
        update={"drafting": config.drafting.model_copy(update={"draft_blind_replies": True})}
    )
    writer = RecordingWriter()
    drafter = ReplyDrafter(permissive, writer=writer)

    assert drafter.draft(_first("message_received_01.eml")) is not None
    assert len(writer.calls) == 1


def test_comments_and_invitations_do_get_drafted(config: AppConfig) -> None:
    """These carry real context, so there's something specific to say."""
    writer = RecordingWriter()
    drafter = ReplyDrafter(config, writer=writer)

    assert drafter.draft(_first("mention_01_comment_on_post.eml")) is not None
    assert drafter.draft(_first("connection_request_01.eml")) is not None
    assert len(writer.calls) == 2


def test_noise_is_never_drafted(config: AppConfig) -> None:
    writer = RecordingWriter()
    drafter = ReplyDrafter(config, writer=writer)

    for path in FIXTURE_DIR.glob("other_*.eml"):
        for item in parse_email(read_fixture(path), source_ref=path.name):
            assert drafter.draft(item) is None
    assert writer.calls == []


def test_a_claude_failure_is_not_a_crash(config: AppConfig) -> None:
    """An API outage must degrade to 'no draft', never to an exception."""
    drafter = ReplyDrafter(config, writer=RecordingWriter(reply=None))
    assert drafter.draft(_first("mention_01_comment_on_post.eml")) is None


# -- Gmail client ----------------------------------------------------------


def test_gmail_scope_is_read_only_and_nothing_else() -> None:
    """The compliance guarantee, asserted rather than promised."""
    assert GMAIL_SCOPES == ("https://www.googleapis.com/auth/gmail.readonly",)


def test_shipped_config_selects_the_clients_it_names(config: AppConfig) -> None:
    """Gmail and drafting are live now; the mock paths stay tested below."""
    reader = build_mail_reader(config)
    expected_mock = config.integrations.gmail == "mock"
    assert isinstance(reader, MockMailReader) is expected_mock
    assert build_text_writer(config).name == config.drafting.provider


def test_mock_reader_returns_the_real_fixtures(config: AppConfig) -> None:
    emails = MockMailReader(config, fixture_dir=FIXTURE_DIR).fetch("anything", 50)
    assert len(emails) >= 15
    assert all(email.message_id.startswith("fixture:") for email in emails)


@pytest.mark.parametrize(
    ("provider", "expected"),
    [("claude", "claude"), ("gemini", "gemini"), ("mock", "mock")],
)
def test_drafting_can_run_on_either_provider(
    config: AppConfig, provider: str, expected: str
) -> None:
    """Drafting is where nearly all the volume is, so it must be switchable."""
    switched = config.model_copy(
        update={"drafting": config.drafting.model_copy(update={"provider": provider})}
    )
    assert build_text_writer(switched).name == expected


def test_drafting_and_chat_choose_providers_independently(config: AppConfig) -> None:
    """So drafting can sit on one model and chat on a cheaper one."""
    from app.llm import build_chat_model

    mixed = config.model_copy(
        update={
            "drafting": config.drafting.model_copy(update={"provider": "claude"}),
            "chat": config.chat.model_copy(update={"provider": "gemini"}),
        }
    )
    assert build_text_writer(mixed).name == "claude"
    assert build_chat_model(mixed).name == "gemini"


def test_all_mocked_accounts_for_the_ai_providers(config: AppConfig) -> None:
    """A live AI provider means calls leave the server, whatever else is mocked.

    Asserted both directions off a synthetic config rather than the shipped one,
    so this keeps testing the logic no matter what the deployment is set to.
    """
    fully_mocked = config.model_copy(
        update={
            "integrations": config.integrations.model_copy(
                update={"gmail": "mock", "buffer": "mock", "sms": "mock"}
            ),
            "drafting": config.drafting.model_copy(update={"provider": "mock"}),
            "chat": config.chat.model_copy(update={"provider": "mock"}),
        }
    )
    assert fully_mocked.all_mocked

    live_drafting = fully_mocked.model_copy(
        update={"drafting": fully_mocked.drafting.model_copy(update={"provider": "gemini"})}
    )
    assert not live_drafting.all_mocked


def test_mock_draft_text_is_obviously_a_placeholder(config: AppConfig) -> None:
    """A convincing mock could end up pasted into LinkedIn during testing."""
    text = MockTextWriter(config).write(system="s", prompt="p", label="a reply")
    assert "MOCK" in text and "do not send" in text


# -- end to end ------------------------------------------------------------


@pytest.fixture
def service(config: AppConfig, repository: DraftRepository) -> InboxService:
    return InboxService(
        config,
        repository,
        reader=MockMailReader(config, fixture_dir=FIXTURE_DIR),
        writer=RecordingWriter(),
    )


def test_poll_creates_cards_only_for_actionable_email(
    service: InboxService, repository: DraftRepository
) -> None:
    result = service.poll()

    assert result.emails_read >= 15
    assert result.created == result.actionable
    assert result.created > 0

    stored = repository.list_open()
    assert len(stored) == result.created
    assert all(draft.linkedin_url for draft in stored)


def test_poll_twice_does_not_duplicate_davids_work(
    service: InboxService, repository: DraftRepository
) -> None:
    first = service.poll()
    second = service.poll()

    assert second.created == 0
    assert second.already_seen == first.created
    assert len(repository.list_open()) == first.created


def test_the_digest_becomes_three_separate_cards(
    service: InboxService, repository: DraftRepository
) -> None:
    service.poll()
    names = {draft.person_name for draft in repository.list_open()}
    assert {"Monique Ally", "Miriam Hernandez Pintado", "Miriam Buckner"} <= names


def test_messages_are_stored_as_cards_without_a_draft(
    service: InboxService, repository: DraftRepository
) -> None:
    """David still sees that Ephraim wrote — he just reads it on LinkedIn."""
    service.poll()
    ephraim = next(d for d in repository.list_open() if d.person_name == "Ephraim Adeyemo")
    assert ephraim.body == ""
    assert "messaging/thread" in ephraim.linkedin_url


def test_comments_become_comment_drafts_not_messages(
    service: InboxService, repository: DraftRepository
) -> None:
    service.poll()
    bernard = next(d for d in repository.list_open() if d.person_name == "Bernard Lim")
    assert bernard.kind is DraftKind.COMMENT
    assert bernard.body


def test_everything_starts_as_a_draft_awaiting_david(
    service: InboxService, repository: DraftRepository
) -> None:
    """Nothing the inbox produces may skip ahead in the approval lifecycle."""
    service.poll()
    assert all(d.status is DraftStatus.DRAFT for d in repository.list_open())
