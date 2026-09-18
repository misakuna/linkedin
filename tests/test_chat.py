"""David's chat with the assistant.

The interesting tests here aren't about the model — they're about what the
assistant is told it cannot do, and what happens when the model is unreachable.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.llm import MockChatModel, build_chat_model, gemini_text
from app.chat.service import FALLBACK_REPLY, ChatService, build_system_prompt
from app.config import AppConfig
from app.db import DraftKind, DraftRepository, DraftStatus
from app.db.chat_repository import ChatRepository


class RecordingModel:
    """Captures what the model was told, and answers with fixed text."""

    def __init__(self, answer: str | None = "You've got three people waiting.") -> None:
        self.answer = answer
        self.calls: list[dict] = []

    def reply(self, *, system: str, messages: list[dict[str, str]]) -> str | None:
        self.calls.append({"system": system, "messages": messages})
        return self.answer


@pytest.fixture
def chats(db_path) -> ChatRepository:
    return ChatRepository(db_path)


@pytest.fixture
def model() -> RecordingModel:
    return RecordingModel()


@pytest.fixture
def chat(config: AppConfig, repository: DraftRepository, chats: ChatRepository, model) -> ChatService:
    return ChatService(config, repository, chats, model=model)


# -- storage ---------------------------------------------------------------


def test_the_conversation_is_kept_in_order(chats: ChatRepository) -> None:
    chats.append("user", "Who's waiting?")
    chats.append("assistant", "Karen is.")
    chats.append("user", "Thanks")

    assert [(m.role, m.content) for m in chats.recent()] == [
        ("user", "Who's waiting?"),
        ("assistant", "Karen is."),
        ("user", "Thanks"),
    ]


def test_only_the_recent_conversation_is_kept_in_view(chats: ChatRepository) -> None:
    """A year of chat must not end up in a prompt."""
    for n in range(30):
        chats.append("user", f"question {n}")
    recent = chats.recent(limit=10)
    assert len(recent) == 10
    assert recent[-1].content == "question 29"
    assert recent[0].content == "question 20"


def test_clearing_starts_over(chat: ChatService, chats: ChatRepository) -> None:
    chat.ask("Hello")
    assert chat.clear() == 2
    assert chats.recent() == []


# -- what the assistant is told --------------------------------------------


def test_the_assistant_is_told_it_cannot_act(config: AppConfig) -> None:
    """The failure that matters is David believing something was sent."""
    # Whitespace-normalized: the prompt is hard-wrapped, and these assertions
    # are about what it says, not where the lines break.
    system = " ".join(build_system_prompt(config, "nothing happening").split())
    assert "You CANNOT send messages, accept invitations, publish posts" in system
    assert "Never say you have done something, or that you will do it later" in system
    assert "You have no way to act" in system


def test_the_assistant_knows_how_david_writes(config: AppConfig) -> None:
    system = build_system_prompt(config, "nothing happening")
    assert config.voice.notes.strip()[:40] in system
    for rule in config.voice.never:
        assert rule in system


def test_the_assistant_is_told_who_is_waiting(
    chat: ChatService, repository: DraftRepository
) -> None:
    repository.create(
        kind=DraftKind.REPLY, body="Hello Karen.", person_name="Karen Mills",
        notification_type="message_received", linkedin_url="https://www.linkedin.com/x",
    )
    repository.create(
        kind=DraftKind.COMMENT, body="Thanks Bernard.", person_name="Bernard Lim",
        notification_type="mention", linkedin_url="https://www.linkedin.com/y",
    )

    situation = chat.describe_situation(today=date(2026, 8, 3))
    assert "Karen Mills" in situation
    assert "Bernard Lim" in situation


def test_the_assistant_knows_when_nobody_is_waiting(chat: ChatService) -> None:
    situation = chat.describe_situation(today=date(2026, 8, 3))
    assert "caught up" in situation
    assert "No post is waiting" in situation


def test_the_assistant_can_see_the_post_awaiting_approval(
    chat: ChatService, repository: DraftRepository
) -> None:
    post = repository.create(
        kind=DraftKind.POST,
        body="I sat with an executive last week who thought 70 hours was a badge of honor.",
        source_ref="post:2026-08-04",
        scheduled_for=datetime(2026, 8, 4, 19, 39, tzinfo=timezone.utc),
    )
    repository.transition(post.id, DraftStatus.PENDING_APPROVAL)

    situation = chat.describe_situation(today=date(2026, 8, 3))
    assert "waiting for his OK" in situation
    assert "badge of honor" in situation


def test_the_assistant_knows_the_next_posting_days(chat: ChatService) -> None:
    situation = chat.describe_situation(today=date(2026, 8, 3))
    assert "Tuesday" in situation and "Thursday" in situation


def test_the_situation_is_rebuilt_every_turn(
    chat: ChatService, repository: DraftRepository, model: RecordingModel
) -> None:
    """Otherwise it would confidently name someone David dealt with an hour ago."""
    chat.ask("Who's waiting?")
    assert "caught up" in model.calls[0]["system"]

    repository.create(
        kind=DraftKind.REPLY, body="Hi.", person_name="Karen Mills",
        notification_type="message_received", linkedin_url="https://www.linkedin.com/x",
    )
    chat.ask("And now?")
    assert "Karen Mills" in model.calls[1]["system"]


# -- asking ----------------------------------------------------------------


def test_asking_stores_both_sides(chat: ChatService, chats: ChatRepository) -> None:
    answer = chat.ask("Who's waiting on me?")

    assert answer.role == "assistant"
    assert answer.content == "You've got three people waiting."
    assert [m.role for m in chats.recent()] == ["user", "assistant"]


def test_the_model_sees_the_conversation_so_far(
    chat: ChatService, model: RecordingModel
) -> None:
    chat.ask("Who's waiting?")
    chat.ask("What should I say to her?")

    sent = model.calls[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[-1]["content"] == "What should I say to her?"


def test_an_empty_question_is_refused(chat: ChatService) -> None:
    with pytest.raises(ValueError):
        chat.ask("   ")


def test_a_model_outage_still_gives_david_an_answer(
    config: AppConfig, repository: DraftRepository, chats: ChatRepository
) -> None:
    """A spinner that never resolves is the worst possible outcome here."""
    service = ChatService(config, repository, chats, model=RecordingModel(answer=None))
    answer = service.ask("Who's waiting?")

    assert answer.content == FALLBACK_REPLY
    assert [m.role for m in chats.recent()] == ["user", "assistant"]


# -- providers -------------------------------------------------------------


def test_the_shipped_config_builds_the_provider_it_names(config: AppConfig) -> None:
    """Chat is live on Gemini now, so assert the wiring rather than a mock."""
    assert build_chat_model(config).name == config.chat.provider


def test_the_mock_answer_is_obviously_not_real(config: AppConfig) -> None:
    reply = MockChatModel(config).reply(system="s", messages=[{"role": "user", "content": "hi"}])
    assert "Demo answer" in reply and "no AI was used" in reply


@pytest.mark.parametrize("provider", ["claude", "gemini"])
def test_either_provider_can_be_selected(config: AppConfig, provider: str) -> None:
    switched = config.model_copy(
        update={"chat": config.chat.model_copy(update={"provider": provider})}
    )
    assert build_chat_model(switched).name.lower() == provider


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"candidates": []},
        {"candidates": [{}]},
        {"candidates": [{"content": {}}]},
        {"candidates": [{"content": {"parts": []}}]},
    ],
)
def test_a_blocked_or_empty_gemini_response_is_handled(payload) -> None:
    """Gemini returns no parts when it blocks a response — don't index blindly."""
    assert gemini_text(payload) is None


