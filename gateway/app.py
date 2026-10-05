#!/usr/bin/env python3
"""
Multi-agent gateway (Tailscale-friendly).

Currently registered: agent=tutor
Future: research, roast, inquiry, hermes — same /v1/turn contract.

Run on gpu01 (example):
  cd ~/dev/tutor
  python3 -m uvicorn gateway.app:app --host 0.0.0.0 --port 8787

Then open http://<tailscale-ip>:8787/ from iPhone or laptop on Tailscale.
Do not port-forward this to the public internet.
"""

from __future__ import annotations

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

app = FastAPI(
    title="Local Agent Gateway",
    version="0.1.0",
    description="Tutor + future agents over Tailscale",
)

# In-memory conversation per browser session id (state stays on disk)
_conversations: dict[str, list] = {}


class TurnRequest(BaseModel):
    message: str = Field(..., min_length=1)
    agent: str = "tutor"
    session_id: str = "default"
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
    }


@app.get("/api/bootstrap")
def bootstrap(agent: str = "tutor"):
    if agent != "tutor":
        raise HTTPException(400, f"Unknown agent: {agent}")
    return core.bootstrap()


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
    conv = _conversations.setdefault(req.session_id, [])
    state = T.load_state()
    result = core.handle_turn(
        req.message,
        state=state,
        conversation=conv,
        agent=req.agent,
        save=True,
        stream=False,
    )
    # Keep conversation list bounded in memory too
    if len(conv) > 40:
        del conv[: len(conv) - 40]
    return result


@app.post("/api/v1/session/reset")
def reset_session(session_id: str = "default", agent: str = "tutor"):
    _conversations[session_id] = []
    return {"ok": True, "session_id": session_id}


# Static UI
if WEB_DIR.is_dir():
    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
