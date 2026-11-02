#!/usr/bin/env python3
"""
Repair a learner_state.json that was poisoned by a tutor mistake or by per-turn FSRS grading.

  python tools/fix_user_state.py --user vriend1 --subject kernfusie --concept kernfusie \\
      --drop-mc-containing "larger nuclei" --reset-fsrs --reset-session-memory --dry-run
  (same command without --dry-run to apply)

Safety (this project had a cross-user overwrite incident, so these are not optional):
  * works on data/users/<user>/learner_state.json ONLY, never on a default user
  * refuses when state.learner.id != --user
  * writes a timestamped .bak-fix-* copy first, then an atomic replace
  * --dry-run changes nothing and prints what would change
Stdlib only; does not import the tutor.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

SESSION_MEMORY_DEFAULTS = {
    "session_thread": None, "resume_instruction": None, "last_tutor_question": None,
    "recent_questions": [], "banned_questions": [], "open_threads": [],
    "last_verdict": None, "pending_verdict_feedback": False, "partial_missing": False,
    "echo_teach_flag": False, "must_advance": False, "repeat_streak": 0, "reask_count": 0,
    "same_activity_streak": 0, "last_learner_signal": None, "prefer_probe_type": None,
    "stuck_signal": False, "learner_correction": False, "force_scaffold": False,
    "force_full_teach": False, "confused_by_tutor": False, "repetition_feedback": False,
    "soft_incorrect": False,
}
FSRS_RESET = {
    "review_count": 0, "reps": 0, "lapses": 0, "stability": None, "difficulty": None,
    "fsrs_state": "new", "last_grade": None, "last_review": None, "next_review": None,
    "fsrs_session": None,
}


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--user", required=True)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--subject")
    p.add_argument("--concept")
    p.add_argument("--drop-mc-containing", action="append", default=[], metavar="TEXT")
    p.add_argument("--reset-fsrs", action="store_true")
    p.add_argument("--reset-session-memory", action="store_true")
    p.add_argument("--clear-established", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    return p


def plan(state, a):
    """Return (new_state, [human readable changes])."""
    new, changes = copy.deepcopy(state), []
    if a.drop_mc_containing or a.reset_fsrs:
        if not (a.subject and a.concept):
            raise SystemExit("--subject and --concept are required for --drop-mc-containing / --reset-fsrs")
        try:
            c = new["subjects"][a.subject]["concepts"][a.concept]
        except KeyError:
            raise SystemExit(f"unknown subject/concept: {a.subject}/{a.concept}")
        if a.drop_mc_containing:
            keep, drop = [], []
            for m in c.get("misconceptions") or []:
                text = str(m.get("description") or "").lower()
                (drop if any(t.lower() in text for t in a.drop_mc_containing) else keep).append(m)
            c["misconceptions"] = keep
            changes += [f"drop misconception: {str(m.get('description'))[:80]}" for m in drop] or ["no misconception matched"]
        if a.reset_fsrs:
            before = {k: c.get(k) for k in ("review_count", "lapses", "difficulty", "stability")}
            c.update(FSRS_RESET)
            changes.append(f"reset FSRS {before} -> fresh")
    act = new.get("active_session") or {}
    if a.reset_session_memory:
        act.update(copy.deepcopy(SESSION_MEMORY_DEFAULTS))
        changes.append("clear session memory (thread, resume text, recent/banned questions, flags)")
    if a.clear_established:
        act["established_points"] = []
        changes.append("clear established_points")
    new["active_session"] = act
    return new, changes


def atomic_write(path: Path, data: dict):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    path = Path(a.data_dir) / "users" / a.user / "learner_state.json"
    if not path.exists():
        print(f"not found: {path}", file=sys.stderr)
        return 2
    state = json.loads(path.read_text(encoding="utf-8"))
    lid = str((state.get("learner") or {}).get("id") or "")
    if lid != a.user:
        print(f"REFUSING: state.learner.id={lid!r} but --user={a.user!r} (cross-user guard)", file=sys.stderr)
        return 3
    new, changes = plan(state, a)
    print(f"{'DRY-RUN ' if a.dry_run else ''}{path}")
    for c in changes or ["nothing to change"]:
        print("  -", c)
    if a.dry_run or new == state:
        return 0
    bak = path.with_name(f"{path.name}.bak-fix-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, bak)
    atomic_write(path, new)
    print(f"backup: {bak.name}\nwritten: {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
