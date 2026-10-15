#!/usr/bin/env python3
"""Restore george to the known-good full state (AI-agents + zon mastery).

Picks the best backup among:
  data/users/george/learner_state.json.bak-unmix-20261015-182446
  learner_state_full.json (repo root, if present)
  other bak* with learner.id=george and ai_agents

Also strips vriend1 threads from web_conversation.json.
"""
from __future__ import annotations
import json
import shutil
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import auth

UID = "george"
STATE = auth.state_path(UID)
CONV = auth.conv_path(UID)
DROP_THREADS = {
    "kernfusie",
    "ik_wil_leren_over_efficient_vergaderen",
    "gras_groeien",
    "ai_zonder_llms",
    "fascia",
}


def score_candidate(path: Path) -> tuple[int, dict] | None:
    try:
        cand = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    lid = str((cand.get("learner") or {}).get("id") or "").strip().lower()
    # Reject explicit other users
    if lid and lid not in (UID, ""):
        return None
    subs = cand.get("subjects") or {}
    if "ai_agents" not in subs or "zon" not in subs:
        return None
    # Prefer more concepts under zon
    zon = (subs.get("zon") or {}).get("concepts") or {}
    score = 1000 + len(subs) * 10 + len(zon)
    return score, cand


def main():
    candidates = [
        STATE.parent / "learner_state.json.bak-unmix-20261015-182446",
        ROOT / "learner_state_full.json",
        *sorted(STATE.parent.glob("learner_state.json.bak*")),
    ]
    best = None
    best_score = -1
    best_path = None
    for p in candidates:
        if not p.exists():
            continue
        r = score_candidate(p)
        if not r:
            continue
        sc, cand = r
        if sc > best_score:
            best_score = sc
            best = cand
            best_path = p

    if best is None:
        print("No good george backup found (need ai_agents + zon).")
        print("Looked in:", STATE.parent)
        sys.exit(1)

    print("Using backup:", best_path)
    print("  subjects:", list((best.get("subjects") or {}).keys()))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if STATE.exists():
        bad = STATE.with_suffix(f".json.bak-before-repair-{stamp}")
        shutil.copy2(STATE, bad)
        print("Quarantined current →", bad.name)

    best.setdefault("learner", {})["id"] = UID
    # drop empty vriend shells if any
    for sid in list((best.get("subjects") or {}).keys()):
        if sid in DROP_THREADS:
            sub = best["subjects"][sid]
            if not (sub.get("concepts") or {}):
                del best["subjects"][sid]
                print("removed empty shell", sid)

    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(best, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE)
    print("Restored state →", STATE)

    if CONV.exists():
        shutil.copy2(CONV, CONV.with_suffix(f".json.bak-before-repair-{stamp}"))
        conv = json.loads(CONV.read_text(encoding="utf-8"))
        threads = conv.get("threads") or {}
        for tid in list(threads.keys()):
            if tid in DROP_THREADS:
                del threads[tid]
                print("removed thread", tid)
        conv["session_id"] = UID
        conv["active_thread"] = "_lobby"
        # Keep only last ~40 lobby msgs to drop contaminated /state lines
        lobby = threads.get("_lobby") or []
        if len(lobby) > 40:
            threads["_lobby"] = lobby[-40:]
        tmp = CONV.with_suffix(".tmp")
        tmp.write_text(json.dumps(conv, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CONV)
        print("Cleaned conversation")

    print()
    print("OK. Next:")
    print("  sudo systemctl restart tutor-gateway")
    print("  python tools/check_users.py")
    print("  # expect george subjects: AI-agents, zon")
    print("  # hard refresh browser, logout, login as george")


if __name__ == "__main__":
    main()
