"""Everything that talks to a language model.

What: two interfaces — `TextWriter` for one-shot drafting, `ChatModel` for
conversation — each with a Claude, a Gemini, and an offline implementation.

Why one file: drafting and chat need the same three things from a provider
(retry, graceful degradation, a plain-language log line), and the Gemini REST
call was being written twice. Keeping every provider here means adding a fourth
is one file to open, and swapping providers is one config line per feature.

How: providers are chosen by `drafting.provider` and `chat.provider` in
config.yaml, independently — so drafting can run on one model and chat on
another if that's ever the right trade.

Every call fails soft. A provider that's down, rate-limited, or refusing
returns None, which the callers turn into "no draft" or the plain-language
fallback reply. Nothing here raises into a scheduled job or a web request.
"""

from __future__ import annotations

import time
from typing import Protocol

from app.config import AppConfig
from app.logging_setup import get_logger

logger = get_logger(__name__)

GEMINI_API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"

# The Claude SDK retries transient failures for us (max_retries=3). The Gemini
# path is a hand-rolled REST call, so it needs its own — and it needs it for the
# same reason: 503 "model is currently experiencing high demand" is common
# enough to hit in normal use, and without a retry David sees "Something didn't
# work — text support" for something that would have cleared in a second.
GEMINI_ATTEMPTS = 3
GEMINI_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# 5xx is Gemini shedding load and usually clears in a second.
GEMINI_BACKOFF_SECONDS = 1

# 429 is different in kind, not degree. The free tier caps requests per MINUTE,
# so retrying a second later lands in the same window and fails identically —
# which is exactly what happened: an inbox poll produced 12 cards and only 4
# drafts, every failure a 429 that had been "retried" twice inside two seconds.
# Waiting out the window is the only thing that works.
GEMINI_RATE_LIMIT_BACKOFF_SECONDS = 20


class TextWriter(Protocol):
    """Turns a prompt into a piece of text. Used for drafts."""

    def write(self, *, system: str, prompt: str, label: str) -> str | None:
        """Return generated text, or None if it couldn't be produced.

        `label` is a short human phrase for the log ("a reply to Karen Mills") —
        these lines are how the developer supports David remotely.
        """
        ...


class ChatModel(Protocol):
    """Continues a conversation. Used for the Ask screen."""

    def reply(self, *, system: str, messages: list[dict[str, str]]) -> str | None:
        """Answer the latest message, or return None if it couldn't."""
        ...


# --------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------


