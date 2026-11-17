#!/usr/bin/env python3
"""
Chaos / invariant fuzzing of core.handle_turn. NO GPU, NO Ollama: the model is a deterministic-by-seed chaos stub that fails the
ways the real one does (truncated JSON, invalid verdicts/intents, empty or repeated drafts, exceptions, malformed assessment/session).

    python tools/fuzz_turns.py --seeds 500 --turns 8        # reports violations with the seed that reproduces them
    python tools/fuzz_turns.py --seed 123 --turns 8 -v      # replay one seed, print every turn

Invariants checked after EVERY turn (a violation prints the seed, the turn and a short reproduction):
  I1  handle_turn never raises
  I2  result is a dict with a str `response`; a teaching turn that is ok has a non-empty response
  I3  the state stays strictly JSON-serialisable (it is written to disk)
  I4  active_session.correct_streak is an int >= 0
  I5  conversation bookkeeping: ok turn -> +2 messages (user, assistant); failed turn -> +0, +1 (user line kept on purpose) or +2 (graceful reply)
  I6  hold-and-flush: the UI callback never receives a discarded draft
  I7  concede turns (intent challenges_tutor) are never rewritten by a repair
  I8  a misconception appears only after a confirmed error (judge incorrect / evaluator incorrect / verified key)
  I9  FSRS: at most one grade per concept per SESSION (a new session may grade again)
  I10 a turn completes in < 2 s (no hidden loops)
  I11 the learner id never changes
  I12 never a byte-identical reply to the previous one unless a last resort is logged
  I13 (with --echo) the reply never repeats the learner's own message unless a last resort is logged
  I14 save=False never writes to disk
"""
from __future__ import annotations

import argparse
import logging
logging.disable(logging.CRITICAL)
import copy
import json
import os
import random
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("METRICS_ENABLED", "0")
os.environ.setdefault("TUTOR_EVENT_LOG", "0")
os.environ.setdefault("TUTOR_EVIDENCE", "0")
os.environ.setdefault("TUTOR_EVIDENCE_WIKI", "0")

def G_tokens(s):
    import guards
    return guards.tokens(s)


INTENTS = ["answer", "asks_hint", "asks_reveal", "asks_clarification", "side_question", "insight", "challenges_tutor",
           "frustration", "off_topic"]
CORR = ["correct", "partial", "incorrect", "unknown"]
LONG = "Dit is een lange tekst. " * 150
DRAFTS = [
    "Klopt. Wat gebeurt er dan met de afstand tussen de kernen?",
    "Goed. Nu iets anders: wat denk je dat er gebeurt als de waarde verdubbelt? En waarom?",
    "Je hebt gelijk.", "", "   ", "Een tekst zonder vraag.", LONG + "Wat denk je?",
    "Vergelijk dit t.o.v. de andere, bijv. een kleinere. Wat is groter?",
    "Je weet dat meer lading meer afstoting geeft. Kun je nu in één zin uitleggen waarom meer lading meer afstoting geeft?",
    "Dezelfde vraag altijd opnieuw: welke keten wint dan, en waarom?",
    'Klopt {"response": "x"} ``` Wat nu?', "Klopt 😀 — «vraag»: wat denk jij? ¿Y tú?",
]
FIXED_REPEAT = "Welke van de twee levert meer energie, en waarom?"


