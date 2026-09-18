"""Shared pytest fixtures.

Tests never touch a real API and never touch the real database file. Every test
gets a throwaway SQLite file in tmp_path, and config is loaded from the repo's
config.yaml (which ships with every integration set to "mock").
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT, AppConfig, get_config, load_config
from app.db import DraftRepository, init_db

# Secrets the suite pins to known values, and the real keys it blanks.
#
# app/env_secrets.py calls load_dotenv() at import, so without this the tests
# read whatever is in the developer's .env. That is how this broke: the suite
# passed on a fresh clone, then started failing the moment a real DASHBOARD_PIN
# was configured, because the login tests type "0000".
#
# Blanking the API keys matters more than the PIN. The module docstring above
# promises tests never touch a real API, and config.yaml shipping as all-mock
# is what enforces that today. If a future test builds a live client by hand —
# the way scripts/check_gemini.py does — a populated .env would have it spend
# real money against a real account. Now it gets a MissingSecretError instead.
_TEST_ENV = {
    "DASHBOARD_PIN": "0000",
    "SESSION_SECRET": "test-only-session-secret",
    "GEMINI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "GMAIL_CLIENT_ID": "",
    "GMAIL_CLIENT_SECRET": "",
    "GMAIL_REFRESH_TOKEN": "",
    "GMAIL_USER": "",
    "BUFFER_TOKEN": "",
    "BUFFER_PROFILE_ID": "",
    "SMS_ACCOUNT_SID": "",
    "SMS_AUTH_TOKEN": "",
    "SMS_FROM_NUMBER": "",
}


@pytest.fixture(autouse=True)
def isolated_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cut every test off from the developer's .env.

    Autouse and unconditional: a test that depends on which machine it runs on
    is worse than no test, because it passes in the place you'd notice.
    """
    for name, value in _TEST_ENV.items():
        if value:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
            os.environ.pop(name, None)


@pytest.fixture
def config() -> AppConfig:
    """The real config.yaml, validated — with the inter-call pause removed.

    drafting.seconds_between_calls paces real Gemini calls so an inbox poll
    doesn't trip the per-minute rate limit. Tests use mock writers that make no
    network calls, so honouring it would only sleep: at 7s a call, one poll test
    over a dozen fixtures takes over a minute and the suite becomes unrunnable.
    Zeroed here rather than in each test, so a future test can't reintroduce it
    by forgetting.
    """
    config = load_config(PROJECT_ROOT / "config.yaml")
    return config.model_copy(
        update={"drafting": config.drafting.model_copy(update={"seconds_between_calls": 0})}
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A fresh, initialized SQLite database for one test."""
    path = tmp_path / "test.db"
    init_db(path)
    return path


@pytest.fixture
def repository(db_path: Path) -> DraftRepository:
    """A repository pointed at the throwaway database."""
    return DraftRepository(db_path)


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A TestClient with the app booted against a throwaway database.

    Importing inside the fixture keeps FastAPI's TestClient (and the startup
    it triggers) out of tests that don't need it.
    """
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "app.db"))
    get_config.cache_clear()

    from app.main import app

    with TestClient(app) as client:
        yield client

    get_config.cache_clear()
