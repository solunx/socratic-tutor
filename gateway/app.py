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
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core  # noqa: E402
import tutor as T  # noqa: E402

WEB_DIR = ROOT / "web"
# Shared across devices: one learner, one chat log on disk
CONV_FILE = ROOT / "web_conversation.json"
DEFAULT_SESSION = "george"
MAX_CONV_MESSAGES = 80  # UI history; model still uses MAX_CONVERSATION_MESSAGES

app = FastAPI(
    title="Local Agent Gateway",
    version="0.2.0",
    description="Tutor + future agents over Tailscale",
)

# In-memory cache; disk is source of truth for DEFAULT_SESSION
_conversations: dict[str, list] = {}


def _load_conv(session_id: str) -> list:
    if session_id in _conversations:
        return _conversations[session_id]
    # All web clients share the same persisted log for the single learner
    path = CONV_FILE
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                _conversations[session_id] = data
                return data
            if isinstance(data, dict) and isinstance(data.get("messages"), list):
                _conversations[session_id] = data["messages"]
                return data["messages"]
        except Exception:
            pass
    _conversations[session_id] = []
    return _conversations[session_id]


def _save_conv(session_id: str, conv: list) -> None:
    # Bound on disk
    if len(conv) > MAX_CONV_MESSAGES:
        del conv[: len(conv) - MAX_CONV_MESSAGES]
    _conversations[session_id] = conv
    try:
        T.atomic_save_json(
            CONV_FILE,
            {
                "version": 1,
                "session_id": session_id,
                "messages": conv,
            },
        )
    except Exception:
        # Fallback write
        try:
            CONV_FILE.write_text(
                json.dumps({"version": 1, "messages": conv}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass


def _history_for_ui(conv: list) -> list[dict]:
    """Map OpenAI-style messages to UI bubbles (skip pure system summaries if needed)."""
    out = []
    for m in conv[-60:]:
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
    return out


class TurnRequest(BaseModel):
    message: str = Field(..., min_length=1)
    agent: str = "tutor"
    session_id: str = DEFAULT_SESSION
    channel: str = "web"


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


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "agents": ["tutor"],
        "model": T.MODEL_NAME,
        "stream_default": False,
        "default_session": DEFAULT_SESSION,
    }


@app.get("/api/bootstrap")
def bootstrap(agent: str = "tutor", session_id: str = DEFAULT_SESSION):
    if agent != "tutor":
        raise HTTPException(400, f"Unknown agent: {agent}")
    base = core.bootstrap()
    conv = _load_conv(session_id or DEFAULT_SESSION)
    base["session_id"] = session_id or DEFAULT_SESSION
    base["history"] = _history_for_ui(conv)
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


@app.post("/api/v1/turn", response_model=TurnResponse)
def turn(req: TurnRequest):
    if req.agent != "tutor":
        raise HTTPException(
            400,
            f"Agent '{req.agent}' not registered. Available: tutor",
        )
    sid = req.session_id or DEFAULT_SESSION
    conv = _load_conv(sid)
    state = T.load_state()
    result = core.handle_turn(
        req.message,
        state=state,
        conversation=conv,
        agent=req.agent,
        save=True,
        stream=False,
    )
    _save_conv(sid, conv)
    return result


@app.post("/api/v1/session/reset")
def reset_session(session_id: str = DEFAULT_SESSION, agent: str = "tutor"):
    sid = session_id or DEFAULT_SESSION
    _conversations[sid] = []
    _save_conv(sid, [])
    return {"ok": True, "session_id": sid}


# Static UI
if WEB_DIR.is_dir():

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