class _ClaudeCaller:
    """Shared Claude plumbing: one client, one set of error branches."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic

            from app import env_secrets

            self._client = anthropic.Anthropic(
                api_key=env_secrets.anthropic_api_key(),
                # The SDK already retries 429s and 5xx with backoff.
                max_retries=3,
            )
        return self._client

    def _call(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: list[dict[str, str]],
        label: str,
        effort: str | None = None,
    ) -> str | None:
        import anthropic

        from app import env_secrets

        extra = {"output_config": {"effort": effort}} if effort else {}

        try:
            response = self._get_client().messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                **extra,
            )
        except env_secrets.MissingSecretError as exc:
            logger.error("Can't write %s: %s", label, exc)
            return None
        except anthropic.AuthenticationError:
            logger.error("Claude rejected our API key, so I couldn't write %s.", label)
            return None
        except anthropic.RateLimitError:
            logger.warning("Claude is rate-limiting us, so I couldn't write %s.", label)
            return None
        except anthropic.APIStatusError as exc:
            logger.error("Claude returned an error (%s) while writing %s.", exc.status_code, label)
            return None
        except anthropic.APIConnectionError:
            logger.warning("Couldn't reach Claude to write %s. Will try again later.", label)
            return None
        except Exception:
            logger.exception("Something unexpected went wrong writing %s.", label)
            return None

        # A safety refusal arrives as a normal 200 with empty or partial
        # content, so it has to be checked before reading the blocks.
        if response.stop_reason == "refusal":
            logger.warning("Claude declined to write %s. Nothing was saved.", label)
            return None

        if response.stop_reason == "max_tokens":
            logger.warning(
                "%s was cut off at the length limit. Raise max_tokens in config.yaml "
                "if this keeps happening.",
                label,
            )

        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()
        if not text:
            logger.warning("Claude returned nothing usable for %s.", label)
        return text or None


class ClaudeTextWriter(_ClaudeCaller):
    """Drafting on the Claude API."""

    name = "claude"

    def write(self, *, system: str, prompt: str, label: str) -> str | None:
        drafting = self._config.drafting
        text = self._call(
            model=drafting.model,
            max_tokens=drafting.max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            label=label,
            effort=drafting.effort,
        )
        if text:
            logger.info("Wrote %s.", label)
        return text


class ClaudeChatModel(_ClaudeCaller):
    """Chat on the Claude API."""

    name = "claude"

    def reply(self, *, system: str, messages: list[dict[str, str]]) -> str | None:
        chat = self._config.chat
        return self._call(
            model=chat.model,
            max_tokens=chat.max_tokens,
            system=system,
            messages=messages,
            label="an answer for David",
        )


# --------------------------------------------------------------------------
# Gemini
#
# Talks to the REST endpoint with httpx rather than adding the Google SDK —
# one endpoint doesn't justify a dependency, and httpx is already here.
#
# VERIFIED against a live key on 2026-07-28: drafting, chat, and multi-turn
# (which is the only case that exercises the assistant -> "model" role mapping).
# Re-check with scripts/check_gemini.py.
#
# The request shape held; the MODEL NAME did not. gemini-2.5-flash was the
# shipped default and now returns 404 "no longer available" on both v1 and
# v1beta. Google retires model versions on its own schedule, and the failure is
# silent from David's side: every call returns None, so replies simply stop
# being drafted and nothing on his screen explains it. Treat a 404 here as a
# stale config.yaml model name, not a broken client.
# --------------------------------------------------------------------------


class _GeminiCaller:
    """Shared Gemini plumbing."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def _call(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: list[dict[str, str]],
        label: str,
        retry_rate_limit: bool = True,
    ) -> str | None:
        """`retry_rate_limit=False` for background work.

        A 429 means a per-minute quota, so riding it out costs 20s or more.
        That is worth it for chat, where David is watching a spinner and there
        is exactly one call. It is wrong for an inbox poll, which is already
        paced and whose misses get filled in by the next run — waiting there
        just makes a background job outlast its own interval.
        """
        import httpx

        from app import env_secrets

        try:
            api_key = env_secrets.gemini_api_key()
        except env_secrets.MissingSecretError as exc:
            logger.error("Can't write %s: %s", label, exc)
            return None

        # Gemini calls the assistant role "model" and wraps text in parts.
        contents = [
            {
                "role": "model" if message["role"] == "assistant" else "user",
                "parts": [{"text": message["content"]}],
            }
            for message in messages
        ]

        request = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"maxOutputTokens": max_tokens},
        }

        payload = None
        for attempt in range(GEMINI_ATTEMPTS):
            try:
                response = httpx.post(
                    f"{GEMINI_API_ROOT}/{model}:generateContent",
                    params={"key": api_key},
                    json=request,
                    timeout=60.0,
                )
                response.raise_for_status()
                payload = response.json()
                break
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                last = attempt == GEMINI_ATTEMPTS - 1

                if status == 429 and not retry_rate_limit:
                    logger.info(
                        "Gemini rate-limited %s. Leaving it for the next poll.", label
                    )
                    return None

                if status in GEMINI_RETRY_STATUSES and not last:
                    base = (
                        GEMINI_RATE_LIMIT_BACKOFF_SECONDS
                        if status == 429
                        else GEMINI_BACKOFF_SECONDS
                    )
                    # 503 is Gemini shedding load and 429 is a rate limit; both
                    # clear on their own. Without this David gets "Something
                    # didn't work — text support" for a blip that would have
                    # resolved in a second, which is the kind of flakiness that
                    # makes someone stop trusting a tool.
                    delay = base * (2**attempt)
                    logger.info(
                        "Gemini returned %s writing %s. Retrying in %ss.",
                        status,
                        label,
                        delay,
                    )
                    time.sleep(delay)
                    continue

                if status == 429:
                    logger.warning(
                        "Gemini is rate-limiting us, so I couldn't write %s. On the free "
                        "tier this is the daily or per-minute cap.",
                        label,
                    )
                elif status == 503:
                    logger.warning(
                        "Gemini is overloaded and stayed overloaded across %s attempts, "
                        "so I couldn't write %s.",
                        GEMINI_ATTEMPTS,
                        label,
                    )
                elif status in (401, 403):
                    logger.error("Gemini rejected our API key, so I couldn't write %s.", label)
                else:
                    logger.error("Gemini returned an error (%s) while writing %s.", status, label)
                return None
            except httpx.TimeoutException:
                if attempt < GEMINI_ATTEMPTS - 1:
                    logger.info("Gemini timed out writing %s. Retrying.", label)
                    continue
                logger.warning("Gemini kept timing out, so I couldn't write %s.", label)
                return None
            except Exception:
                logger.exception("Couldn't reach Gemini to write %s. Will try again later.", label)
                return None

        if payload is None:
            return None

        text = gemini_text(payload)
        if not text:
            logger.warning(
                "Gemini returned nothing usable for %s. This is usually a safety "
                "block rather than an outage.",
                label,
            )
        return text


