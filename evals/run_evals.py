#!/usr/bin/env python3
"""
Eval harness for the Socratic Offline Tutor (Phase B).

Usage (from project root or evals/):
  python3 evals/run_evals.py              # all cases (needs Ollama for LLM cases)
  python3 evals/run_evals.py --policy     # only policy_only cases (no LLM)
  python3 evals/run_evals.py --case 02    # cases whose id contains "02"
  python3 evals/run_evals.py --list

Exit code 0 = all passed, 1 = failures.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
ROOT = EVALS_DIR.parent
CASES_DIR = EVALS_DIR / "cases"

sys.path.insert(0, str(ROOT))

from tutor import (  # noqa: E402
    ask_model_with_json_retry,
    build_messages,
    default_state,
    ensure_concept,
    ensure_subject,
    suggest_next_activity,
    load_system_prompt,
    schedule_fsrs,
    grade_from_assessment,
    ensure_fsrs_fields,
    update_pending_catalog,
    link_subjects,
    resolve_subject_id,
    apply_taxonomy_proposals,
    apply_prerequisite_proposals,
)


# ------------------------------------------------------------
# State factory from case setup
# ------------------------------------------------------------

def build_state_from_setup(setup: dict, mode: str) -> dict:
    state = default_state()
    subjects = setup.get("subjects")
    if subjects:
        for s in subjects:
            ensure_subject(state, s["id"], s.get("title") or s["id"])
            if s.get("goal"):
                state["subjects"][s["id"]]["goal"] = s["goal"]

    sid = setup.get("subject_id")
    if sid:
        ensure_subject(state, sid, setup.get("subject_title") or sid)
        if setup.get("goal"):
            state["subjects"][sid]["goal"] = setup["goal"]

        cid = setup.get("concept_id")
        if cid:
            ensure_concept(state, sid, cid)
            concept = state["subjects"][sid]["concepts"][cid]
            if setup.get("concept_title"):
                concept["title"] = setup["concept_title"]
            for k, v in (setup.get("scores") or {}).items():
                if k in concept:
                    concept[k] = float(v)
            for m in setup.get("misconceptions") or []:
                concept.setdefault("misconceptions", []).append(dict(m))

        active = mode != "meta" and setup.get("active", True)
        state["active_session"].update({
            "active": bool(active),
            "subject_id": sid if active else None,
            "started_at": datetime.now().isoformat(),
            "last_activity": datetime.now().isoformat(),
            "question_number": 1,
            "current_concept_id": setup.get("concept_id"),
            "current_activity": None,
            "resume_instruction": setup.get("resume_instruction"),
            "pending_goal": False,
            "just_resumed": bool(setup.get("just_resumed")),
            "session_thread": setup.get("session_thread"),
            "last_tutor_question": setup.get("last_tutor_question"),
            "recent_questions": list(setup.get("recent_questions") or []),
        })
    elif setup.get("active") is False:
        state["active_session"]["active"] = False

    return state


# ------------------------------------------------------------
# Assert helpers
# ------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def run_assert(assertion: dict, result: dict | None, raw: str, policy: dict | None) -> tuple[bool, str]:
    t = assertion.get("type")

    if t == "json_valid":
        ok = result is not None and isinstance(result, dict)
        return ok, "valid JSON object" if ok else f"invalid/missing JSON (raw[:120]={raw[:120]!r})"

    if t == "has_keys":
        keys = assertion.get("keys") or []
        missing = [k for k in keys if k not in (result or {})]
        ok = not missing
        return ok, "keys ok" if ok else f"missing keys: {missing}"

    if t == "response_nonempty":
        resp = str((result or {}).get("response") or "").strip()
        ok = len(resp) > 0
        return ok, f"response len={len(resp)}"

    if t == "routing_action_in":
        action = str(((result or {}).get("routing") or {}).get("action") or "").lower()
        values = [v.lower() for v in (assertion.get("values") or [])]
        ok = action in values
        return ok, f"action={action!r} in {values}" if ok else f"action={action!r} not in {values}"

    if t == "assessment_activity_in":
        act = str(((result or {}).get("assessment") or {}).get("activity") or "").upper()
        values = [v.upper() for v in (assertion.get("values") or [])]
        ok = act in values
        return ok, f"activity={act!r}" if ok else f"activity={act!r} not in {values}"

    if t == "session_has_last_question":
        sess = (result or {}).get("session") or {}
        lq = sess.get("last_question") or sess.get("last_tutor_question")
        ok = bool(lq and str(lq).strip())
        return ok, "last_question set" if ok else "session.last_question missing"

    if t == "response_max_chars":
        resp = str((result or {}).get("response") or "")
        mx = int(assertion.get("max") or 1000)
        ok = len(resp) <= mx
        return ok, f"len={len(resp)} <= {mx}" if ok else f"len={len(resp)} > {mx}"

    if t == "response_not_contains_near":
        resp = _norm(str((result or {}).get("response") or ""))
        for phrase in assertion.get("forbidden_phrases") or []:
            p = _norm(phrase)
            if p and p in resp:
                return False, f"contains forbidden near-match: {phrase[:60]!r}"
        return True, "no forbidden phrases"

    if t == "response_has_question_or_scaffold":
        resp = str((result or {}).get("response") or "")
        has_q = "?" in resp or "？" in resp
        # soft: short instructional nudge without full lecture still ok if has ?
        ok = has_q or len(resp) < 400
        return ok, "has ? or short scaffold" if ok else "no question and long response"

    if t == "policy_activity":
        expected = assertion.get("value")
        got = (policy or {}).get("activity")
        ok = got == expected
        return ok, f"policy activity={got!r}" if ok else f"expected {expected!r}, got {got!r}"

    if t == "policy_force":
        expected = bool(assertion.get("value"))
        got = bool((policy or {}).get("force"))
        ok = got == expected
        return ok, f"force={got}" if ok else f"expected force={expected}, got {got}"

    if t == "fsrs_again_short_interval":
        concept = (policy or {}).get("_fsrs_concept")
        if not concept:
            return False, "no concept for FSRS test"
        ensure_fsrs_fields(concept)
        before = float(concept.get("stability") or 0.5)
        schedule_fsrs(concept, 1)  # Again
        nr = concept.get("next_review")
        try:
            delta_h = (datetime.fromisoformat(nr) - datetime.now()).total_seconds() / 3600.0
        except Exception:
            return False, f"bad next_review {nr!r}"
        ok = delta_h <= 36  # within ~1.5 days
        return ok, f"Again: stability {before}→{concept['stability']}, next in {delta_h:.1f}h"

    if t == "fsrs_good_grows_stability":
        concept = (policy or {}).get("_fsrs_concept_good")
        if not concept:
            concept = {
                "knowledge": 0.7, "retrieval": 0.7, "application": 0.6,
                "explanation": 0.6, "transfer": 0.5,
                "misconceptions": [],
                "stability": 2.0, "difficulty": 4.0, "reps": 2, "lapses": 0,
            }
        ensure_fsrs_fields(concept)
        before = float(concept["stability"])
        schedule_fsrs(concept, 3)  # Good
        after = float(concept["stability"])
        ok = after > before
        return ok, f"Good: stability {before}→{after}"

    if t == "subject_alias_resolve":
        st = default_state()
        ensure_subject(st, "ai_agents", title="AI-agents")
        st["subjects"]["ai_agents"]["aliases"] = ["agents", "AI agents"]
        resolved = resolve_subject_id(st, "agents", title="agents")
        ok = resolved == "ai_agents"
        # second ensure with alias-like title should not create duplicate
        sid2 = ensure_subject(st, "agents", title="agents")
        ok2 = sid2 == "ai_agents" and len(st["subjects"]) == 1
        return ok and ok2, f"resolved={resolved} n_subjects={len(st['subjects'])}"

    if t == "pending_mc_promotion":
        st = default_state()
        ensure_subject(st, "zon", title="zon")
        ensure_subject(st, "sterrenkunde", title="Sterrenkunde")
        link_subjects(st, "sterrenkunde", "zon")
        for i, (sid, cid) in enumerate([
            ("zon", "kernsamenstelling"),
            ("zon", "nuclear_fusion"),
            ("sterrenkunde", "helium"),
        ]):
            ensure_concept(st, sid, cid)
            st["subjects"][sid]["concepts"][cid]["misconceptions"].append({
                "description": "He-4 heeft vier protonen in de kern",
                "severity": "high",
                "status": "open",
                "evidence": f"seen {i}",
            })
        update_pending_catalog(st)
        pending = st["meta"]["pending_catalog"]
        ok = len(pending) >= 1 and pending[0].get("count", 0) >= 3
        ok2 = st["subjects"]["sterrenkunde"].get("parent_id") == "zon"
        return ok and ok2, f"pending={len(pending)} count={pending[0].get('count') if pending else 0} parent_ok={ok2}"

    if t == "taxonomy_apply":
        st = default_state()
        ensure_subject(st, "zon", title="zon")
        ensure_subject(st, "neutronensterren", title="Neutronensterren")
        ensure_subject(st, "ai_agents", title="AI-agents")
        notes = apply_taxonomy_proposals(st, {
            "taxonomy": {
                "parent_id": "zon",
                "related_ids": ["ai_agents"],
                "add_aliases": ["neutron star", "NS"],
            }
        }, focus_subject_id="neutronensterren")
        child = st["subjects"]["neutronensterren"]
        ok_parent = child.get("parent_id") == "zon"
        ok_rel = "ai_agents" in (child.get("related") or [])
        ok_alias = "neutron star" in (child.get("aliases") or [])
        ok_no_delete = set(st["subjects"].keys()) >= {"zon", "neutronensterren", "ai_agents"}
        return all([ok_parent, ok_rel, ok_alias, ok_no_delete]), (
            f"notes={notes} parent={child.get('parent_id')} "
            f"rel={child.get('related')} aliases={child.get('aliases')}"
        )

    if t == "prereq_redirect":
        st = default_state()
        ensure_subject(st, "zon", title="zon")
        ensure_concept(st, "zon", "nuclear_fusion")
        ensure_concept(st, "zon", "hydrostatisch_evenwicht")
        # weak prereq
        nf = st["subjects"]["zon"]["concepts"]["nuclear_fusion"]
        for d in ("knowledge", "retrieval", "application", "explanation", "transfer"):
            nf[d] = 0.15
        he = st["subjects"]["zon"]["concepts"]["hydrostatisch_evenwicht"]
        for d in ("knowledge", "retrieval", "application", "explanation", "transfer"):
            he[d] = 0.55
        apply_prerequisite_proposals(
            st, "zon", "hydrostatisch_evenwicht",
            [{"id": "nuclear_fusion", "strength": "hard"}],
        )
        st["active_session"].update({
            "active": True,
            "subject_id": "zon",
            "current_concept_id": "hydrostatisch_evenwicht",
            "just_resumed": False,
        })
        policy = suggest_next_activity(st)
        ok = (
            policy.get("concept_id") == "nuclear_fusion"
            and policy.get("activity") == "RETRIEVAL"
            and policy.get("force") is True
        )
        return ok, f"policy={policy}"

    return False, f"unknown assert type: {t}"


# ------------------------------------------------------------
# Run one case
# ------------------------------------------------------------

def run_case(case: dict, base_prompt: str, skip_llm: bool = False) -> dict:
    cid = case["id"]
    mode = case.get("mode") or "teach"
    setup = case.get("setup") or {}
    report = {
        "id": cid,
        "description": case.get("description"),
        "passed": True,
        "checks": [],
        "skipped": False,
        "policy": None,
        "error": None,
    }

    state = build_state_from_setup(setup, mode)
    policy = suggest_next_activity(state)
    # Attach concept snapshot for FSRS asserts
    sid = setup.get("subject_id")
    cid = setup.get("concept_id")
    if sid and cid and sid in state.get("subjects", {}):
        concept = state["subjects"][sid]["concepts"].get(cid)
        if concept:
            policy = dict(policy)
            policy["_fsrs_concept"] = dict(concept)
            policy["_fsrs_concept_good"] = dict(concept)
            policy["_fsrs_concept_good"]["misconceptions"] = []
            policy["_fsrs_concept_good"]["stability"] = 2.0
    report["policy"] = policy

    # Policy expectation (optional)
    pe = case.get("policy_expect")
    if pe:
        if pe.get("activity") is not None and policy.get("activity") != pe.get("activity"):
            report["checks"].append({
                "type": "policy_expect.activity",
                "ok": False,
                "detail": f"expected {pe.get('activity')!r}, got {policy.get('activity')!r}",
            })
            report["passed"] = False
        if "force" in pe and bool(policy.get("force")) != bool(pe.get("force")):
            report["checks"].append({
                "type": "policy_expect.force",
                "ok": False,
                "detail": f"expected force={pe.get('force')}, got {policy.get('force')}",
            })
            report["passed"] = False

    result = None
    raw = ""

    if mode == "policy_only" or skip_llm:
        if mode != "policy_only" and skip_llm:
            report["skipped"] = True
            report["checks"].append({
                "type": "llm",
                "ok": True,
                "detail": "skipped (--policy / no LLM)",
            })
        for assertion in case.get("asserts") or []:
            if assertion.get("type") in ("policy_activity", "policy_force") or mode == "policy_only":
                ok, detail = run_assert(assertion, result, raw, policy)
                report["checks"].append({"type": assertion.get("type"), "ok": ok, "detail": detail})
                if not ok:
                    report["passed"] = False
            elif skip_llm:
                continue
            else:
                ok, detail = run_assert(assertion, result, raw, policy)
                report["checks"].append({"type": assertion.get("type"), "ok": ok, "detail": detail})
                if not ok:
                    report["passed"] = False
        return report

    # LLM turn
    user_message = case.get("user_message") or ""
    conversation = [{"role": "user", "content": user_message}]
    try:
        messages = build_messages(base_prompt, state, conversation, policy=policy)
        raw, result = ask_model_with_json_retry(messages)
    except Exception as e:
        report["passed"] = False
        report["error"] = str(e)
        report["checks"].append({"type": "llm_call", "ok": False, "detail": str(e)})
        return report

    for assertion in case.get("asserts") or []:
        ok, detail = run_assert(assertion, result, raw or "", policy)
        report["checks"].append({"type": assertion.get("type"), "ok": ok, "detail": detail})
        if not ok:
            report["passed"] = False

    return report


def load_cases(filter_substr: str | None = None) -> list[dict]:
    cases = []
    for path in sorted(CASES_DIR.glob("*.json")):
        with open(path, encoding="utf-8") as f:
            case = json.load(f)
        if filter_substr and filter_substr not in case.get("id", "") and filter_substr not in path.name:
            continue
        cases.append(case)
    return cases


def main():
    parser = argparse.ArgumentParser(description="Socratic Tutor eval harness")
    parser.add_argument("--case", help="Substring filter on case id / filename")
    parser.add_argument("--policy", action="store_true", help="Only run policy_only cases (no Ollama)")
    parser.add_argument("--list", action="store_true", help="List cases and exit")
    parser.add_argument("--json-out", type=str, default="", help="Write full report JSON to path")
    args = parser.parse_args()

    cases = load_cases(args.case)
    if not cases:
        print("No cases found.")
        sys.exit(1)

    if args.list:
        for c in cases:
            print(f"  {c['id']:28}  {c.get('mode',''):12}  {c.get('description','')}")
        sys.exit(0)

    if args.policy:
        cases = [c for c in cases if c.get("mode") == "policy_only"]
        if not cases:
            print("No policy_only cases matched.")
            sys.exit(1)

    base_prompt = load_system_prompt()
    results = []
    failed = 0

    print("=" * 60)
    print("SOCRATIC TUTOR EVALS")
    print("=" * 60)

    for case in cases:
        print(f"\n▶ {case['id']}")
        print(f"  {case.get('description', '')}")
        report = run_case(case, base_prompt, skip_llm=False if case.get("mode") != "policy_only" else False)
        # When --policy, only policy cases already filtered; for mixed run policy_only skips LLM inside
        results.append(report)

        status = "PASS" if report["passed"] else "FAIL"
        if report.get("error"):
            status = "ERROR"
        print(f"  → {status}")
        if report.get("policy"):
            p = report["policy"]
            print(f"  policy: activity={p.get('activity')} force={p.get('force')} | {p.get('reason')}")
        for ch in report["checks"]:
            mark = "✓" if ch["ok"] else "✗"
            print(f"    {mark} {ch['type']}: {ch['detail']}")
        if report.get("error"):
            print(f"    ! {report['error']}")
        if not report["passed"]:
            failed += 1

    print("\n" + "=" * 60)
    total = len(results)
    print(f"Results: {total - failed}/{total} passed")
    print("=" * 60)

    if args.json_out:
        out = Path(args.json_out)
        out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Wrote {out}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
