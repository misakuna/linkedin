"""Picking the time a post goes out.

What: `publish_time_for(day)` — the randomly-chosen publish time for one date.

Why: posting at exactly 9:00 every Tuesday looks automated, which defeats the
point of a relationship-first presence. So each posting day gets a time sampled
uniformly from a window.

The catch is that a scheduler restarts. If the time were sampled fresh on every
run, a container restart at 10am could move a post that was already scheduled
for 9:14 — or schedule a second one. So the sample is **seeded by the date**:
asking twice for the same day always gives the same answer, and no state has to
be persisted to make that true.

How: SHA-256 of a seed string, used as the seed for a private RNG. Deliberately
not Python's `hash()`, which is salted per process and would give a different
answer after every restart — exactly the bug this design exists to avoid.
"""

from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, timedelta

from app.config import AppConfig

# Bumping this re-rolls every future posting time. Only useful if the sampled
# times ever turn out to cluster badly in practice.
SEED_SALT = "davids-linkedin-assistant-v1"


def is_posting_day(day: date, config: AppConfig) -> bool:
    """Whether David posts on this date, per config.yaml."""
    weekday = day.strftime("%A").lower()
    return weekday in config.posting.days


def publish_time_for(day: date, config: AppConfig) -> datetime:
    """The publish time for a given date, in David's timezone.

    Deterministic: the same date always yields the same time, on any machine,
    in any process, forever. Re-running today's job cannot move today's post.
    """
    window = config.posting.window

    seed_text = f"{SEED_SALT}|{config.client.name}|{day.isoformat()}"
    digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(digest, "big"))

    # Uniform over the whole window, to the minute. Minute resolution is
    # deliberate — second-level precision would be false precision, since
    # Buffer's own queue doesn't promise it.
    offset = rng.randrange(window.length_minutes)

    start = datetime.combine(day, window.start, tzinfo=config.client.tz)
    return start + timedelta(minutes=offset)


def prepare_time_for(day: date, config: AppConfig) -> datetime:
    """When the draft gets written and texted to David, on a given date."""
    return datetime.combine(day, config.posting.prepare_at, tzinfo=config.client.tz)


def next_posting_days(after: date, config: AppConfig, count: int = 4) -> list[date]:
    """The next few posting dates, for showing David what's coming up."""
    found: list[date] = []
    day = after
    # 21 days is three weeks — enough to find several matches for any
    # reasonable schedule without looping forever on a misconfiguration.
    for _ in range(21):
        day = day + timedelta(days=1)
        if is_posting_day(day, config):
            found.append(day)
            if len(found) == count:
                break
    return found
