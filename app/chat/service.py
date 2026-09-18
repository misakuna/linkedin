"""David's chat with the assistant.

What: answers David's questions, with his actual LinkedIn situation in view.

Why: a generic chatbot would tell him what any chatbot tells anyone. This one
knows who is waiting on a reply, what post is queued for Tuesday, and how David
writes — so "what should I post about?" and "who am I ignoring?" get real
answers instead of general advice.

How: each turn builds a fresh context block from the database and prepends it
to the conversation. Nothing is cached, so the assistant is never confidently
wrong about a card David dealt with an hour ago.

**It cannot do anything.** It reads and it talks. It cannot send a message,
accept an invitation, publish a post, or change a draft — and the system prompt
says so plainly, because the failure mode that matters is David believing
something was sent when it wasn't.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.llm import ChatModel, build_chat_model
from app.config import AppConfig
from app.db import DraftKind, DraftRepository, DraftStatus
from app.db.chat_repository import ChatMessage, ChatRepository
from app.logging_setup import get_logger
from app.posting.schedule import next_posting_days

logger = get_logger(__name__)

# What David is told when the model can't be reached. Same words the rest of
# the dashboard uses for any failure.
FALLBACK_REPLY = "Something didn't work — text support."


def build_system_prompt(config: AppConfig, situation: str) -> str:
    """The assistant's instructions, including today's live situation."""
    voice = config.voice
    never = "\n".join(f"- {rule}" for rule in voice.never)

    return f"""You are {config.client.name}'s assistant. He calls you when he wants to
think something through about LinkedIn — what to post, how to reply to someone,
whether an idea is any good, or what's waiting on him.

You are talking TO David. Speak to him directly, warmly, like someone who works
with him and knows his business.

WHO DAVID IS AND HOW HE WRITES
{voice.notes.strip()}

WHEN YOU SUGGEST WORDS FOR HIM TO USE, NEVER DO THESE
{never}

WHAT YOU CAN AND CANNOT DO
You can read what's on his plate, talk things through, and suggest wording.
You CANNOT send messages, accept invitations, publish posts, schedule anything,
or change what's on his dashboard. If he asks you to do one of those, say
plainly that he'll need to do it himself and tell him exactly where — the
Today screen, the My Posts screen, or LinkedIn itself. Never say you have done
something, or that you will do it later. You have no way to act.

HOW TO TALK TO HIM
- Short. Two or three sentences unless he asks for more.
- Plain words. No jargon, no bullet-point lectures, no headers.
- Never mention databases, drafts, statuses, APIs, or how any of this works.
- If you don't know something, say so. Don't guess at numbers or names.
- Don't end every message with a question. Only ask when you genuinely need
  to know something to help.

WHAT'S HAPPENING RIGHT NOW
{situation}"""


class ChatService:
    """Answers David's questions with his own situation in context."""

    def __init__(
        self,
        config: AppConfig,
        drafts: DraftRepository,
        chats: ChatRepository,
        *,
        model: ChatModel | None = None,
    ) -> None:
        self._config = config
        self._drafts = drafts
        self._chats = chats
        self._model = model or build_chat_model(config)

    # -- reading -----------------------------------------------------------

    def history(self) -> list[ChatMessage]:
        """The conversation so far, oldest first."""
        return self._chats.recent(self._config.chat.history_messages)

    def describe_situation(self, *, today: date | None = None) -> str:
        """A short plain-language briefing on what's on David's plate.

        Rebuilt every turn so the assistant can't be stale. Deliberately
        names people — "Karen Mills is waiting" is useful; "3 open items" is
        the kind of thing a dashboard says, not a person.
        """
        today = today or datetime.now(self._config.client.tz).date()
        lines: list[str] = []

        open_items = [
            d
            for d in self._drafts.list_open(limit=100)
            if d.kind in (DraftKind.REPLY, DraftKind.COMMENT)
        ]
        if open_items:
            names = [d.person_name for d in open_items if d.person_name]
            listed = ", ".join(names[:8])
            more = f" and {len(names) - 8} more" if len(names) > 8 else ""
            lines.append(f"Waiting for a reply from David: {listed}{more}.")
        else:
            lines.append("Nobody is waiting on a reply — he's caught up.")

        awaiting = [
            d
            for d in self._drafts.list_by_status([DraftStatus.PENDING_APPROVAL])
            if d.kind is DraftKind.POST
        ]
        if awaiting:
            post = awaiting[0]
            when = (
                post.scheduled_for.astimezone(self._config.client.tz).strftime("%A at %-I:%M %p")
                if post.scheduled_for
                else "soon"
            )
            lines.append(
                f'A post is waiting for his OK, planned for {when}. It reads: "{post.body[:400]}"'
            )
        else:
            lines.append("No post is waiting for his approval right now.")

        upcoming = next_posting_days(today, self._config, count=2)
        if upcoming:
            days = " and ".join(d.strftime("%A") for d in upcoming)
            lines.append(f"His next posting days are {days}.")

        return "\n".join(lines)

    # -- answering ---------------------------------------------------------

    def ask(self, question: str) -> ChatMessage:
        """Answer a question from David and store both sides of the exchange.

        Always returns an assistant message. If the model is unreachable,
        that message is the plain-language fallback — David gets an answer on
        screen either way, never a spinner that never resolves.
        """
        question = (question or "").strip()
        if not question:
            raise ValueError("empty question")

        self._chats.append("user", question)

        system = build_system_prompt(self._config, self.describe_situation())
        conversation = [
            {"role": message.role, "content": message.content} for message in self.history()
        ]

        answer = self._model.reply(system=system, messages=conversation)
        if not answer:
            logger.warning("Couldn't answer David's question; showed him the fallback.")
            answer = FALLBACK_REPLY

        return self._chats.append("assistant", answer)

    def clear(self) -> int:
        """Start the conversation over."""
        removed = self._chats.clear()
        logger.info("David cleared the chat (%s message(s)).", removed)
        return removed
