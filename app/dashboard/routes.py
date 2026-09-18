"""David's dashboard: the page itself and the JSON behind it.

What: serves the single-file dashboard and the small API it calls.

Why: this is the only part of the system David ever sees. Two rules govern
every handler below:

  1. **No technical detail reaches the screen.** Every failure returns the same
     plain sentence. A stack trace, a status code, or the word "draft" leaking
     into the UI is a bug, not a detail.
  2. **Nothing here sends anything to LinkedIn.** The buttons copy text to his
     clipboard and open links. The one exception is approving a post, which
     hands it to the publisher — the sanctioned path, after David has said yes.

How: FastAPI router mounted by main.py. Auth is a signed cookie checked by a
dependency; there is one user, so there's nothing else to look up.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app.dashboard.auth import COOKIE_NAME, LoginGate
from app.dashboard.viewmodels import is_item, item_to_json, post_to_json
from app.db import DraftKind, DraftStatus, InvalidTransitionError
from app.db.repository import DraftNotFoundError
from app.logging_setup import get_logger

logger = get_logger(__name__)

router = APIRouter()

# The one thing David ever sees when something breaks.
FRIENDLY_ERROR = "Something didn't work — text support."

STATIC_DIR = Path(__file__).parent / "static"

# Statuses that mean an item still wants David's attention.
_OPEN_ITEM_STATUSES = (DraftStatus.DRAFT, DraftStatus.PENDING_APPROVAL, DraftStatus.APPROVED)
_ALL_ITEM_STATUSES = _OPEN_ITEM_STATUSES + (DraftStatus.DONE, DraftStatus.REJECTED)


class PinPayload(BaseModel):
    pin: str = Field(min_length=1, max_length=12)


class PostTextPayload(BaseModel):
    body: str = Field(min_length=1, max_length=8000)


class ChatPayload(BaseModel):
    # Bounded so a paste of an entire document can't run up a bill or blow the
    # context window. David types questions, not essays.
    message: str = Field(min_length=1, max_length=4000)


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------


def _state(request: Request):
    return request.app.state


def require_login(request: Request) -> None:
    """Reject anything without a valid session cookie."""
    gate: LoginGate = request.app.state.login_gate
    if not gate.verify(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not signed in")


def _friendly_failure() -> JSONResponse:
    return JSONResponse(status_code=500, content={"ok": False, "message": FRIENDLY_ERROR})


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard_page(request: Request) -> HTMLResponse:
    """Serve the dashboard. Login is handled inside the page itself."""
    html = _state(request).dashboard_html
    if html is None:
        # The page failed to load at boot. Say the human thing, not the reason.
        return HTMLResponse(
            f"<!doctype html><meta charset='utf-8'>"
            f"<body style=\"font-family:-apple-system,sans-serif;font-size:20px;"
            f"padding:2rem;line-height:1.6\">{FRIENDLY_ERROR}</body>",
            status_code=500,
        )
    return HTMLResponse(html)


# David's whole footprint is one bookmarked URL. On iOS, "Add to Home Screen"
# without an apple-touch-icon shows a blurry screenshot of the page instead of
# an icon — these routes are what make it look like something he taps daily.
# iOS does not reliably accept a data: URI for the touch icon, so both are
# served as real files.
@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon")


@router.get("/apple-touch-icon.png", include_in_schema=False)
@router.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
def apple_touch_icon() -> FileResponse:
    return FileResponse(STATIC_DIR / "apple-touch-icon.png", media_type="image/png")


def load_dashboard_html(path: Path) -> str | None:
    """Read the dashboard file and switch it from mock data to the live API.

    The same file runs standalone from disk for design work (mock data) and
    served from here against the real database — the only difference is this
    one flag, flipped at boot so the file on disk stays openable.
    """
    try:
        html = path.read_text(encoding="utf-8")
    except OSError:
        logger.exception(
            "Couldn't read the dashboard page at %s. David will see the "
            "'text support' message until this is fixed.",
            path,
        )
        return None

    marker = "const USE_MOCK = true;"
    if marker not in html:
        logger.error(
            "The dashboard page has no '%s' line, so it may still be showing "
            "made-up demo data to David. Check %s.",
            marker,
            path,
        )
        return html

    return html.replace(marker, "const USE_MOCK = false;", 1)


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


@router.get("/api/session")
def get_session(request: Request) -> dict:
    gate: LoginGate = _state(request).login_gate
    return {"authenticated": gate.verify(request.cookies.get(COOKIE_NAME))}


@router.post("/api/login")
def login(payload: PinPayload, request: Request, response: Response) -> dict:
    gate: LoginGate = _state(request).login_gate

    if gate.locked_out:
        # Deliberately the same shape as a wrong PIN. Telling an attacker they
        # hit a lockout tells them the lockout exists.
        raise HTTPException(status_code=401, detail="wrong pin")

    if not gate.check_pin(payload.pin):
        raise HTTPException(status_code=401, detail="wrong pin")

    # Mark the cookie Secure only when the connection really is HTTPS.
    #
    # Railway terminates TLS at its edge and forwards to us over plain HTTP, so
    # request.url.scheme reads "http" even in production — X-Forwarded-Proto is
    # the honest signal. Deriving it per-request means production gets a Secure
    # cookie automatically and local http:// still works, with no environment
    # flag anyone has to remember to flip.
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    over_https = forwarded_proto == "https" or request.url.scheme == "https"

    response.set_cookie(
        COOKIE_NAME,
        gate.issue(),
        max_age=gate.max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=over_https,
    )
    return {"ok": True}


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


@router.get("/api/today", dependencies=[Depends(require_login)])
def get_today(request: Request) -> dict:
    """Everything still waiting on David."""
    try:
        drafts = _state(request).repository.list_by_status(_OPEN_ITEM_STATUSES, limit=100)
        items = [item_to_json(d) for d in drafts if is_item(d)]
    except Exception:
        logger.exception("Couldn't build David's Today screen.")
        return _friendly_failure()
    return {"items": items}


@router.get("/api/messages", dependencies=[Depends(require_login)])
def get_messages(request: Request) -> dict:
    """The same cards, including ones he's already handled."""
    try:
        drafts = _state(request).repository.list_by_status(_ALL_ITEM_STATUSES, limit=200)
        items = [item_to_json(d) for d in drafts if is_item(d)]
    except Exception:
        logger.exception("Couldn't build David's Messages screen.")
        return _friendly_failure()
    return {"items": items}


