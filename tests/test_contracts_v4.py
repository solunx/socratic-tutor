"""
CONTRACT TESTS v4 — what the live run on gpu01 (2026-10-26) showed.

  1. The judge failed on the HARD cases: think="low" + a 500-token cap -> the model thought for the whole budget (11 s = 500 tokens
     at 59 tok/s), no JSON, llm.py passed the thinking prose to the JSON parser. 4 of 6 judge calls failed in a real session,
     exactly on the contested answers, and each wasted ~11 s.  => reasoning INSIDE the JSON (field `analysis` first), think off,
     fixed length; and truncation must be visible (`done_reason`).
  2. The echo-repair heuristic replaced a BETTER draft (it contained the correction "in the pp-chain the later steps are a lighter
     nucleus plus a proton, step 1 is p+p") with one that invented a false premise and approved a wrong claim. Its recall was 2/10.
     => switchable; `stats.echo_flagged` is logged even when it is off, so its hit rate can be audited.
  3. A rotation turn replaced the feedback on the learner's last claim by a bridge sentence ("the CNO cycle depends on heavier nuclei")
     so "it doubles" was never confirmed.  => feedback first, then bridge.

Contract:
  core.JUDGE_STYLE in {"cot_json" (default; think off, max_tokens <= 500, JSON keys in order analysis, verdict, reason, missing),
                       "think" (think on, max_tokens >= 1000)}; env TUTOR_JUDGE_STYLE.
  core.ECHO_REPAIR (env TUTOR_ECHO_REPAIR=0 disables); stats.echo_flagged / echo_skipped=="disabled".
  llm stats carry `done_reason` (stream and non-stream); stats.judge_truncated / judge_failed / judge_style; events: judge_analysis,
  judge_truncated, judge_style, echo_flagged.  policy ROTATE reason: "FIRST give clear feedback on their LAST answer".
"""
import importlib.util
import json
from pathlib import Path

import pytest

import core
import debug_events
import policy as P
import tutor as T
from conftest import ROOT, main_result, make_state

TODO = lambda what: (lambda f: f)

STARTED = "2026-10-26T14:01:22"
PREM = ["Twee sterren hebben dezelfde temperatuur", "Dezelfde CNO-concentratie", "Verschillende protonendichtheid"]
ECHO_DRAFT = ("Geen probleem, dat is een lastig punt. De afstoting hangt af van de ladingen van de kernen. Hoe meer protonen "
              "in een kern, hoe groter de positieve lading. Een kern met meer protonen stoot een naderend proton dus "
              "sterker af. Kun je nu in één zin uitleggen waarom een kern met meer protonen een sterkere afstoting heeft?")
REPAIR_TEXT = "Prima. Kijk eens naar de stap die daarna komt: wat verandert er aan het contactpunt?"


def st_with(premises=PREM, **act):
    st = make_state(started_at=STARTED, **act)
    if premises:
        st["active_session"]["open_threads"][0]["premises"] = list(premises)
    return st


def turn(scripted_llm, script, *, state=None, msg="mijn antwoord", draft=None):
    st = state or st_with()
    llm = scripted_llm(script, main_draft_text=draft)
    res = core.handle_turn(msg, state=st, conversation=[], save=False, stream=False, user_id="george")
    return res, st, llm


def classify(intent, corr):
    return {"intent": intent, "correctness": corr}


def judge_opts(llm):
    c = next(c for c in llm.calls if c[0] == "judge")
    return c[1], c[2]


