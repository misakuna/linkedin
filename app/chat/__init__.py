"""Chat — David asks the assistant questions.

Automation level: read and talk only. This module can see what's on David's
plate and suggest wording, and it can do nothing else. It cannot send, accept,
publish, or schedule anything, and its instructions say so explicitly — the
failure that would matter here is David believing something was sent when
nothing was.
"""

from app.chat.service import ChatService, build_system_prompt

__all__ = ["ChatService", "build_system_prompt"]
