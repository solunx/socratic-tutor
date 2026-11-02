"""GREEN. tools/fix_user_state.py against a trimmed copy of the real poisoned state."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "fix_user_state.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "learner_state_vriend1_before.json"
ARGS = ["--subject", "kernfusie", "--concept", "kernfusie", "--drop-mc-containing", "larger nuclei",
        "--reset-fsrs", "--reset-session-memory"]


def _setup(tmp_path, user="vriend1", as_user=None):
    d = tmp_path / "data" / "users" / (as_user or user)
    d.mkdir(parents=True)
    shutil.copy(FIXTURE, d / "learner_state.json")
    return d / "learner_state.json"


def _run(tmp_path, user, *extra):
    return subprocess.run([sys.executable, str(SCRIPT), "--user", user, "--data-dir", str(tmp_path / "data"), *ARGS, *extra],
                          capture_output=True, text=True)


def _concept(path):
    return json.loads(path.read_text(encoding="utf-8"))["subjects"]["kernfusie"]["concepts"]["kernfusie"]


def test_dry_run_changes_nothing(tmp_path):
    p = _setup(tmp_path)
    before = p.read_bytes()
    r = _run(tmp_path, "vriend1", "--dry-run")
    assert r.returncode == 0 and "larger nuclei" in r.stdout
    assert p.read_bytes() == before and not list(p.parent.glob("*.bak-fix-*"))


def test_fix_removes_false_misconception_resets_fsrs_and_keeps_a_backup(tmp_path):
    p = _setup(tmp_path)
    original = p.read_text(encoding="utf-8")
    assert _concept(p)["misconceptions"], "fixture must contain the poisoned misconception"
    assert _run(tmp_path, "vriend1").returncode == 0
    c, s = _concept(p), json.loads(p.read_text(encoding="utf-8"))
    assert c["misconceptions"] == []
    assert c["review_count"] == 0 and c["lapses"] == 0 and c["difficulty"] is None and c["next_review"] is None
    a = s["active_session"]
    assert a["session_thread"] is None and a["recent_questions"] == [] and a["echo_teach_flag"] is False
    assert s["learner"]["id"] == "vriend1"
    assert c["evidence"], "mastery evidence must be preserved"
    baks = list(p.parent.glob("*.bak-fix-*"))
    assert len(baks) == 1 and baks[0].read_text(encoding="utf-8") == original


def test_refuses_when_learner_id_does_not_match_the_user(tmp_path):
    p = _setup(tmp_path, user="vriend1", as_user="george")   # vriend1's state sitting in george's folder
    before = p.read_bytes()
    r = _run(tmp_path, "george")
    assert r.returncode == 3 and "cross-user" in r.stderr
    assert p.read_bytes() == before


def test_unknown_concept_is_a_clean_error(tmp_path):
    _setup(tmp_path)
    r = subprocess.run([sys.executable, str(SCRIPT), "--user", "vriend1", "--data-dir", str(tmp_path / "data"),
                        "--subject", "nope", "--concept", "nope", "--reset-fsrs"], capture_output=True, text=True)
    assert r.returncode != 0


def test_fixed_state_is_loadable_by_the_tutor(tmp_path):
    """The reset FSRS fields must be re-seedable by tutor.ensure_fsrs_fields (no None crash)."""
    import tutor as T
    p = _setup(tmp_path)
    assert _run(tmp_path, "vriend1").returncode == 0
    state = json.loads(p.read_text(encoding="utf-8"))
    c = state["subjects"]["kernfusie"]["concepts"]["kernfusie"]
    T.ensure_fsrs_fields(c)
    assert c["stability"] is not None and c["difficulty"] is not None
    T.rebuild_review_queue(state)