def chaos_llm(rng, rec, echo=False):
    """Returns an llm_call replacement. `rec` collects what happened (roles, drafts) for the invariant checks."""
    def result(parsed, stats=None, raw=None):
        return types.SimpleNamespace(raw=raw if raw is not None else json.dumps(parsed, ensure_ascii=False),
                                     parsed=parsed, stats=stats or {})

    def maybe_fail(p):
        if rng.random() < p:
            raise RuntimeError("chaos: model call failed")

    def draft():
        return FIXED_REPEAT if rng.random() < 0.25 else rng.choice(DRAFTS)

    def llm_call(role, messages, **opts):
        rec["roles"].append(role)
        if role == "classify":
            maybe_fail(0.08)
            r = rng.random()
            if r < 0.10:
                return result({"intent": "nonsense"})
            if r < 0.15:
                return result(None)
            d = {"intent": rng.choice(INTENTS), "correctness": rng.choice(CORR + ["???"])}
            rec["classified"] = d["intent"]
            return result(d)
        if role == "judge":
            maybe_fail(0.08)
            r = rng.random()
            if r < 0.18:
                return result(None, {"done_reason": "length"}, raw="")
            if r < 0.28:
                return result({"verdict": "maybe"})
            return result({"analysis": rng.choice(["", "a", LONG[:300]]), "verdict": rng.choice(["correct", "partial", "incorrect", "uncertain"]),
                            "reason": rng.choice(["", "r", None, 7]), "missing": rng.choice([None, "x", "null", 3])})
        if role in ("evaluate",):
            maybe_fail(0.1)
            return result({"verdict": rng.choice(["correct", "incorrect", "uncertain", "weird"]), "reason": "r"})
        if role in ("repair", "reframe"):
            maybe_fail(0.15)
            r = rng.random()
            if r < 0.12:
                return result({"response": ""})
            if r < 0.2:
                return result(None)
            t = rng.choice(DRAFTS + ["Nieuwe vraag: wat verandert er aan het contactpunt?"])
            return result({"response": t, "assessment": rng.choice([{}, None]), "session": rng.choice([{}, None])})
        # ---- main
        maybe_fail(0.05)
        cb = opts.get("on_response_delta")
        r = rng.random()
        if r < 0.06:
            return result(None, raw="not json at all")
        text = draft()
        if echo and rng.random() < 0.15:           # the model sends the learner's own message back (stateless EMS run, critical/eco)
            lm = next((m.get("content") for m in reversed(messages) if m.get("role") == "user"), "")
            if len(G_tokens(lm)) >= 4:
                text = lm
        rec["last_main_draft"] = text
        if cb and text:
            third = max(1, len(text) // 3)
            for i in range(0, len(text), third):
                cb(text[: i + third], text[i: i + third])
        a_verdict = rng.choice(["correct", "partial", "incorrect", None, "weird", "ok", "wrong"])
        assessment = rng.choice([
            {"subject": "kernfusie", "concept": "kernfusie", "activity": rng.choice(["EXPLANATION", "TRANSFER", "NOPE", None]),
             "verdict": a_verdict, "learner_signal": rng.choice(CORR + ["stuck", None]),
             "misconceptions": rng.choice([None, [], [{"description": "m", "severity": "high", "status": "open"}], "oops", [{"x": 1}]]),
             "gap_card": rng.choice([None, {"key": "k", "expected_answer": "e", "status": rng.choice(["model_only", "disputed", None])}, "bad"]),
             "dimensions": rng.choice([None, {"knowledge": rng.random()}, {"knowledge": "x"}])},
            None, {}, "garbage"])
        session = rng.choice([{"last_question": rng.choice([None, "Wat nu?", "", 5]), "premises": rng.choice([None, ["p1", "p2"], "p", [1, 2]])},
                              None, {}, [], "x"])
        routing = {"action": rng.choices(["continue", "switch", "new_subject", "list_subjects", "clarify", "bogus", None],
                                         [80, 3, 3, 3, 4, 3, 4])[0], "subject_id": rng.choice([None, "kernfusie", "zzz"])}
        return result({"routing": routing, "response": text, "assessment": assessment, "session": session})
    return llm_call


def random_state(rng):
    from conftest import make_state
    st = make_state(started_at="2026-11-04T10:00:00")
    a = st["active_session"]
    a["correct_streak"] = rng.choice([0, 0, 1, 3, 4, 5])
    a["must_advance"] = rng.random() < 0.3
    a["repeat_streak"] = rng.choice([0, 1, 2, 3])
    a["same_activity_streak"] = rng.choice([0, 1, 2, 3, 4])
    if rng.random() < 0.6:
        a["open_threads"][0]["premises"] = rng.choice([["p1"], ["p1", "p2", "p3"]])
    if rng.random() < 0.5:
        import tutor as T
        T.ensure_concept(st, "kernfusie", "tweede")
        st["subjects"]["kernfusie"]["concepts"]["tweede"].update({"title": "Tweede", "knowledge": rng.random(), "retrieval": rng.random(),
                                                                  "application": rng.random(), "explanation": rng.random(), "transfer": rng.random()})
    if rng.random() < 0.3:
        a["established_points"] = ["Learner showed: a", "Established: b", "Tutor-asserted (unverified): c", "Answered: d"]
    return st


MESSAGES = ["geen idee", "dat weet ik niet", "ik denk van wel", "Hint please", "vertel mij", "Nee", "Omg dat zei ik al, je draait in rondjes",
            "aaah wacht, dus als de temperatuur stijgt dan...", "wat is een Coulomb-barrière?", "klopt dit wel?", "", "   ", "x" * 5000,
            "dat hebben we al behandeld", "/state", "/tree", "/help", "ik wil nu over iets anders leren", "😀", "{\"a\": 1}",
            "Ja want de dichtheid geeft meer botsingen, dus de verhouding blijft gelijk."]


def open_mc_count(st):
    n = 0
    for s in (st.get("subjects") or {}).values():
        for c in (s.get("concepts") or {}).values():
            n += sum(1 for m in (c.get("misconceptions") or []) if isinstance(m, dict) and m.get("status") == "open")
    return n


def fsrs_counts(st):
    return {(sid, cid): int(c.get("review_count") or 0) for sid, s in (st.get("subjects") or {}).items()
            for cid, c in (s.get("concepts") or {}).items()}


def run_sequence(seed, n_turns, verbose=False, echo=False):
    import tempfile
    import auth
    import core
    tmpdir = Path(tempfile.mkdtemp(prefix="fuzz_data_"))
    real_data_dir = getattr(auth, "DATA_DIR", None)
    auth.DATA_DIR = tmpdir                      # any write lands here, never in the real data/
    try:
        return _run_sequence(seed, n_turns, verbose, tmpdir, echo)
    finally:
        auth.DATA_DIR = real_data_dir
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def _run_sequence(seed, n_turns, verbose, tmpdir, echo=False):
    import core
    rng = random.Random(seed)
    rec = {"roles": []}
    st = random_state(rng)
    conv = []
    lid = st["learner"]["id"]
    viol = []
    llm = chaos_llm(rng, rec, echo)
    orig = getattr(core, "llm_call", None)
    core.llm_call = llm
    fs0 = fsrs_counts(st)
    sessions = {st["active_session"].get("started_at")}
    try:
        for turn in range(n_turns):
            msg = rng.choice(MESSAGES)
            rec["roles"].clear(); rec["classified"] = None; rec["last_main_draft"] = None
            seen = []
            mc_before, conv_before = open_mc_count(st), len(conv)
            t0 = time.time()
            try:
                res = core.handle_turn(msg, state=st, conversation=conv, save=False, stream=True,
                                       on_response_delta=lambda p, d: seen.append(p), user_id=lid)
            except Exception as e:                                                   # I1
                viol.append((seed, turn, "I1 raised", f"{type(e).__name__}: {e}", msg[:40]))
                break
            dt = time.time() - t0
            if dt > 2.0:
                viol.append((seed, turn, "I10 slow", f"{dt:.1f}s", msg[:40]))
            if not isinstance(res, dict) or not isinstance(res.get("response"), str):          # I2
                viol.append((seed, turn, "I2 bad result", repr(res)[:80], msg[:40]))
                continue
            kind, ok = res.get("kind"), res.get("ok", True)
            teaching = kind in ("message", None) and ok
            if teaching and not res["response"].strip() and msg.strip() and not msg.startswith("/"):
                viol.append((seed, turn, "I2 empty response on an ok teaching turn", f"stats={res.get('stats')}", msg[:40]))
            try:
                json.dumps(st)                                                            # I3
            except Exception as e:
                viol.append((seed, turn, "I3 state not serialisable", str(e)[:80], msg[:40]))
            cs = st["active_session"].get("correct_streak")
            if cs is not None and (not isinstance(cs, int) or cs < 0):                     # I4 (absent = 0, e.g. after a new session)
                viol.append((seed, turn, "I4 correct_streak", repr(cs), msg[:40]))
            if teaching and kind == "message":                                              # I5
                if len(conv) != conv_before + 2 or conv[-1].get("role") != "assistant" or conv[-2].get("role") != "user":
                    viol.append((seed, turn, "I5 conversation bookkeeping", f"{conv_before}->{len(conv)}", msg[:40]))
            elif not ok:
                # failed turns have three legitimate shapes: +0; +1 (the user's line is kept ON PURPOSE, visible after reload);
                # +2 (graceful 'could not finish that answer' reply, user + assistant). Anything else is a bug.
                d = len(conv) - conv_before
                if d not in (0, 1, 2) or (d == 1 and conv[-1].get("role") != "user") or (d == 2 and conv[-1].get("role") != "assistant"):
                    viol.append((seed, turn, "I5 failed turn changed the conversation unexpectedly", f"{conv_before}->{len(conv)}", msg[:40]))
            stats = res.get("stats") or {}
            draft = stats.get("draft")
            if draft and draft != res["response"] and any(draft in s for s in seen):          # I6
                viol.append((seed, turn, "I6 discarded draft reached the UI", draft[:50], msg[:40]))
            if st["active_session"].get("turn_intent") == "challenges_tutor" and "repair" in rec["roles"] and stats.get("gate_repair") != "concede_duplicate" and not stats.get("concede_repair_failed") and not stats.get("empty_fallback"):   # I7
                viol.append((seed, turn, "I7 repair ran on a concede turn", str(rec["roles"]), msg[:40]))
            jv = (stats.get("judge") or {}).get("verdict") if isinstance(stats.get("judge"), dict) else None
            if open_mc_count(st) > mc_before and jv != "incorrect" and stats.get("correction_gate") != "incorrect":   # I8
                viol.append((seed, turn, "I8 misconception without a confirmed error", f"judge={jv} gate={stats.get('correction_gate')}", msg[:40]))
            if teaching and kind == "message":                                               # I12 / I13
                prev = [m for m in conv[:-2] if m.get("role") == "assistant"]
                lastv = prev[-1]["content"] if prev else None
                last_resort = any(stats.get(k) for k in ("gate_repair_failed", "reframe_failed", "concede_repair_failed", "gate_skipped", "reframe_repeat", "empty_fallback"))
                if lastv and res["response"].strip() and res["response"].strip() == str(lastv).strip() and not last_resort:
                    viol.append((seed, turn, "I12 byte-identical reply to the previous one without a logged last resort", res["response"][:50], msg[:40]))
            sessions.add((st.get("active_session") or {}).get("started_at"))
            if echo and teaching and kind == "message" and res["response"].strip():             # I13 (only when echo drafts are on)
                ta, tb = G_tokens(res["response"]), G_tokens(msg)
                last_resort = any(stats.get(k) for k in ("gate_repair_failed", "reframe_failed", "concede_repair_failed", "gate_skipped", "reframe_repeat", "empty_fallback"))
                if len(tb) >= 4 and len(ta) >= 4 and len(ta & tb) / len(ta | tb) >= 0.8 and not last_resort:
                    viol.append((seed, turn, "I13 the reply repeats the learner's own message", res["response"][:50], msg[:40]))
            if st["learner"]["id"] != lid:                                                  # I11
                viol.append((seed, turn, "I11 learner id changed", st["learner"]["id"], msg[:40]))
            if verbose:
                print(f"seed {seed} t{turn} roles={rec['roles']} kind={kind} ok={ok} intent={rec['classified']} "
                      f"stats={ {k: v for k, v in stats.items() if k in ('gate_repair','gate_repair_failed','reframe','reframe_failed','judge_failed','judge_truncated','echo_flagged','echo_skipped','gate_skipped')} } "
                      f"resp={res['response'][:50]!r}")
        written = [str(f.relative_to(tmpdir)) for f in tmpdir.rglob("*") if f.is_file()]
        if written:                                                                          # I14
            viol.append((seed, n_turns, "I14 save=False wrote to disk", ", ".join(written[:3]), ""))
        fs1 = fsrs_counts(st)
        for k, v in fs1.items():                                                            # I9
            if v - fs0.get(k, 0) > max(1, len(sessions)):
                viol.append((seed, n_turns, "I9 FSRS graded more than once per session", f"{k}: {fs0.get(k,0)}->{v}", ""))
    finally:
        if orig is not None:
            core.llm_call = orig
    return viol


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, default=300)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--turns", type=int, default=8)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--echo", action="store_true", help="also inject learner-echo drafts and check invariant I13")
    a = ap.parse_args(argv)
    seeds = [a.seed] if a.seed is not None else range(a.seeds)
    allv = []
    t0 = time.time()
    for s in seeds:
        allv += run_sequence(s, a.turns, a.verbose, a.echo)
    kinds = {}
    for v in allv:
        kinds.setdefault(v[2], []).append(v)
    n = len(list(seeds)) * a.turns
    print(f"{len(list(seeds))} sequences x {a.turns} turns = {n} turns in {time.time()-t0:.1f}s ; violations: {len(allv)}")
    for k, vs in sorted(kinds.items()):
        print(f"\n[{k}]  x{len(vs)}   e.g. seed={vs[0][0]} turn={vs[0][1]}: {vs[0][3]}   (message: {vs[0][4]!r})")
    return 1 if allv else 0


if __name__ == "__main__":
    sys.exit(main())
