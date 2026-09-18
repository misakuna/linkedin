"""Drafting replies in David's voice.

What: turns a `ParsedNotification` into suggested text for David to send.

Why: the drafts ARE the product. The worst possible outcome for this build
precisely — "a generic AI comment appearing under David's name on a
high-profile connection's post" — so this module is built to refuse rather than
to guess.

How: the voice notes and the "never" list from config.yaml become the system
prompt; the parsed notification becomes the request. Nothing is ever sent
anywhere: drafts are stored, and David copies them himself.

### The blind-reply problem (OPEN DECISION)

LinkedIn's message notification emails do not contain the message. They say
"You have 1 new message" and give a name, a headline, and a link — that is all.
So for `message_received` there is literally nothing to reply *to*.

Two options, and the config flag `drafting.draft_blind_replies` picks between
them:

  false (default) — no draft. The card reads "Ephraim Adeyemo messaged you"
    with an [Open in LinkedIn] button. Honest, and David does what he'd do
    anyway: read it and reply like a person.

  true — draft a warm opener from the name and headline alone. Saves David
    typing, at the cost of a reply that cannot possibly reference what was
    actually said.

Default is false because a plausible-but-hollow reply under David's name is
worse than no reply at all. Flip it in config.yaml to change that — no
code change needed.
"""

from __future__ import annotations

from app.llm import TextWriter, build_text_writer
from app.config import AppConfig
from app.db import DraftKind
from app.inbox.models import NotificationType, ParsedNotification
from app.logging_setup import get_logger

logger = get_logger(__name__)

# What kind of thing David will end up sending, per notification type. Comments
# on his posts get answered with a comment; everything else with a message.
DRAFT_KINDS: dict[NotificationType, DraftKind] = {
    NotificationType.MESSAGE_RECEIVED: DraftKind.REPLY,
    NotificationType.CONNECTION_REQUEST: DraftKind.REPLY,
    NotificationType.CONNECTION_ACCEPTED: DraftKind.REPLY,
    NotificationType.MENTION: DraftKind.COMMENT,
}

# What David is actually trying to do in each case. Given to Claude as the task
# so the draft has a purpose rather than just a tone.
INTENTS: dict[NotificationType, str] = {
    NotificationType.MESSAGE_RECEIVED: (
        "They sent David a direct message on LinkedIn. Write a short, warm opening "
        "reply that acknowledges them by name and invites them to say more."
    ),
    NotificationType.CONNECTION_REQUEST: (
        "They asked to connect with David. Write a short note David can send back "
        "after accepting — friendly, specific to who they are, and with no pitch "
        "of any kind."
    ),
    NotificationType.CONNECTION_ACCEPTED: (
        "They accepted David's invitation, so they are a brand-new connection. "
        "Write a short hello that thanks them for connecting and shows genuine "
        "interest in what they do. No pitch, no ask."
    ),
    NotificationType.MENTION: (
        "They commented on David's post. Write a short reply comment that responds "
        "to what they actually said. It must reference something specific from "
        "their comment — a generic thank-you is worse than saying nothing."
    ),
}


# Types where a reply is only meaningful if we know what was *said*. Knowing
# who someone is doesn't help you answer a message you can't read, and a
# comment reply that doesn't engage with the comment is the exact failure
# this build guards against. For invitations and new connections it's different —
# there is no prior message to answer, and their headline genuinely is the
# thing a person would remark on.
NEEDS_THEIR_WORDS: frozenset[NotificationType] = frozenset(
    {NotificationType.MESSAGE_RECEIVED, NotificationType.MENTION}
)


def _has_something_specific_to_say(notification: ParsedNotification) -> bool:
    """Whether we know enough to write something that couldn't fit anybody."""
    if notification.type in NEEDS_THEIR_WORDS:
        return bool(notification.preview)
    return bool(notification.preview or notification.headline)


def build_system_prompt(config: AppConfig) -> str:
    """The voice instructions, assembled from config.yaml.

    Everything here is configuration rather than code, because tuning David's
    voice is a conversation with David — not a deploy.
    """
    voice = config.voice
    never = "\n".join(f"- {rule}" for rule in voice.never)

    return f"""You are drafting LinkedIn replies on behalf of {config.client.name}.
David will read what you write, and if he likes it he will paste it into
LinkedIn and send it himself under his own name. Write as David, in the first
person. Never mention that you are an assistant.

HOW DAVID WRITES
{voice.notes.strip()}

NEVER DO ANY OF THESE
{never}

RULES
- Keep it under {config.drafting.max_words} words. Shorter is almost always better.
- Use the person's first name once, naturally, the way you would in conversation.
- Refer to something specific about this person or what they said. If you have
  nothing specific to refer to, keep it genuinely short rather than padding it
  with warmth that could apply to anybody.
- Never invent facts about David, about the other person, or about any shared
  history. If you don't know something, don't imply that you do.
- Never ask for a meeting, a call, or business of any kind. These messages
  build the relationship; David decides if and when anything else happens.
- Reply with the message text only. No greeting line like "Here's a draft",
  no subject line, no quotation marks around it, no sign-off block."""


def build_prompt(notification: ParsedNotification) -> str:
    """The per-notification request: who, what they said, what David is doing."""
    intent = INTENTS.get(notification.type, "Write a short, friendly reply.")

    lines = [intent, "", f"Their name: {notification.person_name}"]
    if notification.headline:
        lines.append(f"Their LinkedIn headline: {notification.headline}")
    if notification.preview:
        lines.append(f'What they said, word for word: "{notification.preview}"')
    else:
        lines.append(
            "LinkedIn did not include what they said, so do not refer to the "
            "contents of their message."
        )
    return "\n".join(lines)


class ReplyDrafter:
    """Writes reply drafts for parsed notifications."""

    def __init__(self, config: AppConfig, writer: TextWriter | None = None) -> None:
        self._config = config
        self._writer = writer or build_text_writer(config)
        self._system = build_system_prompt(config)

    def should_draft(self, notification: ParsedNotification) -> bool:
        """Whether this notification is worth spending a draft on.

        Three gates: is it actionable at all, do we know who it's from, and do
        we have anything specific to say back? The last one is what keeps
        hollow drafts out of David's dashboard — see the blind-reply note at
        the top of this module.
        """
        if not notification.is_actionable or not notification.person_name:
            return False
        if _has_something_specific_to_say(notification):
            return True
        return self._config.drafting.draft_blind_replies

    def draft(self, notification: ParsedNotification) -> str | None:
        """Return drafted text, or None when we shouldn't or couldn't write one.

        None is a normal outcome, not an error: the dashboard shows the card
        with its [Open in LinkedIn] button and no suggested text.
        """
        if not self.should_draft(notification):
            if notification.is_actionable:
                logger.info(
                    "No draft for %s's %s — LinkedIn didn't tell us what was said, "
                    "so David will read it himself.",
                    notification.person_name or "someone",
                    notification.type.value.replace("_", " "),
                )
            return None

        label = f"a reply to {notification.person_name}"
        text = self._writer.write(
            system=self._system,
            prompt=build_prompt(notification),
            label=label,
        )
        return text.strip() if text else None


def draft_kind_for(notification: ParsedNotification) -> DraftKind:
    """Which kind of draft row this notification becomes."""
    return DRAFT_KINDS.get(notification.type, DraftKind.REPLY)
