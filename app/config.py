"""Load and validate config.yaml.

What: turns config.yaml into a typed, validated `AppConfig` object.

Why: magic values in code are forbidden. Everything tunable — posting days,
time windows, timezone, approval phone, voice notes — lives in one file, and a
typo there should fail loudly at boot rather than quietly at 4pm on a posting
day.

How: PyYAML reads the file, Pydantic validates it. Validation is strict:
unknown keys are rejected so a renamed-but-not-migrated key can't be silently
ignored.
"""

from __future__ import annotations

import os
import re
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Repo root — config.yaml sits beside this package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# E.164: a leading +, then 8–15 digits. Rejects "555-0100" and "(555) 555-0100".
E164 = re.compile(r"^\+[1-9]\d{7,14}$")

# "mock" = offline, no credentials, no network. "live" = the real API.
ProviderMode = Literal["mock", "live"]


class ConfigError(RuntimeError):
    """config.yaml is missing, unreadable, or invalid."""


class _Strict(BaseModel):
    """Base model that rejects unknown keys, so typos surface at boot."""

    model_config = ConfigDict(extra="forbid")


class ClientConfig(_Strict):
    name: str = Field(min_length=1)
    timezone: str

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @property
    def tz(self) -> ZoneInfo:
        """The client's timezone as a tzinfo object."""
        return ZoneInfo(self.timezone)


class PostingWindow(_Strict):
    start: time
    end: time

    @model_validator(mode="after")
    def _start_before_end(self) -> PostingWindow:
        if self.start >= self.end:
            raise ValueError(
                f"posting window start ({self.start}) must be before end ({self.end})"
            )
        return self

    @property
    def length_minutes(self) -> int:
        """Width of the window in minutes — the range we sample a post time from."""
        start_minutes = self.start.hour * 60 + self.start.minute
        end_minutes = self.end.hour * 60 + self.end.minute
        return end_minutes - start_minutes


class PostingConfig(_Strict):
    days: list[str] = Field(min_length=1)
    window: PostingWindow
    prepare_at: time
    themes: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _prepare_before_window(self) -> PostingConfig:
        """David must get the text before the earliest moment it could go out."""
        if self.prepare_at > self.window.start:
            raise ValueError(
                f"posting prepare_at ({self.prepare_at}) must not be after the window "
                f"start ({self.window.start}) — David needs the draft before it can post"
            )
        return self

    @field_validator("days")
    @classmethod
    def _valid_weekdays(cls, values: list[str]) -> list[str]:
        normalized = [day.strip().lower() for day in values]
        for day in normalized:
            if day not in WEEKDAYS:
                raise ValueError(f"{day!r} is not a weekday name (expected one of {WEEKDAYS})")
        if len(set(normalized)) != len(normalized):
            raise ValueError(f"duplicate posting days: {normalized}")
        return normalized


class ApprovalsConfig(_Strict):
    phone_number: str
    expires_after_hours: int = Field(gt=0, le=168)

    @field_validator("phone_number")
    @classmethod
    def _e164(cls, value: str) -> str:
        if not E164.match(value):
            raise ValueError(
                f"{value!r} is not an E.164 phone number (expected e.g. +15555550100)"
            )
        return value


class QuietHours(_Strict):
    """When David is not to be texted, in his own timezone."""

    start: time
    end: time

    def covers(self, moment: time) -> bool:
        """Whether a local time falls inside the quiet window.

        Handles the normal case of a window that wraps midnight (21:00 to
        07:00), which is the only shape anyone actually configures.
        """
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end


class AlertsConfig(_Strict):
    on_new_items: bool
    new_items_min_minutes: int = Field(ge=0, le=10080)
    daily_digest_at: time | None
    quiet_hours: QuietHours
    dashboard_url: str = ""

    @field_validator("dashboard_url")
    @classmethod
    def _url(cls, value: str) -> str:
        value = value.strip()
        if value and not value.startswith(("http://", "https://")):
            raise ValueError(
                f"{value!r} needs to start with https:// — it goes in a text message "
                "for David to tap"
            )
        return value.rstrip("/")


class VoiceConfig(_Strict):
    notes: str = Field(min_length=1)
    never: list[str] = Field(min_length=1)
    comment_must_reference_post: bool


AiProvider = Literal["mock", "claude", "gemini"]


class IntegrationsConfig(_Strict):
    gmail: ProviderMode
    buffer: ProviderMode
    sms: ProviderMode

    @property
    def all_mocked(self) -> bool:
        """True when none of these will make an external API call.

        The AI providers are checked separately — see AppConfig.all_mocked.
        """
        return all(mode == "mock" for mode in (self.gmail, self.buffer, self.sms))


class InboxConfig(_Strict):
    gmail_query: str = Field(min_length=1)
    max_messages_per_poll: int = Field(gt=0, le=500)
    poll_minutes: int = Field(gt=0, le=1440)


class DraftingConfig(_Strict):
    provider: AiProvider
    model: str = Field(min_length=1)
    max_tokens: int = Field(gt=0, le=64000)
    effort: Literal["low", "medium", "high"]
    seconds_between_calls: float = Field(ge=0, le=120)
    max_drafts_per_poll: int = Field(gt=0, le=200)
    max_words: int = Field(gt=0, le=500)
    draft_blind_replies: bool


class ChatConfig(_Strict):
    provider: AiProvider
    model: str = Field(min_length=1)
    max_tokens: int = Field(gt=0, le=32000)
    history_messages: int = Field(gt=0, le=500)


class StorageConfig(_Strict):
    database_path: str = Field(min_length=1)


class DashboardConfig(_Strict):
    html_path: str = Field(min_length=1)
    support_contact: str = Field(min_length=1)
    session_days: int = Field(gt=0, le=365)

    def resolved_html_path(self) -> Path:
        path = Path(self.html_path)
        return path if path.is_absolute() else (PROJECT_ROOT / path)


class AppConfig(_Strict):
    client: ClientConfig
    posting: PostingConfig
    approvals: ApprovalsConfig
    alerts: AlertsConfig
    voice: VoiceConfig
    integrations: IntegrationsConfig
    inbox: InboxConfig
    drafting: DraftingConfig
    chat: ChatConfig
    storage: StorageConfig
    dashboard: DashboardConfig
    open_decisions_resolved: bool

    @property
    def all_mocked(self) -> bool:
        """True when nothing in this process will make any external call."""
        return (
            self.integrations.all_mocked
            and self.drafting.provider == "mock"
            and self.chat.provider == "mock"
        )

    def database_path(self) -> Path:
        """Absolute path to the SQLite file.

        DATABASE_PATH in the environment wins — on Railway/Render it points at
        the mounted persistent volume. Otherwise the config value is resolved
        relative to the repo root.
        """
        override = os.environ.get("DATABASE_PATH", "").strip()
        raw = override or self.storage.database_path
        path = Path(raw)
        return path if path.is_absolute() else (PROJECT_ROOT / path)


def load_config(path: Path | None = None) -> AppConfig:
    """Read and validate config.yaml.

    Raises ConfigError with a message a human can act on — never a raw
    Pydantic traceback, because this is the first thing that fails on a bad
    deploy and the message ends up in Railway's log viewer.
    """
    config_path = path or Path(os.environ.get("CONFIG_PATH") or DEFAULT_CONFIG_PATH)

    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not read config file at {config_path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path} is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"{config_path} must contain a YAML mapping at the top level")

    try:
        return AppConfig.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError and friends
        raise ConfigError(f"{config_path} is invalid:\n{exc}") from exc


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Process-wide config singleton. Call `get_config.cache_clear()` in tests."""
    return load_config()
