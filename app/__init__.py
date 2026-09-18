"""David's LinkedIn Assistant — agent core.

What: a human-in-the-loop assistant that drafts LinkedIn posts, replies, and
comments for David Whittington, and hands them to him to send himself.

Why: LinkedIn's terms forbid automating the site, and David's account is his
primary business asset. See the compliance rules in README.md. Nothing here
contacts linkedin.com — ever.

How: Gmail (read-only) supplies LinkedIn's notification emails, Claude drafts
the text, David approves and acts, and Buffer publishes approved posts through
LinkedIn's official partner API.
"""

__version__ = "0.1.0"