@router.get("/api/posts", dependencies=[Depends(require_login)])
def get_posts(request: Request) -> dict:
    """Posts grouped the way the screen shows them."""
    state = _state(request)
    tz = state.config.client.tz
    try:
        drafts = [
            d
            for d in state.repository.list_by_status(_ALL_ITEM_STATUSES, limit=200)
            if d.kind is DraftKind.POST
        ]
        now = datetime.now(timezone.utc)
        return {
            "awaiting": [
                post_to_json(d, tz, now=now)
                for d in drafts
                if d.status is DraftStatus.PENDING_APPROVAL
            ],
            "scheduled": [
                post_to_json(d, tz, now=now) for d in drafts if d.status is DraftStatus.APPROVED
            ],
            "past": [
                post_to_json(d, tz, now=now)
                for d in drafts
                if d.status in (DraftStatus.DONE, DraftStatus.REJECTED)
            ],
        }
    except Exception:
        logger.exception("Couldn't build David's My Posts screen.")
        return _friendly_failure()


# --------------------------------------------------------------------------
# Acting
# --------------------------------------------------------------------------


@router.post("/api/items/{draft_id}/done", dependencies=[Depends(require_login)])
def mark_item_done(draft_id: int, request: Request) -> dict:
    """David copied the reply and sent it himself, or dealt with it another way."""
    repository = _state(request).repository
    try:
        draft = repository.get(draft_id)
        if draft.kind is DraftKind.POST:
            # Posts have their own approve/skip flow; this endpoint would let
            # one reach `done` without ever being approved.
            raise HTTPException(status_code=400, detail="not an item")
        if draft.status not in (DraftStatus.DONE, DraftStatus.REJECTED):
            repository.transition(draft_id, DraftStatus.DONE, note="David handled it himself")
    except HTTPException:
        raise
    except DraftNotFoundError:
        raise HTTPException(status_code=404, detail="not found") from None
    except Exception:
        logger.exception("Couldn't mark item #%s as handled.", draft_id)
        return _friendly_failure()
    return {"ok": True}


