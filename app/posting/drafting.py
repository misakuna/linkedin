"""Writing David's LinkedIn posts.

What: turns a date into drafted post text.

Why: same reasoning as the reply drafter — the words are the product, and they
have to sound like David rather than like a marketing department.

How: the voice notes from config.yaml become the system prompt, and one theme
from `posting.themes` becomes the subject. The theme is chosen by date, using
the same seeded approach as the publish time, so re-running a day's job doesn't
swap the subject out from under a draft David has already been texted about.
"""

from __future__ import annotations

import hashlib
from datetime import date

from app.llm import TextWriter, build_text_writer
from app.config import AppConfig
from app.logging_setup import get_logger

logger = get_logger(__name__)


def theme_for(day: date, config: AppConfig) -> str:
    """The subject for a given posting day.

    Deterministic per date, and walks the whole list before repeating, so
    David doesn't get the same theme twice in one week.
    """
    themes = config.posting.themes
    digest = hashlib.sha256(f"theme|{day.isoformat()}".encode("utf-8")).digest()
    return themes[int.from_bytes(digest, "big") % len(themes)]


def build_system_prompt(config: AppConfig) -> str:
    """Voice instructions for post writing, assembled from config.yaml."""
    voice = config.voice
    never = "\n".join(f"- {rule}" for rule in voice.never)

    return f"""You are writing a LinkedIn post on behalf of {config.client.name}.
David will read it, and if he likes it he will publish it under his own name.
Write as David, in the first person. Never mention that you are an assistant.

HOW DAVID WRITES
{voice.notes.strip()}

NEVER DO ANY OF THESE
{never}

RULES
- Under {config.drafting.max_words} words. A short post that says one true
  thing beats a long one that says four.
- Open with something concrete — a moment, a person, a specific thing that
  happened. Never open with a question to the audience or a bare statement of
  a lesson.
- One idea per post. Do not summarise at the end; trust the reader.
- No call to action. No "DM me". No "what do you think?". David is building
  relationships, not generating leads.
- Never invent a specific person, client, result, number, or event. If you
  need an example, keep it general enough to be honestly true of David's work.
- Reply with the post text only. No title, no preamble, no hashtags,
  no quotation marks around it."""


def build_prompt(day: date, config: AppConfig) -> str:
    """The per-day request: the theme, and a reminder about honesty."""
    return (
        f"Write David's LinkedIn post for {day.strftime('%A %-d %B')}.\n\n"
        f"The theme for this one: {theme_for(day, config)}\n\n"
        "Write it the way David would actually say it out loud. If you find "
        "yourself reaching for a specific story you don't have, stay general "
        "and true rather than inventing a detail."
    )


class PostDrafter:
    """Writes post drafts."""

    def __init__(self, config: AppConfig, writer: TextWriter | None = None) -> None:
        self._config = config
        self._writer = writer or build_text_writer(config)
        self._system = build_system_prompt(config)

    def draft(self, day: date) -> str | None:
        """Draft the post for a date, or None if it couldn't be written.

        None means no post that day. That is the right failure: an empty or
        half-written post reaching David's phone for approval would be worse
        than silence, and the next posting day comes around soon enough.
        """
        text = self._writer.write(
            system=self._system,
            prompt=build_prompt(day, self._config),
            label=f"David's post for {day.strftime('%A %-d %B')}",
        )
        return text.strip() if text else None
