"""Plain-language logging to stdout.

What: one logging configuration for the whole service.

Why: the developer supports David remotely by reading Railway/Render's log viewer.
Log lines are the only window into what the assistant did today, so they read
as sentences a human can scan ("Drafted a reply to Karen Mills"), not as
key=value telemetry.

How: a single stdout handler with a timestamp and level. Modules call
`get_logger(__name__)` and write sentences.
"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False

_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: int = logging.INFO) -> None:
    """Configure stdout logging once, idempotently.

    Safe to call from the app entrypoint, tests, and one-off scripts; repeat
    calls are no-ops so we never end up with doubled log lines.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt=_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]

    # These libraries are chatty at INFO and drown out the lines the developer needs.
    for noisy in ("apscheduler.scheduler", "apscheduler.executors.default", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger, configuring stdout logging on first use."""
    setup_logging()
    return logging.getLogger(name)
