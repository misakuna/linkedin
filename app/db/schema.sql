-- Schema for David's LinkedIn Assistant.
--
-- Applied on every boot with CREATE ... IF NOT EXISTS, so a redeploy against
-- the mounted volume is a no-op rather than a migration.

-- Every piece of text the assistant writes: post drafts, reply drafts,
-- comment drafts. Status drives the whole dashboard.
CREATE TABLE IF NOT EXISTS drafts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,

    -- What kind of thing this is. Posts go to Buffer after approval; replies
    -- and comments are copied by David and pasted into LinkedIn by hand.
    kind          TEXT NOT NULL CHECK (kind IN ('post', 'reply', 'comment')),

    -- draft -> pending_approval -> approved -> done, or -> rejected.
    status        TEXT NOT NULL CHECK (status IN (
                      'draft', 'pending_approval', 'approved', 'done', 'rejected'
                  )),

    -- The drafted text itself. David may edit this before using it.
    body          TEXT NOT NULL,

    -- Who this is for/about, in David's words ("Karen Mills"). NULL for posts.
    person_name   TEXT,

    -- Their LinkedIn headline ("Founder & Executive Coach"). Shown on the card
    -- so David remembers who someone is before he replies.
    headline      TEXT,

    -- What LinkedIn told us happened: message_received, connection_request,
    -- connection_accepted, mention. Kept because `kind` above says how David
    -- will send his answer, not what he is answering — both a new message and
    -- an invitation produce a 'reply'. The dashboard needs this to write the
    -- plain-language summary line.
    notification_type TEXT,

    -- Deep link back into LinkedIn, lifted out of the notification email.
    -- This is what the dashboard's [Open in LinkedIn] button uses.
    linkedin_url  TEXT,

    -- Where this came from, e.g. a Gmail message id. UNIQUE-indexed below so
    -- re-polling the same inbox cannot create duplicate drafts.
    source_ref    TEXT,

    -- For posts: the randomly-sampled publish time (ISO 8601, client tz).
    scheduled_for TEXT,

    -- Set the instant a publisher call is about to be made, and only ever by a
    -- conditional UPDATE that requires it to be NULL. Three callers can reach
    -- the same approved post at once — the retry sweep, the SMS webhook and the
    -- dashboard button — and the publisher call takes up to 30s, so "still
    -- approved?" is not a safe question to ask before making it. Winning this
    -- claim is what earns the right to call Buffer.
    publish_claimed_at TEXT,

    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- Idempotent inbox polling: one draft per source email.
CREATE UNIQUE INDEX IF NOT EXISTS idx_drafts_source_ref
    ON drafts (source_ref) WHERE source_ref IS NOT NULL;

-- The dashboard's Today screen is "everything not yet done", by status.
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts (status, kind);
CREATE INDEX IF NOT EXISTS idx_drafts_scheduled ON drafts (scheduled_for);

-- Audit trail of every status change. Two jobs: it is what the plain-language
-- INFO log lines are generated from, and it answers "what happened to that
-- post?" weeks later when David asks about it.
CREATE TABLE IF NOT EXISTS draft_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id    INTEGER NOT NULL REFERENCES drafts (id) ON DELETE CASCADE,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_draft_events_draft ON draft_events (draft_id, id);

-- David's conversation with the assistant. One user, one long-running thread,
-- so there is no conversation id — just messages in order.
CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_time ON chat_messages (id);

-- When each kind of alert text was last sent to David.
--
-- Exists purely so a restart cannot un-throttle him. Without it, every deploy
-- resets the "last texted" clock, and a Railway redeploy during a busy morning
-- would text him again immediately. One row per alert kind; the row is the
-- state.
CREATE TABLE IF NOT EXISTS alerts_sent (
    kind    TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);

-- Twilio message ids we have already acted on.
--
-- Twilio's signature covers the URL and the parameters, and nothing else — no
-- nonce, no timestamp. So a signed request stays valid forever, and anyone who
-- captures one copy of David's "YES" can replay it: each replay approves the
-- next post waiting on him, because the handler always takes the oldest. One
-- observed request would become a standing permission to publish.
--
-- Twilio sends a unique MessageSid per message, so recording it and refusing
-- repeats is what turns a signed request into a single-use one.
-- NOT NULL is load-bearing, not decoration: SQLite does not imply it for a
-- TEXT PRIMARY KEY, so without it NULL ids would insert over and over without
-- ever colliding, and every replay would be waved through. The webhook already
-- rejects a missing id, but this is the constraint that makes the guard hold
-- even if that check is ever moved or removed.
CREATE TABLE IF NOT EXISTS sms_messages_seen (
    message_sid TEXT PRIMARY KEY NOT NULL,
    seen_at     TEXT NOT NULL
);
