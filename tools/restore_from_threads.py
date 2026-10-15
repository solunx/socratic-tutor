#!/usr/bin/env python3
"""Rebuild missing subjects on a user from web_conversation thread keys.

Does NOT restore mastery/evidence — only subject shells so the sidebar matches chats.
Use after accidental overwrite of learner_state.json.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import auth
import tutor as T


SKIP = {"_lobby", "lobby", "default"}

TITLE_MAP = {
    "ai_agents": "AI-agents",
    "ai_zonder_llms": "AI zonder LLMs",
    "gras_groeien": "Gras groeien",
    "fascia": "Fascia",
    "zon": "zon",
    "kernfusie": "kernfusie",
}


def main():
    uid = (sys.argv[1] if len(sys.argv) > 1 else "george").strip().lower()
    state_p = auth.state_path(uid)
    conv_p = auth.conv_path(uid)
    if not conv_p.exists():
        print("No web_conversation for", uid, conv_p)
        return
    conv = json.loads(conv_p.read_text(encoding="utf-8"))
    threads = conv.get("threads") or {}
    state = T.load_state(user_id=uid)
    subjects = state.setdefault("subjects", {})
    added = []
    for tid in threads:
        if tid in SKIP or not tid:
            continue
        if tid in subjects:
            continue
        # humanize id
        title = TITLE_MAP.get(tid) or tid.replace("_", " ").strip()
        if title and tid not in TITLE_MAP:
            title = title[0].upper() + title[1:]
        subjects[tid] = {
            "title": title,
            "goal": None,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "parent_id": None,
            "related": [],
            "aliases": [],
            "concepts": {},
            "diagnostic": {
                "status": "done",
                "turns": 0,
                "max_turns": 3,
                "level_estimate": "emerging",
                "notes": "restored shell from web thread after state overwrite",
            },
        }
        added.append(tid)
    # Close any active session pointing at foreign friend subjects if desired
    T.save_state(state, user_id=uid)
    print(f"User {uid}: added {len(added)} subject shells: {added}")
    print(f"Total subjects now: {len(subjects)}")
    print("Mastery/evidence was NOT restored — search backups for full state.")


if __name__ == "__main__":
    main()
