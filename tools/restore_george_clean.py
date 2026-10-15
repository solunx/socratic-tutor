#!/usr/bin/env python3
"""Restore george to AI-agents + zon from the known-good bak-unmix backup.

Run ONLY after the isolation patch is deployed (tutor.py save_state fix).

  cd ~/dev/tutor && source .venv/bin/activate
  python tools/restore_george_clean.py
  sudo systemctl restart tutor-gateway
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
G = ROOT / "data" / "users" / "george"
KEEP = {"ai_agents", "zon"}
BAK_NAME = "learner_state.json.bak-unmix-20261015-182446"


def main() -> None:
    if not G.is_dir():
        raise SystemExit(f"missing {G}")
    bak = G / BAK_NAME
    if not bak.exists():
        # pick largest bak with ai_agents
        cands = []
        for p in G.glob("learner_state.json.bak*"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                subs = d.get("subjects") or {}
                if "ai_agents" in subs and "zon" in subs:
                    cands.append((len(subs), p.stat().st_size, p))
            except Exception:
                continue
        if not cands:
            raise SystemExit("no backup with ai_agents+zon found")
        cands.sort(reverse=True)
        bak = cands[0][2]
        print("Using backup:", bak.name)
    else:
        print("Using backup:", bak.name)

    ts = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(G / "learner_state.json", G / f"learner_state.json.bak-before-restore-{ts}")
    if (G / "web_conversation.json").exists():
        shutil.copy2(
            G / "web_conversation.json",
            G / f"web_conversation.json.bak-before-restore-{ts}",
        )

    data = json.loads(bak.read_text(encoding="utf-8"))
    print("ids in bak:", list((data.get("subjects") or {}).keys()))
    data.setdefault("learner", {})["id"] = "george"
    data["subjects"] = {
        k: v for k, v in (data.get("subjects") or {}).items() if k in KEEP
    }
    data["history"] = [
        h
        for h in (data.get("history") or [])
        if isinstance(h, dict) and h.get("subject_id") in KEEP
    ]
    data["review_queue"] = [
        r
        for r in (data.get("review_queue") or [])
        if isinstance(r, dict) and r.get("subject_id") in KEEP
    ]
    a = data.get("active_session") or {}
    if a.get("subject_id") not in KEEP:
        data["active_session"] = {
            **a,
            "active": False,
            "subject_id": None,
            "current_concept_id": None,
        }
    (G / "learner_state.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    conv_path = G / "web_conversation.json"
    if conv_path.exists():
        conv = json.loads(conv_path.read_text(encoding="utf-8"))
        threads = conv.get("threads") or {}
        conv["threads"] = {
            k: v for k, v in threads.items() if k in KEEP or k == "_lobby"
        }
        conv["session_id"] = "george"
        if conv.get("active_thread") not in conv["threads"]:
            conv["active_thread"] = "_lobby"
        conv_path.write_text(
            json.dumps(conv, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    print("Restored george subjects:", list(data["subjects"].keys()))
    print("OK — restart gateway and hard-refresh browsers")


if __name__ == "__main__":
    main()
