"""Check that the Gemini credentials and model name actually work.

What: makes one real drafting call and one real chat call through the app's own
provider code, then reports what came back. Writes nothing to the database.

Why: two things about Gemini fail quietly and look identical from the outside.
The key can be wrong, or — far more likely — the model name in config.yaml can
have been retired out from under us. Google removes model versions on its own
schedule; gemini-2.5-flash was the shipped default and started returning 404
"no longer available". Every drafting call then returns None, the inbox stores
cards with no suggested reply, and David simply sees fewer drafts. Nothing on
his screen says why.

This runs the real GeminiTextWriter and GeminiChatModel, not a hand-rolled
request, so a pass means the shipped code path works — not merely that the key
is valid. When the model is the problem it lists the ones the key can actually
reach, so the fix is a copy-paste into config.yaml.

Usage:
    .venv/bin/python scripts/check_gemini.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from app.config import PROJECT_ROOT, AppConfig, load_config  # noqa: E402
from app.llm import GEMINI_API_ROOT, build_chat_model, build_text_writer  # noqa: E402


def _live_models(api_key: str) -> list[str]:
    """Model names this key can actually call, newest-looking first."""
    import httpx

    names: list[str] = []
    for version in ("v1beta", "v1"):
        try:
            response = httpx.get(
                f"https://generativelanguage.googleapis.com/{version}/models",
                params={"key": api_key},
                timeout=30.0,
            )
            response.raise_for_status()
        except Exception:
            continue
        for model in response.json().get("models", []):
            if "generateContent" not in model.get("supportedGenerationMethods", []):
                continue
            name = model["name"].removeprefix("models/")
            # Text drafting only — skip image, audio and video variants.
            if any(skip in name for skip in ("image", "tts", "audio", "video", "embedding")):
                continue
            if name not in names:
                names.append(name)
    return names


def _report_failure(config: AppConfig, api_key: str) -> None:
    print(
        "\nNothing came back. The log line above says which it was — a 404 means\n"
        "the model name is the problem, not the key.\n"
    )
    models = _live_models(api_key)
    if not models:
        print("Couldn't list models either, so this is more likely the key or the network.")
        return

    print("Models this key can call right now:\n")
    for name in models:
        print(f"  {name}")
    print(
        f"\nconfig.yaml currently asks for:\n"
        f"  drafting.model: {config.drafting.model}\n"
        f"  chat.model:     {config.chat.model}\n"
        "\nIf those aren't in the list above, that's the whole problem."
    )


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=False)

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("GEMINI_API_KEY is not set in .env. See .env.example.")
        return 1

    config = load_config(PROJECT_ROOT / "config.yaml")

    print(f"drafting : provider={config.drafting.provider}  model={config.drafting.model}")
    print(f"chat     : provider={config.chat.provider}  model={config.chat.model}")
    if config.drafting.provider != "gemini" or config.chat.provider != "gemini":
        print(
            "\nNote: config.yaml doesn't have both of these on gemini. Testing the\n"
            "Gemini path anyway — flip them once this passes."
        )
    print()

    # Force gemini regardless of what's configured; the point is to test it.
    gemini = config.model_copy(
        update={
            "drafting": config.drafting.model_copy(update={"provider": "gemini"}),
            "chat": config.chat.model_copy(update={"provider": "gemini"}),
        }
    )

    draft = build_text_writer(gemini).write(
        system=(
            "You write short LinkedIn replies for David, a life coach. Warm, plain, "
            "no emojis, no hashtags. Two sentences maximum."
        ),
        prompt="Karen Mills messaged asking how your coaching cohort is going. Reply.",
        label="a test reply",
    )

    # Multi-turn on purpose: Gemini calls the assistant role "model", and a bad
    # mapping only shows up once there's a prior turn to map.
    answer = build_chat_model(gemini).reply(
        system="You are David's assistant. Answer in one short sentence.",
        messages=[
            {"role": "user", "content": "Who is waiting on me?"},
            {"role": "assistant", "content": "Karen Mills is waiting on a reply."},
            {"role": "user", "content": "What should I say to her?"},
        ],
    )

    if not draft or not answer:
        _report_failure(config, api_key)
        return 1

    print("DRAFTING — ok")
    print(f"  {draft.strip()[:300]}\n")
    print("CHAT (multi-turn) — ok")
    print(f"  {answer.strip()[:300]}\n")
    print(f"Gemini is working via {GEMINI_API_ROOT}. Nothing was written to the database.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