# ============================================================ 1. judge style
@TODO("default judge = reasoning inside the JSON, think off, fixed length")
def test_default_judge_reasons_inside_the_json(scripted_llm):
    assert core.JUDGE_STYLE == "cot_json"
    res, st, llm = turn(scripted_llm, {"classify": classify("answer", "partial"),
                                       "judge": {"analysis": "a", "verdict": "partial", "reason": "r", "missing": "m"},
                                       "main": main_result("Wat gebeurt er dan?", verdict="partial")})
    msgs, opts = judge_opts(llm)
    assert not opts.get("think") and 0 < int(opts.get("max_tokens") or 0) <= 600
    sysmsg = msgs[0]["content"]
    key = '"claim"' if '"claim"' in sysmsg else '"verdict"'           # new prompt: claim/responsive; old prompt: verdict
    assert '"analysis"' in sysmsg and sysmsg.index('"analysis"') < sysmsg.index(key)   # reasoning before the verdict fields


@TODO("think style keeps a budget the thinking cannot exhaust before the JSON")
def test_think_style_has_room_for_the_json(scripted_llm, monkeypatch):
    monkeypatch.setattr(core, "JUDGE_STYLE", "think")
    res, st, llm = turn(scripted_llm, {"classify": classify("answer", "partial"),
                                       "judge": {"verdict": "correct", "reason": "r", "missing": None},
                                       "main": main_result("Een vraag?")})
    msgs, opts = judge_opts(llm)
    assert opts.get("think") and int(opts.get("max_tokens") or 0) >= 1000
    assert '"analysis"' not in msgs[0]["content"]


@TODO("analysis is kept for the log and never trusted over the verdict")
def test_judge_analysis_is_logged(scripted_llm, tmp_path, monkeypatch):
    res, st, llm = turn(scripted_llm, {"classify": classify("answer", "partial"),
                                       "judge": {"analysis": "ANALYSIS-TEXT: both scale with rho^2", "verdict": "correct",
                                                 "reason": "r", "missing": None},
                                       "main": main_result("Een vraag?")})
    assert res["stats"]["judge"]["verdict"] == "correct" and "ANALYSIS-TEXT" in res["stats"]["judge"]["analysis"]
    monkeypatch.setattr(debug_events, "DATA_DIR", tmp_path / "users")
    monkeypatch.setattr(debug_events, "BASE_DIR", tmp_path)
    monkeypatch.setattr(debug_events, "ENABLED", True)
    debug_events.log_turn("george", message="m", thread_from="zon", thread_to="zon", result=res)
    ev = debug_events.read_events("george", last=1)[0]
    assert "ANALYSIS-TEXT" in ev.get("judge_analysis", "") and ev.get("judge_style") == "cot_json"
    assert "judge analysis" in debug_events.format_events([ev], verbose=True)


@TODO("a truncated judge is visible (judge_truncated) and harmless")
def test_a_truncated_judge_is_visible_and_harmless(scripted_llm, tmp_path, monkeypatch):
    res, st, llm = turn(scripted_llm, {"classify": classify("answer", "partial"), "judge": {"__truncated__": True},
                                       "main": main_result("Een vraag?", verdict="partial")})
    assert res["ok"] and res["stats"].get("judge_failed") and res["stats"].get("judge_truncated")
    monkeypatch.setattr(debug_events, "DATA_DIR", tmp_path / "users")
    monkeypatch.setattr(debug_events, "BASE_DIR", tmp_path)
    monkeypatch.setattr(debug_events, "ENABLED", True)
    debug_events.log_turn("george", message="m", thread_from="zon", thread_to="zon", result=res)
    ev = debug_events.read_events("george", last=1)[0]
    assert ev.get("judge_truncated") is True and "judge_truncated" in debug_events.format_events([ev])


