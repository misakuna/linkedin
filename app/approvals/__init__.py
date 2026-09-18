"""Approvals — the text-message approval loop.

Automation level: notify and wait. David gets a text with the drafted post, and
the system does nothing further until he answers. This module is the only path
from `pending_approval` to `approved`, which is what makes "nothing is published
that David hasn't read" a property of the code rather than a promise.
"""

from app.approvals.service import ApprovalService, ReplyOutcome
from app.approvals.sms import SmsSender, build_sms_sender, shorten_for_sms

__all__ = [
    "ApprovalService",
    "ReplyOutcome",
    "SmsSender",
    "build_sms_sender",
    "shorten_for_sms",
]
