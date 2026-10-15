#!/usr/bin/env python3
"""Remove foreign subjects/threads from a user store.

Example (george had vriend1 topics mixed into web_conversation):
  python tools/unmix_user.py george --remove kernfusie ik_wil_leren_over_efficient_vergaderen
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import auth
import tutor as T


def main():
    p = argparse.ArgumentParser()
    p.add_argument("user_id")
    p.add_argument("--remove", nargs="+", required=True, help="subject/thread ids to remove")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    uid = args.user_id.strip().lower()
    remove = {T.normalize_id(x) for x in args.remove}
    # also accept raw ids
    remove |= {x.strip() for x in args.remove}

    state_p = auth.state_path(uid)
    conv_p = auth.conv_path(uid)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    state = T.load_state(user_id=uid)
    subs = state.get("subjects") or {}
    removed_subs = []
    for sid in list(subs.keys()):
        if sid in remove or T.normalize_id(sid) in remove:
            removed_subs.append(sid)
            if not args.dry_run:
                del subs[sid]

    active = state.get("active_session") or {}
    if active.get("subject_id") in remove:
        if not args.dry_run:
            active["active"] = False
            active["subject_id"] = None
            active["current_concept_id"] = None

    removed_threads = []
    conv = {}
    if conv_p.exists():
        conv = json.loads(conv_p.read_text(encoding="utf-8"))
        threads = conv.get("threads") or {}
        for tid in list(threads.keys()):
            if tid in remove or T.normalize_id(tid) in remove:
                removed_threads.append(tid)
                if not args.dry_run:
                    del threads[tid]
        if conv.get("active_thread") in remove:
            if not args.dry_run:
                conv["active_thread"] = "_lobby"

    print(f"User: {uid}")
    print(f"  subjects to remove: {removed_subs}")
    print(f"  threads to remove:  {removed_threads}")
    if args.dry_run:
        print("dry-run — nothing written")
        return

    # backups
    if state_p.exists():
        shutil.copy2(state_p, state_p.with_suffix(f".json.bak-unmix-{stamp}"))
    if conv_p.exists():
        shutil.copy2(conv_p, conv_p.with_suffix(f".json.bak-unmix-{stamp}"))

    T.save_state(state, user_id=uid)
    if conv:
        tmp = conv_p.with_suffix(".tmp")
        tmp.write_text(json.dumps(conv, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(conv_p)
    print("done — backups written next to state/conv files")


if __name__ == "__main__":
    main()