# ============================================================ 2. llm exposes done_reason
def _fresh_llm():
    spec = importlib.util.spec_from_file_location("llm_real", ROOT / "llm.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _Resp:
    status_code = 200

    def __init__(self, payload=None, lines=None):
        self._p, self._lines = payload, lines or []

    def raise_for_status(self): pass
    def json(self): return self._p
    def iter_lines(self, *a, **k): return iter(self._lines)
    def __enter__(self): return self
    def __exit__(self, *a): return False


@TODO("llm stats carry done_reason (non-stream)")
def test_llm_exposes_done_reason_non_stream(monkeypatch):
    m = _fresh_llm()
    payload = {"message": {"role": "assistant", "content": "", "thinking": "I am still thinking..."}, "done": True,
               "done_reason": "length", "prompt_eval_count": 10, "eval_count": 500,
               "prompt_eval_duration": 10**8, "eval_duration": 10**9, "total_duration": 2 * 10**9, "load_duration": 1}
    monkeypatch.setattr(m.requests, "post", lambda *a, **k: _Resp(payload))
    content, stats = m.ask_model([{"role": "user", "content": "x"}], stream=False)
    assert stats.get("done_reason") == "length"


@TODO("llm stats carry done_reason (stream)")
def test_llm_exposes_done_reason_stream(monkeypatch):
    m = _fresh_llm()
    lines = [json.dumps({"message": {"content": '{"response": "hi"'}, "done": False}).encode(),
             json.dumps({"message": {"content": ""}, "done": True, "done_reason": "length", "prompt_eval_count": 10,
                         "eval_count": 5, "prompt_eval_duration": 10**8, "eval_duration": 10**8,
                         "total_duration": 3 * 10**8, "load_duration": 1}).encode()]
    monkeypatch.setattr(m.requests, "post", lambda *a, **k: _Resp(lines=lines))
    content, stats = m.ask_model([{"role": "user", "content": "x"}], stream=True)
    assert stats.get("done_reason") == "length"


# ============================================================ 3. echo repair is auditable and switchable
@TODO("echo repair switch + echo_flagged audit")
def test_echo_repair_can_be_switched_off_and_is_still_audited(scripted_llm, monkeypatch):
    assert T.response_looks_like_echo_teach(ECHO_DRAFT)
    mk = lambda: {"classify": classify("answer", "correct"), "main": main_result(ECHO_DRAFT),     # fresh per run:
                  "repair": {"response": REPAIR_TEXT, "assessment": {}, "session": {}}}            # handle_turn mutates it
    monkeypatch.setattr(core, "ECHO_REPAIR", True)
    _, _, ctl = turn(scripted_llm, mk())
    assert "repair" in ctl.roles(), "control: with the switch on the repair runs"
    monkeypatch.setattr(core, "ECHO_REPAIR", False)
    res, st, llm = turn(scripted_llm, mk())
    assert "repair" not in llm.roles() and res["response"] == ECHO_DRAFT
    assert res["stats"].get("echo_flagged") is True and res["stats"].get("echo_skipped") == "disabled"


@TODO("echo_flagged reaches the event log and the tail view")
def test_echo_flag_is_logged(scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setattr(core, "ECHO_REPAIR", False)
    res, st, llm = turn(scripted_llm, {"classify": classify("answer", "correct"), "main": main_result(ECHO_DRAFT)})
    monkeypatch.setattr(debug_events, "DATA_DIR", tmp_path / "users")
    monkeypatch.setattr(debug_events, "BASE_DIR", tmp_path)
    monkeypatch.setattr(debug_events, "ENABLED", True)
    debug_events.log_turn("george", message="m", thread_from="zon", thread_to="zon", result=res)
    ev = debug_events.read_events("george", last=1)[0]
    assert ev.get("echo_flagged") is True and "echo_flagged=True" in debug_events.format_events([ev])


# ============================================================ 4. rotation keeps the feedback duty
@TODO("a rotation turn must give feedback on the last answer BEFORE the bridge")
def test_rotation_turn_asks_for_feedback_first():
    st = make_state(correct_streak=4, turn_intent="answer", turn_correctness="correct")
    T.ensure_concept(st, "kernfusie", "other")
    st["subjects"]["kernfusie"]["concepts"]["other"].update(
        {"title": "Ander concept", "knowledge": 0.2, "retrieval": 0.2, "application": 0.2, "explanation": 0.2, "transfer": 0.2})
    p = P.suggest_next_activity(st)
    assert p.get("move") == "rotate"
    assert "FIRST give clear feedback on their LAST answer" in p["reason"]
