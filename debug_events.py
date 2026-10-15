#!/usr/bin/env python3
"""
Per-user event log for debugging multi-thread / lobby / switch UX.

Writes one JSON line per event under:
  data/users/<user_id>/events.jsonl

Never logs passwords. Message text is truncated (see MAX_TEXT).
Disable with TUTOR_EVENT_LOG=0.

CLI:
  python tools/tail_events.py [user_id] [--last N]
  python -m debug_events [user_id] [--last N]
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "users"
MAX_TEXT = int(os.environ.get("TUTOR_EVENT_LOG_MAX_TEXT", "800"))
ENABLED = os.environ.get("TUTOR_EVENT_LOG", "1") != "0"


def _path(user_id: str) -> Path:
    uid = (user_id or "unknown").strip() or "unknown"
    # sanitize path segment
    uid = "".join(c for c in uid if c.isalnum() or c in "-_")[:64] or "unknown"
    d = DATA_DIR / uid
    d.mkdir(parents=True, exist_ok=True)
    return d / "events.jsonl"


def _clip(text: str | None, n: int = MAX_TEXT) -> str:
    t = (text or "").replace("\r\n", "\n").strip()
    if len(t) <= n:
        return t
    return t[: n - 1] + "…"


def log_event(user_id: str, event: str, **fields: Any) -> None:
    """Append one event. Safe to call from request handlers (best-effort)."""
    if not ENABLED:
        return
    try:
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": str(event),
            "user": user_id or "unknown",
        }
        for k, v in fields.items():
            if v is None:
                continue
            if k in ("text", "response", "message", "user_text", "assistant_text"):
                rec[k] = _clip(str(v))
            elif isinstance(v, (str, int, float, bool)):
                rec[k] = v
            else:
                try:
                    rec[k] = json.loads(json.dumps(v, default=str))
                except Exception:
                    rec[k] = str(v)[:500]
        p = _path(user_id)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
        # Mirror for ops (easy tail without knowing user id)
        try:
            mirror = BASE_DIR / "metrics" / "events_all.jsonl"
            mirror.parent.mkdir(parents=True, exist_ok=True)
            with open(mirror, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass
    except Exception:
        # Never break the tutor on logging failure
        pass


def log_turn(
    user_id: str,
    *,
    message: str,
    thread_from: str | None,
    thread_to: str | None,
    result: dict | None,
) -> None:
    """Log one completed tutor turn (after gateway has final thread_id)."""
    r = result or {}
    routing = r.get("routing") if isinstance(r.get("routing"), dict) else {}
    policy = r.get("policy") if isinstance(r.get("policy"), dict) else {}
    log_event(
        user_id,
        "turn",
        thread_from=thread_from or "",
        thread_to=thread_to or "",
        switched=bool(
            thread_from
            and thread_to
            and str(thread_from) != str(thread_to)
        ),
        kind=r.get("kind") or "",
        ok=bool(r.get("ok", True)),
        routing_action=str(routing.get("action") or ""),
        subject_id=r.get("subject_id") or routing.get("subject") or "",
        policy_activity=str(policy.get("activity") or ""),
        policy_force=bool(policy.get("force")) if policy else None,
        message=_clip(message),
        response=_clip(r.get("response") or ""),
        error=_clip(r.get("error")) if r.get("error") else None,
    )


def read_events(user_id: str, last: int = 50) -> list[dict]:
    p = _path(user_id)
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[-max(1, last) :]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"raw": line[:200]})
    return out


def format_events(events: list[dict]) -> str:
    rows = []
    for e in events:
        ts = e.get("ts", "")
        ev = e.get("event", "")
        if ev == "turn":
            sw = " SWITCH" if e.get("switched") else ""
            rows.append(
                f"{ts}  turn{sw}  {e.get('thread_from','')} → {e.get('thread_to','')}  "
                f"kind={e.get('kind')}  route={e.get('routing_action') or '-'}  "
                f"policy={e.get('policy_activity') or '-'}"
            )
            if e.get("message"):
                rows.append(f"         YOU: {e['message'][:200]}")
            if e.get("response"):
                rows.append(f"         BOT: {e['response'][:200]}")
        else:
            rows.append(f"{ts}  {ev}  " + json.dumps(
                {k: v for k, v in e.items() if k not in ("ts", "event", "user")},
                ensure_ascii=False,
            )[:240])
    return "\n".join(rows)


def ping(user_id: str = "george") -> str:
    """Write a test event; return path. Use to verify logging works."""
    log_event(user_id, "ping", note="debug_events.ping")
    return str(_path(user_id))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Show recent tutor debug events")
    ap.add_argument("user_id", nargs="?", default="george")
    ap.add_argument("--last", type=int, default=40)
    args = ap.parse_args()
    ev = read_events(args.user_id, last=args.last)
    if not ev:
        print(f"No events for {args.user_id} yet ({_path(args.user_id)})")
    else:
        print(format_events(ev))