class GeminiTextWriter(_GeminiCaller):
    """Drafting on the Gemini API."""

    name = "gemini"

    def write(self, *, system: str, prompt: str, label: str) -> str | None:
        drafting = self._config.drafting
        text = self._call(
            model=drafting.model,
            max_tokens=drafting.max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            label=label,
            # Background work. A rate-limited draft is picked up next poll.
            retry_rate_limit=False,
        )
        if text:
            logger.info("Wrote %s.", label)
        return text


class GeminiChatModel(_GeminiCaller):
    """Chat on the Gemini API."""

    name = "gemini"

    def reply(self, *, system: str, messages: list[dict[str, str]]) -> str | None:
        chat = self._config.chat
        return self._call(
            model=chat.model,
            max_tokens=chat.max_tokens,
            system=system,
            messages=messages,
            label="an answer for David",
        )


def gemini_text(payload: object) -> str | None:
    """Pull the generated text out of a Gemini response.

    FRAGILE: Google's response shape. A blocked or empty candidate carries no
    parts at all, which is why every level is checked rather than indexed.
    """
    if not isinstance(payload, dict):
        return None
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    parts = (candidates[0] or {}).get("content", {}).get("parts")
    if not isinstance(parts, list):
        return None
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
    return text or None


# --------------------------------------------------------------------------
# Offline
# --------------------------------------------------------------------------


class MockTextWriter:
    """Offline stand-in for drafting. Makes no network calls.

    Returns text that is obviously a placeholder, on purpose. A convincing mock
    would let a broken live path pass unnoticed — and worse, could end up
    pasted into LinkedIn under David's name during testing.
    """

    name = "mock"

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def write(self, *, system: str, prompt: str, label: str) -> str | None:
        logger.info("Wrote %s. (Mock — no AI call was made.)", label)
        return (
            "[MOCK DRAFT — not written by AI, do not send]\n"
            f"This is where the drafted text for {label} will appear once "
            "drafting is switched on in config.yaml."
        )


class MockChatModel:
    """Offline stand-in for chat."""

    name = "mock"

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def reply(self, *, system: str, messages: list[dict[str, str]]) -> str | None:
        asked = messages[-1]["content"] if messages else ""
        logger.info("Answered David. (Mock — no AI call was made.)")
        return (
            "[Demo answer — no AI was used]\n\n"
            f'You asked: "{asked[:180]}"\n\n'
            "Once chat is switched on in config.yaml, a real answer appears here."
        )


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

_TEXT_WRITERS = {"claude": ClaudeTextWriter, "gemini": GeminiTextWriter, "mock": MockTextWriter}
_CHAT_MODELS = {"claude": ClaudeChatModel, "gemini": GeminiChatModel, "mock": MockChatModel}


def build_text_writer(config: AppConfig) -> TextWriter:
    """The drafting model named in config.yaml."""
    return _TEXT_WRITERS[config.drafting.provider](config)


def build_chat_model(config: AppConfig) -> ChatModel:
    """The chat model named in config.yaml."""
    return _CHAT_MODELS[config.chat.provider](config)
