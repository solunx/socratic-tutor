#!/usr/bin/env python3
"""Regression: start_session/close_session must never write into another user's file.

Run from project root:
  python tools/test_isolation_save.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tutor as T  # noqa: E402


def main() -> int:
    T.STRICT_USERS = True
    tmp = Path(tempfile.mkdtemp(prefix="tutor-iso-"))
    try:
        # Point auth paths at temp tree
        import auth

        auth.DATA_DIR = tmp / "data"
        auth.DATA_DIR.mkdir(parents=True)
        (auth.DATA_DIR / "users").mkdir()
        # Minimal users registry
        (auth.DATA_DIR / "users.json").write_text(
            json.dumps({"version": 1, "users": {"george": {}, "vriend1": {}}}),
            encoding="utf-8",
        )

        g_dir = auth.DATA_DIR / "users" / "george"
        v_dir = auth.DATA_DIR / "users" / "vriend1"
        g_dir.mkdir()
        v_dir.mkdir()

        # Seed george with zon only
        g_state = T.default_state()
        g_state["learner"]["id"] = "george"
        g_state["subjects"] = {
            "zon": {"title": "zon", "goal": "test", "concepts": {}}
        }
        (g_dir / "learner_state.json").write_text(
            json.dumps(g_state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Seed vriend1 with kernfusie
        v_state = T.default_state()
        v_state["learner"]["id"] = "vriend1"
        v_state["subjects"] = {
            "kernfusie": {"title": "kernfusie", "goal": "test", "concepts": {}}
        }
        (v_dir / "learner_state.json").write_text(
            json.dumps(v_state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        before_g = (g_dir / "learner_state.json").read_text(encoding="utf-8")

        # Simulate the old bug path: vriend1 state + start_session WITHOUT user_id
        st = json.loads((v_dir / "learner_state.json").read_text(encoding="utf-8"))
        assert st["learner"]["id"] == "vriend1"
        T.start_session(st, "kernfusie", is_resume=True)  # no user_id arg
        T.close_session(st, "resume q")  # no user_id arg

        after_g = (g_dir / "learner_state.json").read_text(encoding="utf-8")
        after_v = json.loads((v_dir / "learner_state.json").read_text(encoding="utf-8"))

        if before_g != after_g:
            print("FAIL: george's file was modified by vriend1 start/close_session")
            print("george subjects now:", list(json.loads(after_g).get("subjects") or {}))
            return 1
        if "kernfusie" not in (after_v.get("subjects") or {}):
            print("FAIL: vriend1 lost kernfusie")
            return 1
        if after_v.get("learner", {}).get("id") != "vriend1":
            print("FAIL: vriend1 learner.id changed")
            return 1

        # Explicit wrong user_id must raise
        st2 = json.loads((v_dir / "learner_state.json").read_text(encoding="utf-8"))
        try:
            T.save_state(st2, user_id="george")
            print("FAIL: save_state(vriend1 blob, user_id=george) should raise")
            return 1
        except RuntimeError as e:
            if "CROSS-USER" not in str(e):
                print("FAIL: unexpected error", e)
                return 1

        print("PASS: isolation save paths OK")
        print("  george subjects unchanged:", list(json.loads(after_g).get("subjects") or {}))
        print("  vriend1 subjects:", list(after_v.get("subjects") or {}))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
