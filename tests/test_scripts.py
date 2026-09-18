"""The one-time setup scripts.

Only one thing here is worth protecting, and it is worth protecting properly:
`get_gmail_token.py` writes the minted refresh token into `.env`. A careless
rewrite of that file would take the Gemini key and the dashboard PIN with it,
and the damage would be invisible until the next run failed for an unrelated
reason. These tests pin the file down.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT


def _load(name: str):
    """Import a script from scripts/, which is not an importable package."""
    path = PROJECT_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


write_refresh_token = _load("get_gmail_token")._write_refresh_token


SAMPLE = '''# Copy to .env and fill in. NEVER commit .env.
GEMINI_API_KEY="AIzaSyExample"
GMAIL_CLIENT_ID="abc.apps.googleusercontent.com"
GMAIL_CLIENT_SECRET="GOCSPX-example"
GMAIL_REFRESH_TOKEN=
GMAIL_USER="developer@example.com"

# Dashboard
DASHBOARD_PIN=1234
'''


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def test_the_token_lands_in_the_right_place(env_file: Path) -> None:
    assert write_refresh_token(env_file, "1//0gExampleToken") is True
    assert 'GMAIL_REFRESH_TOKEN="1//0gExampleToken"' in env_file.read_text(encoding="utf-8")


def test_nothing_else_in_the_file_is_touched(env_file: Path) -> None:
    """The failure that would really hurt: losing the other credentials."""
    write_refresh_token(env_file, "1//0gExampleToken")
    written = env_file.read_text(encoding="utf-8")

    for kept in (
        'GEMINI_API_KEY="AIzaSyExample"',
        'GMAIL_CLIENT_ID="abc.apps.googleusercontent.com"',
        'GMAIL_CLIENT_SECRET="GOCSPX-example"',
        'GMAIL_USER="developer@example.com"',
        "DASHBOARD_PIN=1234",
        "# Dashboard",
    ):
        assert kept in written, kept

    assert len(written.splitlines()) == len(SAMPLE.splitlines())


def test_the_key_is_replaced_not_duplicated(env_file: Path) -> None:
    """Two GMAIL_REFRESH_TOKEN lines and the loser silently wins on re-read."""
    write_refresh_token(env_file, "first")
    write_refresh_token(env_file, "second")

    written = env_file.read_text(encoding="utf-8")
    assert written.count("GMAIL_REFRESH_TOKEN=") == 1
    assert "second" in written and "first" not in written


def test_the_result_parses_back_out(env_file: Path) -> None:
    """A token that can't be read back is the same as no token at all."""
    from dotenv import dotenv_values

    write_refresh_token(env_file, "1//0gExampleToken")
    values = dotenv_values(env_file)
    assert values["GMAIL_REFRESH_TOKEN"] == "1//0gExampleToken"
    assert values["GEMINI_API_KEY"] == "AIzaSyExample"


def test_the_key_is_appended_when_absent(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("FOO=1", encoding="utf-8")  # no trailing newline
    assert write_refresh_token(path, "tok") is True
    assert path.read_text(encoding="utf-8") == 'FOO=1\nGMAIL_REFRESH_TOKEN="tok"\n'


def test_an_unwritable_path_reports_failure_instead_of_raising(tmp_path: Path) -> None:
    """The caller falls back to printing the token; an exception would lose it."""
    assert write_refresh_token(tmp_path / "missing" / ".env", "tok") is False
