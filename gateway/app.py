#!/usr/bin/env python3
"""
Multi-agent gateway for local agents (private network only).

Currently registered: agent=tutor
Future: research, roast, inquiry, hermes — same /v1/turn contract.

Run from the project root (venv activated):
  python -m uvicorn gateway.app:app --host 0.0.0.0 --port <PORT>

Prefer binding only on a private/tailnet interface.
Do not expose this service on the public internet.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from collections import defaultdict
from typing import Any, Generator, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import auth  # noqa: E402
import core  # noqa: E402
import tutor as T  # noqa: E402
# Multi-user: never fall back to DEFAULT_USER_ID="george" on missing user_id
T.STRICT_USERS = True
try:
    import debug_events  # noqa: E402
except Exception as _e:
    debug_events = None  # type: ignore
    print('[gateway] debug_events import failed:', _e)

WEB_DIR = ROOT / "web"
CONV_FILE = ROOT / "web_conversation.json"
DEFAULT_SESSION = "default"
MAX_CONV_MESSAGES = 200
LOBBY = "_lobby"

# Login brute-force protection (in-memory; resets on process restart)
_LOGIN_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
_LOGIN_WINDOW_S = 300  # 5 minutes
_LOGIN_MAX = int(__import__("os").environ.get("TUTOR_LOGIN_MAX_ATTEMPTS", "8"))


def _client_ip(request: Request) -> str:
    # Behind a reverse proxy, trust X-Forwarded-For only if TUTOR_TRUST_PROXY=1
    if __import__("os").environ.get("TUTOR_TRUST_PROXY", "").strip() in ("1", "true", "yes"):
        xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if xff:
            return xff
    return (request.client.host if request.client else "") or "unknown"


def _login_allowed(ip: str) -> bool:
    now = __import__("time").time()
    window = _LOGIN_ATTEMPTS[ip]
    _LOGIN_ATTEMPTS[ip] = [t for t in window if now - t < _LOGIN_WINDOW_S]
    return len(_LOGIN_ATTEMPTS[ip]) < _LOGIN_MAX


def _login_fail(ip: str) -> None:
    _LOGIN_ATTEMPTS[ip].append(__import__("time").time())


def _cookie_secure(request: Request) -> bool:
    """Set Secure only when the browser is on HTTPS.

    Important: do NOT force Secure just because TUTOR_PUBLIC=1 — accessing
    http://<tailnet-ip>:8790 would then refuse to store the cookie and login
    appears to do nothing (password OK, then overlay returns).
    Use TUTOR_COOKIE_SECURE=1 only when you always serve via HTTPS (Funnel).
    """
    import os
    # Explicit override (admin knows they only use HTTPS)
    force = os.environ.get("TUTOR_COOKIE_SECURE", "").strip().lower()
    if force in ("1", "true", "yes"):
        # Still prefer actual HTTPS; if someone forces on HTTP, warn once
        if request.url.scheme != "https" and (request.headers.get("x-forwarded-proto") or "").lower() != "https":
            print("[gateway] WARNING: TUTOR_COOKIE_SECURE=1 but request is HTTP — cookie may be dropped by browser")
        return True
    if force in ("0", "false", "no"):
        return False
    if request.url.scheme == "https":
        return True
    if (request.headers.get("x-forwarded-proto") or "").lower() == "https":
        return True
    return False


def _last_unanswered_tutor_q(conv: list) -> str | None:
    """If the thread ends with an assistant question (no user answer after), return it."""
    for m in reversed(conv or []):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = str(m.get("content") or "").strip()
        if role == "user" and content:
            return None  # learner already spoke after last tutor turn
        if role == "assistant" and content:
            if "?" in content:
                return content
            return None
        if role == "system":
            continue
    return None


app = FastAPI(
    title="Local Agent Gateway",
    version="0.5.0",
    description="Multi-user tutor gateway (private network or TLS public)",
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    # Never let a proxy/CDN serve another user's bootstrap/subjects/state.
    # Different devices do not share cookies; cached /api/* would look like
    # "isolation is broken" when two people use the same public URL.
    path = request.url.path or ""
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "private, no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Vary"] = "Cookie"
    # Only send HSTS when explicitly public+HTTPS
    import os
    if os.environ.get("TUTOR_PUBLIC", "").strip().lower() in ("1", "true", "yes"):
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=15552000; includeSubDomains"
        )
    return response



def _current_user(request: Request) -> str | None:
    token = request.cookies.get(auth.COOKIE_NAME)
    return auth.parse_session_token(token)


def _log_turn(user_id, message, thread_from, thread_to, result):
    if debug_events is None:
        return
    try:
        debug_events.log_turn(
            user_id,
            message=message or "",
            thread_from=thread_from,
            thread_to=thread_to,
            result=result or {},
        )
    except Exception as e:
        print("[gateway] event log failed:", type(e).__name__, e)


def _require_user(request: Request) -> str:
    uid = _current_user(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Not logged in")
    return uid


@app.on_event("startup")
def _bootstrap_users():
    """Ensure registry + optional legacy migration for first user."""
    try:
        auth.ensure_default_user("default")
        notes = auth.migrate_legacy_files("default")
        if notes:
            print("[auth] legacy migrate:", "; ".join(notes))
    except Exception as e:
        print("[auth] bootstrap warning:", e)


class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def auth_login(body: LoginBody, request: Request, response: Response):
    ip = _client_ip(request)
    if not _login_allowed(ip):
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Try again in a few minutes.",
        )
    uid = auth.verify_user(body.username, body.password)
    if not uid:
        _login_fail(ip)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = auth.make_session_token(uid)
    response.set_cookie(
        key=auth.COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        max_age=auth.SESSION_DAYS * 86400,
        path="/",
    )
    data = auth.load_users()
    display = (data.get("users") or {}).get(uid, {}).get("display_name") or uid
    try:
        if debug_events is not None:
            debug_events.log_event(uid, "login", ip=_client_ip(request))
    except Exception:
        pass
    return {"ok": True, "user_id": uid, "display_name": display}


@app.post("/api/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(request: Request):
    uid = _current_user(request)
    if not uid:
        return {"ok": False, "user_id": None}
    data = auth.load_users()
    display = (data.get("users") or {}).get(uid, {}).get("display_name") or uid
    return {"ok": True, "user_id": uid, "display_name": display}


@app.get("/api/debug/whoami")
def debug_whoami(request: Request):
    """Truth source for isolation bugs: cookie user vs disk paths vs subject keys.

    Open this URL on phone AND laptop while logged in — the JSON must match the
    badge user, not the other device's account.
    """
    uid = _current_user(request)
    if not uid:
        return {"ok": False, "error": "not_logged_in"}
    try:
        state_path = str(auth.state_path(uid))
    except Exception:
        state_path = "?"
    try:
        conv_path = str(auth.conv_path(uid))
    except Exception:
        conv_path = "?"
    state = T.load_state(user_id=uid)
    lid = str((state.get("learner") or {}).get("id") or "")
    subjects = sorted((state.get("subjects") or {}).keys())
    store = _load_store(uid)
    threads = sorted((store.get("threads") or {}).keys())
    return {
        "ok": True,
        "cookie_user": uid,
        "learner_id_in_state": lid,
        "state_path": state_path,
        "conv_path": conv_path,
        "subject_ids": subjects,
        "thread_ids": threads,
        "match": (lid.lower() == uid.lower()) if lid else False,
    }



@app.exception_handler(Exception)
async def _unhandled(request, exc):
    return JSONResponse(
        status_code=500,
        content={"ok": False, "detail": str(exc), "type": type(exc).__name__},
    )


_store: dict[str, Any] = {}


def _empty_store() -> dict:
    return {"version": 2, "active_thread": LOBBY, "threads": {LOBBY: []}}


def _normalize_store(store: Any) -> dict:
    """Ensure store is always {version, active_thread, threads: {id: [msgs]}}."""
    if not isinstance(store, dict):
        # Legacy: raw message list
        if isinstance(store, list):
            return {"version": 2, "active_thread": LOBBY, "threads": {LOBBY: store}}
        return _empty_store()
    threads = store.get("threads")
    if isinstance(threads, list):
        # Mis-shaped file: threads was a flat list
        store = {
            "version": 2,
            "active_thread": store.get("active_thread") or LOBBY,
            "threads": {LOBBY: threads},
        }
    elif not isinstance(threads, dict):
        store = dict(store)
        store["threads"] = {LOBBY: []}
    else:
        # Copy & coerce message lists
        clean = {}
        for k, v in threads.items():
            clean[str(k)] = list(v) if isinstance(v, list) else []
        store = dict(store)
        store["threads"] = clean
    if LOBBY not in store["threads"]:
        store["threads"][LOBBY] = []
    store.setdefault("version", 2)
    store.setdefault("active_thread", LOBBY)
    return store


def _load_store(session_id: str) -> dict:
    """Always re-read conversation from disk for this user_id.

    In-memory cache alone caused stale/wrong threads after manual repairs and
    made multi-device debugging impossible (process kept old blobs).
    """
    # Per-user conversation store (session_id == authenticated user_id)
    try:
        path = auth.conv_path(session_id)
    except Exception:
        path = CONV_FILE
    store = _empty_store()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Refuse files that claim a different session_id
            if isinstance(data, dict):
                claimed = str(data.get("session_id") or "").strip().lower()
                if claimed and claimed != str(session_id).strip().lower():
                    print(
                        f"[gateway] CONV MISMATCH: file session_id={claimed!r} "
                        f"but load for {session_id!r} ({path}) — using empty store"
                    )
                    store = _empty_store()
                    _store[session_id] = store
                    return store
            if isinstance(data, list):
                store["threads"][LOBBY] = data
            elif isinstance(data, dict):
                if isinstance(data.get("messages"), list) and "threads" not in data:
                    store["threads"][LOBBY] = data["messages"]
                elif "threads" in data:
                    # dict or mis-shaped list — normalize handles both
                    store["version"] = 2
                    store["active_thread"] = data.get("active_thread") or LOBBY
                    store["threads"] = data.get("threads")
                    store = _normalize_store(store)
                else:
                    store = _normalize_store(data) if data else store
        except Exception:
            pass
    store = _normalize_store(store)
    _store[session_id] = store
    return store


def _save_store(session_id: str, store: dict) -> None:
    store = _normalize_store(store)
    for tid, msgs in list(store["threads"].items()):
        if not isinstance(msgs, list):
            store["threads"][tid] = []
            continue
        if len(msgs) > MAX_CONV_MESSAGES:
            store["threads"][tid] = msgs[-MAX_CONV_MESSAGES:]
    _store[session_id] = store
    payload = {
        "version": 2,
        "session_id": session_id,
        "active_thread": store.get("active_thread") or LOBBY,
        "threads": store["threads"],
    }
    try:
        try:
            path = auth.conv_path(session_id)
        except Exception:
            path = CONV_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        T.atomic_save_json(path, payload)
    except Exception:
        try:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass



def _prefer_teaching_thread(state: dict, tid: str) -> str:
    """Goal collection + diagnostic intake belong on the subject chat, not lobby."""
    active = state.get("active_session") or {}
    sid = active.get("subject_id")
    if not sid or not active.get("active"):
        return tid or LOBBY
    sid = str(sid)
    if (tid or LOBBY) != LOBBY:
        return tid
    if (
        active.get("pending_goal")
        or active.get("diagnostic_active")
        or (
            hasattr(T, "subject_needs_diagnostic")
            and T.subject_needs_diagnostic(state, sid)
        )
    ):
        return sid
    return tid or LOBBY


def _thread_id_from_state(state: dict) -> str:
    active = state.get("active_session") or {}
    sid = active.get("subject_id")
    if sid and active.get("active"):
        return str(sid)
    if sid:
        return str(sid)
    return LOBBY



def _align_state_to_thread(state: dict, tid: str, user_id: str | None = None) -> bool:
    """If the UI is on a subject chat, make learner state match that subject.

    Sidebar only loads history; it does not switch active_session. Without this,
    a message typed in "AI-agents" while state is still "gras" is taught as gras
    and then moved into the gras thread — cross-chat contamination.
    Returns True if a silent align happened.
    """
    if not tid or tid == LOBBY:
        return False
    subjects = state.get("subjects") or {}
    if tid not in subjects:
        return False
    active = state.get("active_session") or {}
    if active.get("active") and active.get("subject_id") == tid:
        return False
    # Resume this subject for teaching; not a cold resume (no forced retrieval banner)
    try:
        if active.get("active") and active.get("subject_id") and active.get("subject_id") != tid:
            # Close previous session bookmark without wiping UI threads
            try:
                T.close_session(
                    state, active.get("resume_instruction"), user_id=user_id
                )
            except Exception:
                state["active_session"]["active"] = False
        # Preserve intake flags if this subject is still mid goal/diagnostic
        prev = state.get("active_session") or {}
        was_pending = bool(prev.get("pending_goal")) and prev.get("subject_id") == tid
        T.start_session(state, tid, is_resume=True, user_id=user_id)
        st = state.get("active_session") or {}
        st["just_resumed"] = False  # continuing open chat, not "waar was ik"
        if was_pending:
            st["pending_goal"] = True
        # Resume diagnostic intake if not finished
        if hasattr(T, "subject_needs_diagnostic") and T.subject_needs_diagnostic(state, tid):
            d = T.get_subject_diagnostic(state, tid)
            if d.get("status") == "in_progress" or (
                d.get("status") == "needed" and not st.get("pending_goal")
            ):
                T.begin_subject_diagnostic(state, tid)
        return True
    except Exception:
        return False


def _get_thread(store: dict, thread_id: str) -> list:
    if not isinstance(store.get("threads"), dict):
        store["threads"] = {LOBBY: list(store.get("threads") or []) if isinstance(store.get("threads"), list) else []}
    threads = store["threads"]
    tid = thread_id or LOBBY
    if tid not in threads or not isinstance(threads[tid], list):
        threads[tid] = []
    return threads[tid]


def _history_for_ui(conv: list) -> list[dict]:
    out = []
    for m in conv[-MAX_CONV_MESSAGES:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            out.append({"role": "you", "text": content})
        elif role == "assistant":
            out.append({"role": "tutor", "text": content})
        elif role == "system":
            if "[session boundary" in content:
                out.append({"role": "system", "text": "— onderwerp gewisseld —"})
            elif content.startswith("Hervat:") or "Leerdoel:" in content:
                # Resume banner persisted on the thread
                out.append({"role": "system", "text": content.replace(" | ", "\n")})
    return out



def _apply_topic_switch(store: dict, old_tid: str, new_tid: str, old_conv: list, result: dict, user_text: str) -> str:
    """After a subject switch: keep threads separate.

    - Old thread: only session boundary (already on old_conv); strip any Hervat/resume
      that may have been written there.
    - New thread: user intent + resume banner + resume question — never copy grass
      teaching turns into zon (or vice versa).
    """
    # Clean old thread: remove Hervat / resume assistant that belong to the new topic
    while old_conv:
        last = old_conv[-1]
        if not isinstance(last, dict):
            break
        role = last.get("role")
        content = str(last.get("content") or "")
        if role == "system" and content.startswith("Hervat:"):
            old_conv.pop()
            continue
        if role == "assistant":
            rq = str(result.get("response") or "").strip()
            if rq and content.strip() == rq:
                old_conv.pop()
                continue
            # Also drop assistant msgs right after a Hervat we already removed
            if len(old_conv) >= 1:
                # stop if previous is normal teaching
                break
        if role == "system" and "[session boundary" in content:
            break
        break

    new_conv = _get_thread(store, new_tid)

    # Navigation utterances stay out of subject chats, but ARE kept in the lobby
    # so you can see when you switched and to what (useful for voice + overview).
    ut = (user_text or "").strip()
    if ut:
        while old_conv:
            last = old_conv[-1]
            if (
                isinstance(last, dict)
                and last.get("role") == "user"
                and str(last.get("content") or "").strip() == ut
            ):
                old_conv.pop()
                continue
            break
        lobby = _get_thread(store, LOBBY)
        title = (result.get("resume") or {}).get("title") or new_tid
        # User line + system note (dedupe last user line)
        if not (
            lobby
            and isinstance(lobby[-1], dict)
            and lobby[-1].get("role") == "user"
            and str(lobby[-1].get("content") or "").strip() == ut
        ):
            lobby.append({"role": "user", "content": ut})
        note = f"— wissel naar {title} —"
        if not lobby or str((lobby[-1] or {}).get("content") or "") != note:
            lobby.append({"role": "system", "content": note})

    if result.get("kind") == "new_subject":
        # Brand-new topic: land on subject chat with goal prompt (no Hervat banner)
        sub_title = new_tid
        for s in (result.get("subjects") or []):
            if isinstance(s, dict) and str(s.get("id") or "") == str(new_tid):
                sub_title = s.get("title") or sub_title
                break
        head = f"Nieuw onderwerp: {sub_title}"
        if not any(
            isinstance(m, dict)
            and m.get("role") == "system"
            and str(m.get("content") or "").startswith("Nieuw onderwerp:")
            for m in new_conv[-4:]
        ):
            new_conv.append({"role": "system", "content": head})
        q = (result.get("response") or "").strip()
        if q and (
            not new_conv
            or str((new_conv[-1] or {}).get("content") or "").strip() != q
        ):
            new_conv.append({"role": "assistant", "content": q})
        # Strip goal/diagnostic replies that were incorrectly written on lobby
        store["active_thread"] = new_tid
        return new_tid

    if result.get("kind") == "resume":
        # If this subject thread already ends on an unanswered tutor question,
        # do NOT re-ask / re-banner — just land on that open question.
        open_q = _last_unanswered_tutor_q(new_conv)
        if open_q:
            store["active_thread"] = new_tid
            result["response"] = ""  # UI shows existing history only
            result["resume_reused"] = True
            result["resume"] = dict(result.get("resume") or {})
            result["resume"]["resume_question"] = open_q
            # Sync state last_q to the open one so policy continues that thread
            return new_tid
        banner = result.get("resume") or {}
        head_bits = [f"Hervat: {banner.get('title') or new_tid}"]
        if banner.get("goal"):
            head_bits.append(f"Leerdoel: {banner.get('goal')}")
        recent = banner.get("recent_concepts") or []
        if recent:
            head_bits.append(
                "Recent: "
                + ", ".join(
                    (x.get("title") if isinstance(x, dict) else str(x)) for x in recent
                )
            )
        head = " | ".join(head_bits)
        if not any(
            isinstance(m, dict)
            and m.get("role") == "system"
            and "Hervat:" in str(m.get("content") or "")
            for m in new_conv[-6:]
        ):
            new_conv.append({"role": "system", "content": head})
        q = (result.get("response") or "").strip()
        if q and (
            not new_conv
            or str((new_conv[-1] or {}).get("content") or "").strip() != q
        ):
            new_conv.append({"role": "assistant", "content": q})
    return new_tid


def _result_dict(result: dict, user_id: str | None = None) -> dict:
    """JSON-safe subset for SSE final event / TurnResponse."""
    out = {
        "ok": bool(result.get("ok", True)),
        "agent": result.get("agent") or "tutor",
        "kind": result.get("kind") or "message",
        "response": result.get("response") or "",
    }
    for key in (
        "subjects",
        "summary",
        "resume",
        "reviews",
        "stats",
        "policy",
        "routing",
        "subject_id",
        "raw_preview",
        "resume_reused",
        "switched_from_thread",
    ):
        if key in result and result[key] is not None:
            out[key] = result[key]
    uid = user_id or result.get("user_id")
    if uid:
        out["user_id"] = uid
    return out


class TurnRequest(BaseModel):
    message: str = Field(..., min_length=1)
    agent: str = "tutor"
    session_id: str = DEFAULT_SESSION
    channel: str = "web"
    thread_id: Optional[str] = None


class TurnResponse(BaseModel):
    ok: bool
    agent: str
    kind: str
    response: str = ""
    subjects: Optional[list[Any]] = None
    summary: Optional[dict[str, Any]] = None
    resume: Optional[dict[str, Any]] = None
    reviews: Optional[list[Any]] = None
    stats: Optional[dict[str, Any]] = None
    policy: Optional[dict[str, Any]] = None
    routing: Optional[dict[str, Any]] = None
    subject_id: Optional[str] = None
    raw_preview: Optional[str] = None
    thread_id: Optional[str] = None
    history: Optional[list[Any]] = None


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "agents": ["tutor"],
        "model": T.MODEL_NAME,
        "stream": True,
        "default_session": DEFAULT_SESSION,
    }


@app.get("/api/bootstrap")
def bootstrap(request: Request, agent: str = "tutor"):
    if agent != "tutor":
        raise HTTPException(400, f"Unknown agent: {agent}")
    uid = _current_user(request)
    if not uid:
        return {
            "ok": False,
            "auth_required": True,
            "agents": ["tutor"],
            "model": getattr(T, "MODEL_NAME", ""),
        }
    base = core.bootstrap(user_id=uid)
    store = _load_store(uid)
    state = T.load_state(user_id=uid)
    # Always prefer authenticated user's subjects (never a shared/default load)
    base["subjects"] = core.subjects_payload(state)
    base["summary"] = core.state_summary(state)
    # Start in lobby (meta) unless user was mid-subject chat
    tid = store.get("active_thread") or LOBBY
    if tid != LOBBY and tid not in (store.get("threads") or {}):
        tid = LOBBY
    conv = _get_thread(store, tid)
    base["ok"] = True
    base["session_id"] = uid
    base["user_id"] = uid
    base["thread_id"] = tid
    base["history"] = _history_for_ui(conv)
    base["lobby"] = True
    # Seed lobby with overview if empty
    if tid == LOBBY and not conv:
        overview = core.lobby_overview(state)
        conv.append({"role": "assistant", "content": overview})
        store["active_thread"] = LOBBY
        _save_store(uid, store)
        base["history"] = _history_for_ui(conv)
    base["threads"] = {
        k: len(v) for k, v in (store.get("threads") or {}).items() if k != LOBBY or v
    }
    return base


@app.get("/api/subjects")
def subjects(request: Request, agent: str = "tutor"):
    if agent != "tutor":
        raise HTTPException(400, f"Unknown agent: {agent}")
    uid = _require_user(request)
    state = T.load_state(user_id=uid)
    return {"ok": True, "subjects": core.subjects_payload(state), "user_id": uid}


@app.get("/api/state")
def state_summary(request: Request, agent: str = "tutor"):
    if agent != "tutor":
        raise HTTPException(400, f"Unknown agent: {agent}")
    uid = _require_user(request)
    state = T.load_state(user_id=uid)
    return {"ok": True, "summary": core.state_summary(state), "user_id": uid}


@app.get("/api/thread/{thread_id}")
def get_thread(request: Request, thread_id: str, agent: str = "tutor"):
    if agent != "tutor":
        raise HTTPException(400, f"Unknown agent: {agent}")
    uid = _require_user(request)
    store = _load_store(uid)
    tid = thread_id or LOBBY
    store["active_thread"] = tid
    # Align teaching state to the chat the user opened in the sidebar
    state = T.load_state(user_id=uid)
    if _align_state_to_thread(state, tid, user_id=uid):
        T.save_state(state, user_id=uid)
    conv = _get_thread(store, tid)
    _save_store(uid, store)
    return {
        "ok": True,
        "thread_id": tid,
        "history": _history_for_ui(conv),
        "user_id": uid,
    }


@app.post("/api/v1/turn", response_model=TurnResponse)
def turn(request: Request, req: TurnRequest):
    if req.agent != "tutor":
        raise HTTPException(400, f"Agent '{req.agent}' not registered. Available: tutor")
    sid = _require_user(request)
    lock = T.acquire_user_lock(sid)
    with lock:
      return _turn_locked(request, req, sid)

def _turn_locked(request: Request, req: TurnRequest, sid: str):
    store = _load_store(sid)
    state = T.load_state(user_id=sid)
    tid_from = req.thread_id or store.get("active_thread") or _thread_id_from_state(state)
    tid = _prefer_teaching_thread(state, tid_from)
    if _align_state_to_thread(state, tid, user_id=sid):
        T.save_state(state, user_id=sid)
    store["active_thread"] = tid
    conv = _get_thread(store, tid)
    result = core.handle_turn(
        req.message,
        state=state,
        conversation=conv,
        agent=req.agent,
        save=True,
        stream=False,
        user_id=sid,
    )
    state2 = T.load_state(user_id=sid)
    if result.get("kind") == "lobby":
        lobby_conv = _get_thread(store, LOBBY)
        resp = (result.get("response") or "").strip()
        if resp:
            lobby_conv.append({"role": "assistant", "content": resp})
        store["active_thread"] = LOBBY
        _save_store(sid, store)
        out = _result_dict(result, sid)
        out["thread_id"] = LOBBY
        out["history"] = _history_for_ui(lobby_conv)
        out["subjects"] = result.get("subjects") or core.subjects_payload(state2)
        return out
    new_tid = _thread_id_from_state(state2)
    # Prefer explicit subject_id on resume payload
    if result.get("kind") == "resume":
        resume = result.get("resume") or {}
        if resume.get("subject_id"):
            new_tid = str(resume["subject_id"])
        elif result.get("subject_id"):
            new_tid = str(result["subject_id"])
    routing = (result.get("routing") or {}) if isinstance(result, dict) else {}
    action = str(routing.get("action") or "").lower()
    if result.get("kind") == "new_subject" and result.get("subject_id"):
        new_tid = str(result["subject_id"])
    explicit_switch = (
        action in ("switch", "new_subject")
        or result.get("kind") in ("resume", "new_subject")
        or result.get("switched_from_thread")
    )
    if explicit_switch and new_tid and str(new_tid) != str(tid):
        tid = _apply_topic_switch(
            store, tid, new_tid, conv, result, req.message or ""
        )
    elif explicit_switch and new_tid:
        # Same subject (e.g. false-positive switch) — keep messages on this thread
        store["active_thread"] = str(new_tid)
        tid = str(new_tid)
        if result.get("resume_reused"):
            # Continue the open question already on the thread — no second ask
            state2 = T.load_state(user_id=sid)
            active = state2.get("active_session") or {}
            q = ((result.get("resume") or {}).get("resume_question") or "").strip()
            if q:
                active["last_tutor_question"] = q[:300]
                active["resume_instruction"] = q
                active["just_resumed"] = True
                T.save_state(state2, user_id=sid)
    else:
        store["active_thread"] = tid
        if tid and tid != LOBBY and tid in (state2.get("subjects") or {}):
            if (state2.get("active_session") or {}).get("subject_id") != tid:
                _align_state_to_thread(state2, tid, user_id=sid)
                T.save_state(state2, user_id=sid)
    store["active_thread"] = tid
    _save_store(sid, store)
    _log_turn(sid, req.message or "", tid_from, tid, result)
    out = _result_dict(result, sid)
    out["thread_id"] = tid
    out["history"] = _history_for_ui(_get_thread(store, tid))
    return out


@app.post("/api/v1/turn/stream")
def turn_stream(request: Request, req: TurnRequest):
    """SSE: type=start | token | done. Tokens arrive live while the model generates."""
    if req.agent != "tutor":
        raise HTTPException(400, f"Agent '{req.agent}' not registered.")

    sid = _require_user(request)
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(400, "Empty message")

    def event_stream() -> Generator[str, None, None]:
        # Hold per-user lock for whole stream (load → LLM → save)
        lock = T.acquire_user_lock(sid)
        with lock:
            try:
                yield from _event_stream_inner()
            except Exception as e:
                yield _sse({
                    "type": "done",
                    "ok": False,
                    "kind": "error",
                    "response": f"Server error: {type(e).__name__}: {e}",
                    "agent": "tutor",
                })

    def _event_stream_inner() -> Generator[str, None, None]:
        import queue
        import threading

        store = _load_store(sid)
        state = T.load_state(user_id=sid)
        tid_from = req.thread_id or store.get("active_thread") or _thread_id_from_state(state)
        tid = _prefer_teaching_thread(state, tid_from)
        if _align_state_to_thread(state, tid, user_id=sid):
            T.save_state(state, user_id=sid)
        store["active_thread"] = tid
        conv = _get_thread(store, tid)

        # Commands: instant done, no tokens
        cmd = core._handle_command(state, conv, message, user_id=sid)
        if cmd is not None:
            T.save_state(state, user_id=sid)
            # Persist command output on this thread (lobby or subject)
            if message.strip().startswith("/"):
                conv.append({"role": "user", "content": message.strip()})
            resp = (cmd.get("response") or "").strip()
            if resp:
                conv.append({"role": "assistant", "content": resp})
            store["active_thread"] = tid
            _save_store(sid, store)
            _log_turn(sid, message, tid_from, tid, cmd)
            payload = _result_dict(cmd, sid)
            payload["type"] = "done"
            payload["thread_id"] = tid
            payload["history"] = _history_for_ui(conv)
            yield _sse({"type": "start", "thread_id": tid})
            yield _sse(payload)
            return

        q: queue.Queue = queue.Queue()
        result_box: dict = {}

        def _on_delta(partial, delta):
            # Token queue for SSE — no global monkey-patch of T.ask_model / T.STREAM
            q.put(("token", delta))

        def worker():
            try:
                result_box["result"] = core.handle_turn(
                    message,
                    state=state,
                    conversation=conv,
                    agent="tutor",
                    save=True,
                    stream=True,
                    on_response_delta=_on_delta,
                    user_id=sid,
                )
            except Exception as e:
                result_box["error"] = str(e)
            finally:
                q.put(("end", None))

        yield _sse({"type": "start", "thread_id": tid})
        threading.Thread(target=worker, daemon=True).start()

        while True:
            kind, payload = q.get()
            if kind == "token":
                yield _sse({"type": "token", "text": payload})
            elif kind == "end":
                break

        if result_box.get("error"):
            yield _sse({
                "type": "done",
                "ok": False,
                "kind": "error",
                "response": f"Model error: {result_box['error']}",
                "thread_id": tid,
            })
            return

        result = result_box.get("result") or {
            "ok": False,
            "kind": "error",
            "response": "No result",
            "agent": "tutor",
        }
        state2 = T.load_state(user_id=sid)
        if result.get("kind") == "lobby":
            tid = LOBBY
            lobby_conv = _get_thread(store, LOBBY)
            resp = (result.get("response") or "").strip()
            if resp:
                lobby_conv.append({"role": "assistant", "content": resp})
            store["active_thread"] = LOBBY
            _save_store(sid, store)
            out = _result_dict(result, sid)
            out["type"] = "done"
            out["thread_id"] = LOBBY
            out["history"] = _history_for_ui(lobby_conv)
            out["streamed"] = False
            yield _sse(out)
            return
        new_tid = _thread_id_from_state(state2)
        if result.get("kind") == "resume":
            resume = result.get("resume") or {}
            if resume.get("subject_id"):
                new_tid = str(resume["subject_id"])
            elif result.get("subject_id"):
                new_tid = str(result["subject_id"])
        routing = (result.get("routing") or {}) if isinstance(result, dict) else {}
        action = str(routing.get("action") or "").lower()
        if result.get("kind") == "new_subject" and result.get("subject_id"):
            new_tid = str(result["subject_id"])
        explicit_switch = (
            action in ("switch", "new_subject")
            or result.get("kind") in ("resume", "new_subject")
            or result.get("switched_from_thread")
        )
        if explicit_switch and new_tid and str(new_tid) != str(tid):
            tid = _apply_topic_switch(
                store, tid, new_tid, conv, result, message
            )
        elif explicit_switch and new_tid:
            store["active_thread"] = str(new_tid)
            tid = str(new_tid)
            if result.get("resume_reused"):
                state2 = T.load_state(user_id=sid)
                active = state2.get("active_session") or {}
                q = ((result.get("resume") or {}).get("resume_question") or "").strip()
                if q:
                    active["last_tutor_question"] = q[:300]
                    active["resume_instruction"] = q
                    active["just_resumed"] = True
                    T.save_state(state2, user_id=sid)
        store["active_thread"] = tid
        _save_store(sid, store)
        _log_turn(sid, message, tid_from, tid, result)
        out = _result_dict(result, sid)
        out["type"] = "done"
        out["thread_id"] = tid
        out["history"] = _history_for_ui(_get_thread(store, tid))
        # Avoid duplicating full response if tokens already streamed
        out["streamed"] = True
        yield _sse(out)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.post("/api/v1/session/reset")
def reset_session(request: Request, agent: str = "tutor"):
    sid = _require_user(request)
    store = _load_store(sid)
    tid = store.get("active_thread") or LOBBY
    store["threads"][tid] = []
    _save_store(sid, store)
    return {"ok": True, "session_id": sid, "thread_id": tid}


# Static UI
if WEB_DIR.is_dir():

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
