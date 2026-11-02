#!/usr/bin/env python3
"""
LIVE check of the judge against the REAL model (run on the GPU box; ~1 minute).

Stubs prove the wiring, not the model. This checks whether the 27B, with think="low" and a 500-token cap,
returns the right verdict on cases where the truth is known:

  python tools/live_judge_check.py                  # all cases, BOTH judge styles side by side
  python tools/live_judge_check.py --style cot_json # one style (cot_json = reasoning inside the JSON, think off)
  python tools/live_judge_check.py --case density   # one case
  python tools/live_judge_check.py --host http://localhost:11434

Why two styles: on 2026-10-26 the style "think" (think=low, 500 tokens) failed on the two HARD cases (density,
valid challenge): the model thought for the whole budget (11 s), returned no JSON. This tool shows which style works on YOUR model.

Cases: your human-checked density case (a CORRECT answer the tutor wrongly challenged for 10 turns), plus
domain-free controls with an unambiguous answer (right / wrong / partial / valid challenge / invalid challenge).
Exit code 1 when a decisive verdict is wrong ("uncertain" is reported as WARN: safe, but costs a follow-up question).
Reports verdict, reason, seconds per call and JSON failures. Writes nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = ROOT / "tests" / "fixtures" / "density_case.json"

ARITH = ["A is 3 meer dan B", "B is gelijk aan 4"]


def load_cases():
    d = json.loads(FIX.read_text(encoding="utf-8")) if FIX.exists() else None
    cases = []
    if d:
        cases.append({"id": "density", "kind": "answer", "premises": d["premises"], "question": d["tutor_question"],
                      "answer": d["learner_answer"], "expect": d["correct_verdict"],
                      "note": "HUMAN-CHECKED: production called this 'partial' and pressed for 10 turns"})
    cases += [
        {"id": "arith-right", "kind": "answer", "premises": ARITH, "question": "Wat is A?", "answer": "A is 7", "expect": "correct"},
        {"id": "arith-wrong", "kind": "answer", "premises": ARITH, "question": "Wat is A?", "answer": "A is 12", "expect": "incorrect"},
        {"id": "arith-partial", "kind": "answer", "premises": ARITH, "question": "Wat zijn A en B?", "answer": "B is 4", "expect": "partial"},
        {"id": "challenge-valid", "kind": "challenge", "premises": ["De temperatuur is constant"],
         "question": "Waarom zou proces X dan nog steeds sneller gaan? De echte reden is de temperatuurafhankelijkheid.",
         "answer": "Maar je zei zelf dat de temperatuur constant was, dus die kan het verschil niet verklaren.", "expect": "correct"},
        {"id": "challenge-invalid", "kind": "challenge", "premises": ARITH, "question": "A is dus 7, want 4 + 3.",
         "answer": "Nee, A is 12, je rekent fout.", "expect": "incorrect"},
    ]
    return cases


def grade(expect: str, got: str | None) -> str:
    """OK | WARN (uncertain: safe but slow) | FAIL (wrong decisive verdict) | NOJSON"""
    if got is None:
        return "NOJSON"
    if got == expect:
        return "OK"
    if got == "uncertain":
        return "WARN"
    return "FAIL"


def run_case(case, core, T):
    st = T.default_state()
    st["learner"]["id"] = "livecheck"
    T.ensure_subject(st, "demo", "demo")
    T.ensure_concept(st, "demo", "demo")
    a = st["active_session"]
    a.update({"active": True, "subject_id": "demo", "current_concept_id": "demo", "current_activity": "EXPLANATION",
              "open_threads": [{"id": "t1", "q": case["question"], "status": "open", "tries": 1, "activity": "EXPLANATION",
                                "premises": case["premises"]}],
              "turn_intent": "challenges_tutor" if case["kind"] == "challenge" else "answer", "turn_correctness": "unknown"})
    conv = [{"role": "assistant", "content": case["question"]}]
    t0 = time.time()
    j = core._judge_turn(st, conv, case["answer"])
    meta = a.get("_judge_meta", {})
    return (j or {}), round(time.time() - t0, 1), bool(meta.get("failed")), bool(meta.get("truncated"))


def styles_from_arg(arg: str, default_style: str) -> list[str]:
    arg = (arg or "both").strip().lower()
    if arg == "both":
        return ["cot_json", "think"]
    if arg == "default":
        return [default_style]
    if arg in ("cot_json", "think"):
        return [arg]
    raise SystemExit(f"unknown --style {arg!r} (both | default | cot_json | think)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case")
    ap.add_argument("--host", default=None)
    ap.add_argument("--style", default="both", help="both | default | cot_json | think")
    a = ap.parse_args(argv)
    import llm
    import core
    import tutor as T
    if a.host:
        llm.OLLAMA_HOST = a.host
        llm.MODEL_URL = a.host.rstrip("/") + "/api/chat"
    default_style = core.JUDGE_STYLE
    summary = {}
    for style in styles_from_arg(a.style, default_style):
        core.JUDGE_STYLE = style
        print(f"\n=== style: {style} ===")
        print(f"{'case':18s} {'expect':10s} {'got':10s} {'grade':7s} {'sec':>5s}  reason")
        bad = warn = 0
        secs = []
        for c in load_cases():
            if a.case and a.case != c["id"]:
                continue
            j, sec, failed, trunc = run_case(c, core, T)
            g = grade(c["expect"], j.get("verdict"))
            bad += g in ("FAIL", "NOJSON")
            warn += g == "WARN"
            secs.append(sec)
            print(f"{c['id']:18s} {c['expect']:10s} {str(j.get('verdict')):10s} {g:7s} {sec:5.1f}  {str(j.get('reason') or '')[:60]}"
                  + ("   [TRUNCATED: token budget ran out]" if trunc else ("   [JSON failed]" if failed else ""))
                  + (f"\n{'':18s} note: {c['note']}" if c.get("note") else ""))
        summary[style] = (bad, warn, round(sum(secs) / max(len(secs), 1), 1))
    core.JUDGE_STYLE = default_style
    print("\nsummary (FAIL/NOJSON, WARN, mean sec/case):  " + "   ".join(f"{s}: {v[0]}, {v[1]}, {v[2]}s" for s, v in summary.items()))
    print(f"tutor default style = {default_style!r}  (change with env TUTOR_JUDGE_STYLE)")
    print("Reading: pick the style with 0 FAIL/NOJSON, including 'density' (a FAIL there = the judge repeats the tutor's mistake).")
    bad_default = summary.get(default_style, (0,))[0]
    return 1 if bad_default else 0


if __name__ == "__main__":
    sys.exit(main())
