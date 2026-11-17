#!/usr/bin/env python3
"""
LIVE check of the judge against the REAL model (run on the GPU box; ~1 minute).

Stubs prove the wiring, not the model. This checks whether the 27B, with think="low" and a 500-token cap,
returns the right verdict on cases where the truth is known:

  python tools/live_judge_check.py                  # all cases, BOTH judge styles side by side
  python tools/live_judge_check.py --style cot_json # one style (cot_json = reasoning inside the JSON, think off)
  python tools/live_judge_check.py --case density-absolute -v   # one case, with the full reasoning
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

# VERBATIM sentences of the owner's ems.md (the judge must quote the decisive words; a quote that is not in these lines is not accepted)
EMS = {
    "intro": "EV Boost zonder import ontlaadt de packs tot de ingestelde rest.",
    "night": "Boost 's avonds en 's nachts blijft de packs leegtrekken, zolang de knop aan staat en de kabel erin zit.",
    "six": "Onder 6 A blijft een stilstaande auto uit.",
    "running": "Een lopende auto stopt als de bekende zon onder 300 W zit, en een zelfstart stopt ook tussen 300 W en 6 A.",
    "ecostart": "Een stilstaande auto start pas als de zon minstens 1580 W is, de P1 vers is en er geen import staat, ook als de som ver boven de reserve zit.",
}
Q_BOOST = "Wanneer mag de auto laden zonder zon?"
Q_RUN = "Als je een lopende auto hebt en de bekende zon daalt naar 250 W, wat verwacht je dan dat het EMS doet?"


def load_cases():
    d = json.loads(FIX.read_text(encoding="utf-8")) if FIX.exists() else None
    cases = []
    if d:
        cases.append({"id": "density-ambiguous", "kind": "answer", "premises": d["premises"], "question": d["tutor_question"],
                      "answer": d["learner_answer"], "expect": ("correct", "uncertain"), "ambiguous": True,
                      "note": "the tutor's premise is ambiguous (fraction vs absolute CNO); correct or uncertain are both fine, "
                              "the two variants below decide"})
        for key, v in (d.get("variants") or {}).items():
            cases.append({"id": f"density-{key}", "kind": "answer", "premises": v["premises"], "question": d["tutor_question"],
                          "answer": d["learner_answer"], "expect": v["expect"],
                          "note": (v.get("why") or v.get("note") or key) + "  (my derivation: confirm)"})
    cases += [
        # --- the owner's EMS cases (stateful run reg_stateful_001: 4 of 27 true answers were judged incorrect, 1 of 6 misconceptions correct)
        {"id": "ems-misconception", "kind": "answer", "premises": [], "question": Q_BOOST,
         "answer": "EV Boost kan alleen als er zonneproductie is", "evidence": [EMS["intro"], EMS["night"]], "expect": "incorrect",
         "note": "FALSE per the source (Boost also runs evening and night). 'correct' here = a false accept"},
        {"id": "ems-true-boost", "kind": "answer", "premises": [], "question": Q_BOOST,
         "answer": "Bij EV Boost mag de auto laden zonder zon zolang de packs boven de ingestelde reserve zitten.",
         "evidence": [EMS["intro"], EMS["night"]], "expect": ("correct", "uncertain"),
         "note": "TRUE per the source; 'incorrect' = a false reject (it happened 2x in the stateful run)"},
        {"id": "ems-offtopic-true", "kind": "answer", "premises": [], "question": Q_RUN,
         "answer": "Een stilstaande auto start niet onder 6 A, ook als de som net boven de reserve zit.",
         "evidence": [EMS["six"], EMS["running"]], "expect": "off_target",
         "note": "true, but it does not answer the 250 W question"},
        {"id": "ems-true-responsive", "kind": "answer", "premises": [], "question": Q_RUN,
         "answer": "Dan stopt de auto, want de bekende zon zit onder 300 W.", "evidence": [EMS["running"]], "expect": "correct",
         "note": "positive control: true AND answers the question, the quote exists"},
        {"id": "ems-valid-challenge", "kind": "challenge", "premises": [],
         "question": "Je koppelt EV Boost aan de 6A-startdrempel. Als de zon precies op die drempel staat en de auto staat stil, mag de auto dan laden of niet?",
         "answer": "Dit is EV Boost, geen Eco: er is geen 6A-startdrempel op zon op dezelfde manier.",
         "evidence": [EMS["ecostart"], EMS["intro"]], "expect": ("correct", "uncertain"),
         "note": "the learner separates Boost from Eco; 'incorrect' = rejecting a valid challenge (2x in the stateful run)"},
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


def grade(expect, got, ambiguous: bool = False) -> str:
    """OK | WARN (uncertain: safe but slow) | AMBIG (premise-dependent, not a failure) | FAIL | NOJSON.
    `expect` may be a verdict or a tuple of acceptable verdicts."""
    if got is None:
        return "NOJSON"
    ok = tuple(expect) if isinstance(expect, (tuple, list)) else (expect,)
    if got in ok:
        return "OK"
    if got == "uncertain":
        return "WARN"
    return "AMBIG" if ambiguous else "FAIL"


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
    ev = {"snippets": list(case.get("evidence") or []), "enabled": bool(case.get("evidence"))}
    j = core._judge_turn(st, conv, case["answer"], evidence=ev)
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
    ap.add_argument("--verbose", "-v", action="store_true", help="print the full analysis and reason of every case")
    a = ap.parse_args(argv)
    import llm
    import core
    import tutor as T
    if a.host:
        llm.OLLAMA_HOST = a.host
        llm.MODEL_URL = a.host.rstrip("/") + "/api/chat"
    default_style = core.JUDGE_STYLE
    summary = {}
    t0 = time.time()
    try:                                   # load the model first: the first call otherwise includes ~20 s of model load
        llm.ask_model([{"role": "user", "content": "ok"}], stream=False, think=False, num_predict=4)
        print(f"warm-up (model load + first call): {time.time() - t0:.1f}s  -> the timings below are warm")
    except Exception as e:
        print(f"warm-up failed ({e}); the first timing may include model load")
    for style in styles_from_arg(a.style, default_style):
        core.JUDGE_STYLE = style
        print(f"\n=== style: {style} ===")
        print(f"{'case':18s} {'expect':17s} {'got':10s} {'grade':7s} {'sec':>5s}  reason")
        bad = warn = 0
        secs = []
        for c in load_cases():
            if a.case and a.case != c["id"]:
                continue
            j, sec, failed, trunc = run_case(c, core, T)
            g = grade(c["expect"], j.get("verdict"), bool(c.get("ambiguous")))
            bad += g in ("FAIL", "NOJSON")
            warn += g == "WARN"
            secs.append(sec)
            exp = "/".join(c["expect"]) if isinstance(c["expect"], (tuple, list)) else c["expect"]
            print(f"{c['id']:18s} {exp:17s} {str(j.get('verdict')):10s} {g:7s} {sec:5.1f}  {str(j.get('reason') or '')[:60]}"
                  + ("   [TRUNCATED: token budget ran out]" if trunc else ("   [JSON failed]" if failed else ""))
                  + (("   [quote OK]" if j.get("support_verified") else ("   [quote NOT FOUND -> downgraded to uncertain]" if j.get("downgraded") else "")) if j else "")
                  + (f"\n{'':18s} note: {c['note']}" if c.get("note") else ""))
            if a.verbose:
                print(f"{'':18s} analysis: {j.get('analysis') or '-'}\n{'':18s} support:  {j.get('support') or '-'}\n{'':18s} reason:   {j.get('reason') or '-'}")
        summary[style] = (bad, warn, round(sum(secs) / max(len(secs), 1), 1))
    core.JUDGE_STYLE = default_style
    print("\nsummary (FAIL/NOJSON, WARN, mean sec/case):  " + "   ".join(f"{s}: {v[0]}, {v[1]}, {v[2]}s" for s, v in summary.items()))
    print(f"tutor default style = {default_style!r}  (change with env TUTOR_JUDGE_STYLE)")
    print("Reading: pick the style with 0 FAIL/NOJSON. density-fraction=correct AND density-absolute=incorrect means the judge reasons about the "
          "premises; the same verdict on both means it ignores them. AMBIG on density-ambiguous is fine (the premise is ambiguous).")
    bad_default = summary.get(default_style, (0,))[0]
    return 1 if bad_default else 0


if __name__ == "__main__":
    sys.exit(main())
