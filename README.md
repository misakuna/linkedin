# David's LinkedIn Assistant

A human-in-the-loop LinkedIn assistant built for a coaching client.
The system does the thinking — drafting, scheduling, monitoring. David does the
clicking on LinkedIn itself.

**Read the compliance rules below before changing anything.** They are the
reason this project is shaped the way it is, and they are not negotiable:

1. **Nothing in this codebase ever contacts linkedin.com.** No HTTP client, no
   browser automation, no scraping. LinkedIn's ToS forbids it and a violation
   risks a permanent ban of the client's account.
2. **No LinkedIn credential exists anywhere in it** — not in `.env`, not in
   config, not in a token store. The system never logs in as the client.
3. **Nothing reaches LinkedIn without the client approving the exact text.**
   Publishing goes through an official partner API, gated on an explicit
   approval; the database enforces this rather than trusting callers.
4. **Read-only on the mailbox.** Gmail access is a read-only scope used to find
   LinkedIn's notification emails. The system never sends mail as the client.
5. **Nothing installs on the client's machine.** His whole footprint is one
   bookmarked URL plus a one-time OAuth click.

Ask before working around any of these. If a task seems to require it, the task
is wrong.

## Running it locally

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env        # fill in later — it boots fine empty
uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000. Tests:

```bash
pytest
```

Out of the box every integration in `config.yaml` is set to `mock`, so a local
run makes no network calls at all.

## Layout

```
app/
  config.py        config.yaml loader + validation
  env_secrets.py   API keys from .env (never LinkedIn credentials)
  logging_setup.py plain-language stdout logging
  scheduler.py     APScheduler
  main.py          FastAPI entrypoint
  db/              SQLite: drafts + status history
  posting/         draft posts, random times, Buffer          (STEP 3)
  inbox/           Gmail read-only, parse, draft replies       (STEP 2)
  approvals/       SMS approval loop                           (STEP 3)
  dashboard/       David's three screens                       (STEP 4)
  network/         daily workout — blocked on an open decision
tests/
  fixtures/        real LinkedIn notification emails (.eml)
```

## The draft lifecycle

Everything the assistant writes is a row in `drafts` with a status:

```
draft ──▶ pending_approval ──▶ approved ──▶ done
  │              │                 │
  └──────────────┴─────────────────┴──▶ rejected
```

There is deliberately no path from `draft` straight to `done`. Nothing reaches
LinkedIn without passing through David, and the database enforces that rather
than trusting each caller to remember.

## Deploying

Railway or Render, as a persistent Docker service (not serverless — the
scheduler and SQLite both need to stay alive between requests).

1. Push this directory to its own GitHub repo.
2. Create a Railway/Render service from the repo; it builds the `Dockerfile`.
3. Mount a persistent volume at `/data` and set `DATABASE_PATH=/data/david.db`.
   Without the volume, every deploy wipes David's drafts.
4. Set the environment variables from `.env.example` in the host's dashboard.
5. Health check: `GET /healthz`.

Rollback is a redeploy of the previous commit.