@router.post("/api/posts/{draft_id}/approve", dependencies=[Depends(require_login)])
def approve_post(draft_id: int, request: Request) -> dict:
    """David tapped "Yes, post it" — approve and hand it to the publisher."""
    state = _state(request)
    try:
        draft = state.repository.get(draft_id)
        if draft.kind is not DraftKind.POST:
            raise HTTPException(status_code=400, detail="not a post")

        if draft.is_stuck_publishing:
            # A previous attempt reached the publisher and we never learned the
            # outcome, so the post kept its claim and no job will touch it.
            # Tapping the button again is David saying "it didn't go out, send
            # it" — the one decision that can free it, and one only a person can
            # make, since the alternative risk is a duplicate under his name.
            logger.warning(
                "Post #%s was stuck after an unclear publishing attempt. David asked "
                "for it to be sent again.",
                draft_id,
            )
            state.repository.release_publish_claim(draft_id)
            state.services.posting.publish(draft_id)
            return {"ok": True}

        state.services.posting.approve_and_publish(draft_id)
    except HTTPException:
        raise
    except DraftNotFoundError:
        raise HTTPException(status_code=404, detail="not found") from None
    except InvalidTransitionError:
        # Already approved or already skipped. Tapping twice is not an error.
        logger.info("Post #%s was already dealt with.", draft_id)
    except Exception:
        logger.exception("Couldn't approve post #%s.", draft_id)
        return _friendly_failure()
    return {"ok": True}


@router.post("/api/posts/{draft_id}/skip", dependencies=[Depends(require_login)])
def skip_post(draft_id: int, request: Request) -> dict:
    """David tapped "Skip this one"."""
    repository = _state(request).repository
    try:
        draft = repository.get(draft_id)
        if draft.kind is not DraftKind.POST:
            raise HTTPException(status_code=400, detail="not a post")
        if draft.status not in (DraftStatus.DONE, DraftStatus.REJECTED):
            repository.transition(
                draft_id, DraftStatus.REJECTED, note="David skipped it on the dashboard"
            )
    except HTTPException:
        raise
    except DraftNotFoundError:
        raise HTTPException(status_code=404, detail="not found") from None
    except Exception:
        logger.exception("Couldn't skip post #%s.", draft_id)
        return _friendly_failure()
    return {"ok": True}


@router.get("/api/chat", dependencies=[Depends(require_login)])
def get_chat(request: Request) -> dict:
    """The conversation so far, oldest first."""
    try:
        history = _state(request).services.chat.history()
    except Exception:
        logger.exception("Couldn't load David's chat history.")
        return _friendly_failure()
    return {
        "messages": [
            {"id": m.id, "role": m.role, "content": m.content} for m in history
        ]
    }


@router.post("/api/chat", dependencies=[Depends(require_login)])
def post_chat(payload: ChatPayload, request: Request) -> dict:
    """Ask a question and get an answer.

    Never returns an error to the screen: if the model is unreachable, the
    service stores the plain-language fallback as the reply, so David sees a
    message in the conversation rather than a spinner that never resolves.
    """
    try:
        answer = _state(request).services.chat.ask(payload.message)
    except ValueError:
        raise HTTPException(status_code=400, detail="empty message") from None
    except Exception:
        logger.exception("Couldn't answer David's question.")
        return _friendly_failure()
    return {"reply": {"id": answer.id, "role": answer.role, "content": answer.content}}


@router.delete("/api/chat", dependencies=[Depends(require_login)])
def clear_chat(request: Request) -> dict:
    """Start the conversation over."""
    try:
        _state(request).services.chat.clear()
    except Exception:
        logger.exception("Couldn't clear David's chat.")
        return _friendly_failure()
    return {"ok": True}


@router.put("/api/posts/{draft_id}/text", dependencies=[Depends(require_login)])
def update_post_text(draft_id: int, payload: PostTextPayload, request: Request) -> dict:
    """David edited the wording before approving it."""
    repository = _state(request).repository
    try:
        draft = repository.get(draft_id)
        if draft.kind is not DraftKind.POST:
            raise HTTPException(status_code=400, detail="not a post")
        if draft.status not in (DraftStatus.DRAFT, DraftStatus.PENDING_APPROVAL):
            # Editing something already queued would publish words he never
            # approved — the exact thing the approval step exists to prevent.
            raise HTTPException(status_code=409, detail="already decided")
        repository.update_body(draft_id, payload.body.strip())
        logger.info("David reworded post #%s.", draft_id)
    except HTTPException:
        raise
    except DraftNotFoundError:
        raise HTTPException(status_code=404, detail="not found") from None
    except Exception:
        logger.exception("Couldn't save David's edit to post #%s.", draft_id)
        return _friendly_failure()
    return {"ok": True}
