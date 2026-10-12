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
from typing import Any, Generator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core  # noqa: E402
import tutor as T  # noqa: E402

WEB_DIR = ROOT / "web"
CONV_FILE = ROOT / "web_conversation.json"
DEFAULT_SESSION = "george"
MAX_CONV_MESSAGES = 200
LOBBY = "_lobby"

app = FastAPI(
    title="Local Agent Gateway",
    version="0.3.0",
    description="Tutor + future agents over Tailscale",
)


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
    if session_id in _store:
        store = _normalize_store(_store[session_id])
        _store[session_id] = store
        return store
    path = CONV_FILE
    store = _empty_store()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
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
        T.atomic_save_json(CONV_FILE, payload)
    except Exception:
        try:
            CONV_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass


def _thread_id_from_state(state: dict) -> str:
    active = state.get("active_session") or {}
    sid = active.get("subject_id")
    if sid and active.get("active"):
        return str(sid)
    if sid:
        return str(sid)
    return LOBBY


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
        elif role == "system" and "[session boundary" in content:
            out.append({"role": "system", "text": "— onderwerp gewisseld —"})
    return out


def _result_dict(result: dict) -> dict:
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
    ):
        if key in result and result[key] is not None:
            out[key] = result[key]
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
def bootstrap(agent: str = "tutor", session_id: str = DEFAULT_SESSION):
    if agent != "tutor":
        raise HTTPException(400, f"Unknown agent: {agent}")
    base = core.bootstrap()
    store = _load_store(session_id or DEFAULT_SESSION)
    state = T.load_state()
    tid = store.get("active_thread") or _thread_id_from_state(state)
    conv = _get_thread(store, tid)
    base["session_id"] = session_id or DEFAULT_SESSION
    base["thread_id"] = tid
    base["history"] = _history_for_ui(conv)
    base["threads"] = {
        k: len(v) for k, v in (store.get("threads") or {}).items() if k != LOBBY or v
    }
    return base


@app.get("/api/subjects")
def subjects(agent: str = "tutor"):
    if agent != "tutor":
        raise HTTPException(400, f"Unknown agent: {agent}")
    state = T.load_state()
    return {"ok": True, "subjects": core.subjects_payload(state)}


@app.get("/api/state")
def state_summary(agent: str = "tutor"):
    if agent != "tutor":
        raise HTTPException(400, f"Unknown agent: {agent}")
    state = T.load_state()
    return {"ok": True, "summary": core.state_summary(state)}


@app.get("/api/thread/{thread_id}")
def get_thread(thread_id: str, session_id: str = DEFAULT_SESSION, agent: str = "tutor"):
    if agent != "tutor":
        raise HTTPException(400, f"Unknown agent: {agent}")
    store = _load_store(session_id)
    tid = thread_id or LOBBY
    store["active_thread"] = tid
    conv = _get_thread(store, tid)
    _save_store(session_id, store)
    return {
        "ok": True,
        "thread_id": tid,
        "history": _history_for_ui(conv),
    }


@app.post("/api/v1/turn", response_model=TurnResponse)
def turn(req: TurnRequest):
    if req.agent != "tutor":
        raise HTTPException(400, f"Agent '{req.agent}' not registered. Available: tutor")
    sid = req.session_id or DEFAULT_SESSION
    store = _load_store(sid)
    state = T.load_state()
    tid = req.thread_id or store.get("active_thread") or _thread_id_from_state(state)
    conv = _get_thread(store, tid)
    result = core.handle_turn(
        req.message,
        state=state,
        conversation=conv,
        agent=req.agent,
        save=True,
        stream=False,
    )
    # After routing, may have switched subject
    state2 = T.load_state()
    new_tid = _thread_id_from_state(state2)
    if new_tid != tid and result.get("kind") in ("resume", "new_subject", "message"):
        # Move last exchange into the target thread if subject changed
        new_conv = _get_thread(store, new_tid)
        # messages already appended to old conv by handle_turn; copy tail if switch
        if conv and new_tid != tid:
            # keep boundary note on old; ensure new has recent user/assistant
            tail = [m for m in conv[-4:] if isinstance(m, dict)]
            for m in tail:
                if m not in new_conv:
                    new_conv.append(m)
        tid = new_tid
    store["active_thread"] = tid
    _save_store(sid, store)
    out = _result_dict(result)
    out["thread_id"] = tid
    out["history"] = _history_for_ui(_get_thread(store, tid))
    return out


@app.post("/api/v1/turn/stream")
def turn_stream(req: TurnRequest):
    """SSE: type=start | token | done. Tokens arrive live while the model generates."""
    if req.agent != "tutor":
        raise HTTPException(400, f"Agent '{req.agent}' not registered.")

    sid = req.session_id or DEFAULT_SESSION
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(400, "Empty message")

    def event_stream() -> Generator[str, None, None]:
        import queue
        import threading
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
        state = T.load_state()
        tid = req.thread_id or store.get("active_thread") or _thread_id_from_state(state)
        conv = _get_thread(store, tid)

        # Commands: instant done, no tokens
        cmd = core._handle_command(state, conv, message)
        if cmd is not None:
            T.save_state(state)
            store["active_thread"] = tid
            _save_store(sid, store)
            payload = _result_dict(cmd)
            payload["type"] = "done"
            payload["thread_id"] = tid
            payload["history"] = _history_for_ui(conv)
            yield _sse({"type": "start", "thread_id": tid})
            yield _sse(payload)
            return

        q: queue.Queue = queue.Queue()
        result_box: dict = {}

        real_ask = T.ask_model

        def ask_wrapper(messages, stream=None, on_response_delta=None, live_print=None):
            # Honour explicit non-stream (JSON retry path)
            if stream is False:
                return real_ask(messages, stream=False, live_print=False)

            def _cb(partial, delta):
                q.put(("token", delta))
                if on_response_delta:
                    try:
                        on_response_delta(partial, delta)
                    except Exception:
                        pass

            return real_ask(
                messages,
                stream=True,
                on_response_delta=_cb,
                live_print=False,
            )

        def worker():
            old_stream = T.STREAM
            T.STREAM = True
            T.ask_model = ask_wrapper
            try:
                result_box["result"] = core.handle_turn(
                    message,
                    state=state,
                    conversation=conv,
                    agent="tutor",
                    save=True,
                    stream=True,
                )
            except Exception as e:
                result_box["error"] = str(e)
            finally:
                T.ask_model = real_ask
                T.STREAM = old_stream
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
        state2 = T.load_state()
        new_tid = _thread_id_from_state(state2)
        # If subject changed, ensure thread exists and keep active pointer
        if new_tid != tid:
            new_conv = _get_thread(store, new_tid)
            for m in conv[-6:]:
                if isinstance(m, dict) and m not in new_conv:
                    new_conv.append(m)
        store["active_thread"] = new_tid
        _save_store(sid, store)
        out = _result_dict(result)
        out["type"] = "done"
        out["thread_id"] = new_tid
        out["history"] = _history_for_ui(_get_thread(store, new_tid))
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
def reset_session(session_id: str = DEFAULT_SESSION, agent: str = "tutor"):
    sid = session_id or DEFAULT_SESSION
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
