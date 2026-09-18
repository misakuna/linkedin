# Test fixtures

Real LinkedIn notification emails, scrubbed and renamed. The inbox parser is
written against these, test-first.

Two sources: David's mailbox (the client) and Joshua's (the developer). Mixing
them is fine — LinkedIn renders these from templates that are identical
regardless of recipient. What differs per recipient is names and tokens, and
the parser extracts those rather than depending on them.

## Naming convention

The filename declares the classification the parser must produce — the tests
read the filename as the expected answer. The type is everything up to the
index number; anything after it is a human-readable hint and is ignored:

```text
message_received_01.eml               -> message_received
connection_accepted_02_digest.eml     -> connection_accepted
other_03_linkedin_news.eml            -> other
```

## Inventory (20 files)

### Actionable — David should do something

| File | Template | Source | Notes |
|---|---|---|---|
| `message_received_01.eml` | `email_member_message_v2` | David | Ephraim Adeyemo sent a DM |
| `message_received_02.eml` | `email_member_message_v2` | Joshua | Usman Gul sent a DM |
| `connection_request_01.eml` | `email_m2m_invite_single_01` | David | Nina Nguyen |
| `connection_request_02.eml` | `email_m2m_invite_single_01` | David | Isaac Okyere Asamoah, has connections-in-common block |
| `connection_request_03.eml` | `email_m2m_invite_single_01` | David | Mustafa Uğraşkan |
| `connection_request_04.eml` | `email_m2m_invite_single_01` | Joshua | Raymond Obikwelu |
| `connection_accepted_01.eml` | `email_accept_invite_single_01` | David | Angie Enriquez accepted |
| `connection_accepted_02_digest.eml` | `email_accept_invite_digest_02` | David | **Digest — three people in one email** |
| `mention_01_comment_on_post.eml` | `email_notification_single_commented_on_your_update_01` | Joshua | Bernard Lim commented on Joshua's post |

### Noise — the parser must NOT surface these

| File | Template | Notes |
|---|---|---|
| `other_01_premium_upsell.eml` | *(none)* | Premium marketing, no `X-LinkedIn-*` headers |
| `other_02_profile_views.eml` | *(none)* | "Your profile is getting hits", no headers |
| `other_03_linkedin_news.eml` | `email_editorial_suggested_top_conversations_01` | LinkedIn News |
| `other_04_newsletter.eml` | `email_series_follow_newsletter_02` | Newsletter |
| `other_05_newsletter.eml` | `email_series_follow_newsletter_02` | Newsletter |
| `other_06_network_digest.eml` | `email_notification_digest_01` | Network activity roll-up (`NETWORKDIGEST`) |
| `other_07_pymk.eml` | `email_pymk_02` | People You May Know |
| `other_08_search_appearances.eml` | `email_notification_single_search_appearance_01` | "You appeared in 21 searches" |
| `other_09_analytics_recap.eml` | `email_weekly_analytics_recap_v2` | Weekly post analytics |
| `other_10_groups.eml` | `email_groups_recommended_by_admin_01` | Group activity |
| `other_11_next_best_action.eml` | `email_next_best_action_digest_01` | Prompt digest |

Eleven `other` fixtures is deliberate. Each is a distinct template, and their
job is to prove the classifier doesn't promote noise into an actionable card.
A false positive here is the worst case: a generic AI reply appearing
under David's name where no reply was warranted.

## Classification signal

LinkedIn stamps `X-LinkedIn-Class` and `X-LinkedIn-Template` on notification
mail. That is a structured, stable signal, and far sturdier than matching
subject lines (which vary by locale, A/B cohort, and sender name). The parser
keys off the template header first and falls back to subject/body heuristics
only for the two Premium marketing emails, which carry no `X-LinkedIn-*`
headers at all — and those are `other`, so a fallback that shrugs and says
"other" is already correct.

## Open questions these fixtures raised

- **`mention` is a broader bucket than its name suggests.** The only
  engagement-with-your-content sample available is "commented on your post",
  not a literal @mention. Both lead David to the same action — go reply — so
  they share a bucket. If a true @mention fixture ever lands and its shape
  differs, this may need splitting. Flagged in the parser as `# FRAGILE`.
- **Digests fan out.** `connection_accepted_02_digest.eml` is one email
  containing three people. `message_received_01.eml` is a digest that happens
  to hold a single message ("You have 1 new message") — a two-message digest
  would repeat the sender block. Any parser that assumes one email means one
  item is wrong.
- **`other_06_network_digest.eml` and `other_07_pymk.eml` may answer an open
  open decision** — how the `network/` module sources comment and
  connection suggestions without scraping. Both are LinkedIn-generated feeds of
  people and posts, arriving by email. Not acted on; noted for Joshua.

### Still missing

- A true @mention (`"mentioned you in a post"`). It may not exist as a
  standalone email at all — mentions plausibly roll up into
  `email_notification_digest_01` rather than getting their own message.
- A messages digest holding 2+ messages, which would confirm the repeat-block
  shape rather than leaving it inferred.

## Where the originals are

Unscrubbed originals stay in `docs/`, which is gitignored. If a scrub turns out
to have been too aggressive, re-derive from there rather than from these.

## What was scrubbed

Applied to every unfolded header value and every decoded text part:

- real addresses → `david@example.com` / `joshua@example.com`
- `midToken`, `midSig`, `otpToken`, `loid` → value replaced with `REDACTED`,
  parameter name kept so the URL shape stays realistic
- `sharedKey/<value>/` → `sharedKey/REDACTED/`
- VERP bounce addresses (`s-<token>@bounce.linkedin.com`) → `s-REDACTED@…`

`midToken`/`midSig` are per-recipient signed request tokens; `otpToken` signs
the bearer straight into the account; `loid` identifies the member to the
unsubscribe endpoint; `sharedKey` lets the bearer accept that specific
invitation; the VERP address identifies the recipient mailbox. None of them
belong in a git repo.

Header values are unfolded before scrubbing. RFC 5322 folding splits long
headers across `"\r\n "`, which will hide a token from a regex run over the
serialized message — `&loid=AQG8…` becomes `&lo\r\n id=AQG8…`. Scrub headers
while they are still joined.

Deliberately **not** scrubbed, because the parser extracts them and the tests
need real values: sender names, public profile URLs, LinkedIn URL paths, post
activity URNs, and the `lipi`/`trk` parameters (which carry the template name
and are useful when diagnosing a format change).

No LinkedIn credential, cookie, or session token belongs in this directory —
or anywhere in this repo. The system never logs into LinkedIn.

## Adding more

Export from Gmail on desktop: open the message → **⋮** → **Show original** →
**Download Original**. Drop the file in `docs/`, then re-run the scrub before
committing anything into this directory.