def test_a_normal_gemini_response_is_read() -> None:
    payload = {"candidates": [{"content": {"parts": [{"text": "Hello "}, {"text": "David."}]}}]}
    assert gemini_text(payload) == "Hello David."


# -- the API ---------------------------------------------------------------


@pytest.fixture
def signed_in(app_client):
    app_client.post("/api/login", json={"pin": "0000"})
    return app_client


def test_chat_needs_a_login(app_client) -> None:
    assert app_client.get("/api/chat").status_code == 401
    assert app_client.post("/api/chat", json={"message": "hi"}).status_code == 401
    assert app_client.delete("/api/chat").status_code == 401


def test_the_conversation_round_trips_through_the_api(signed_in) -> None:
    assert signed_in.get("/api/chat").json() == {"messages": []}

    reply = signed_in.post("/api/chat", json={"message": "Who's waiting?"}).json()["reply"]
    assert reply["role"] == "assistant"
    assert reply["content"]

    messages = signed_in.get("/api/chat").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "Who's waiting?"


def test_an_empty_message_is_rejected(signed_in) -> None:
    assert signed_in.post("/api/chat", json={"message": ""}).status_code == 422
    assert signed_in.post("/api/chat", json={"message": "   "}).status_code == 400


def test_a_huge_paste_is_rejected(signed_in) -> None:
    """A pasted document would blow the context window and the bill."""
    assert signed_in.post("/api/chat", json={"message": "x" * 5000}).status_code == 422


def test_clearing_the_chat_through_the_api(signed_in) -> None:
    signed_in.post("/api/chat", json={"message": "hello"})
    assert signed_in.delete("/api/chat").json() == {"ok": True}
    assert signed_in.get("/api/chat").json() == {"messages": []}
