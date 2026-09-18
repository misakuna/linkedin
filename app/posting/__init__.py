"""Posting — draft posts, pick a random time, publish via Buffer after approval.

Automation level: full, but only *after* David approves the exact text. This is
the one sanctioned automation touchpoint in the system, and it publishes only
text David has explicitly approved,
and it reaches LinkedIn through an official partner's API — never through the
LinkedIn website.

The publish time for each posting day is sampled randomly within a window and
seeded by the date, so a restart can't move or duplicate a scheduled post.
"""

from app.posting.publisher import PublishReceipt, Publisher, build_publisher
from app.posting.schedule import is_posting_day, next_posting_days, publish_time_for
from app.posting.service import PostingService, PrepareResult, source_ref_for

__all__ = [
    "PostingService",
    "PrepareResult",
    "PublishReceipt",
    "Publisher",
    "build_publisher",
    "is_posting_day",
    "next_posting_days",
    "publish_time_for",
    "source_ref_for",
]
