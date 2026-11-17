"""
CONTRACT TESTS v3 — quality first (george 2026-10-20, 18 turns, 7 interventions).

What the real session showed (see ANALYSIS in README_FOR_GROK_v3.md):
  1. Repairs broke the policy: the repair prompts said "move to a different aspect" / "change one variable of the setup"
     and knew neither the premises, nor the policy move, nor what the learner had shown -> premise violations,
     re-asserted wrong claims on turns where the policy said "concede", re-asked answered questions.
  2. The tutor graded its own homework: its (wrong) key made a CORRECT learner answer "partial" ->
     policy "ask the missing piece" (none exists) -> learner invents -> misconception stored.
     The correction gate only ran on verdict==incorrect; "partial" (15 of 18 turns) bypassed it.
  3. Nothing logged the discarded drafts or the reframe, so none of this was provable.

Contract:
  * core.llm_call role "judge": small impartial call BEFORE the main call. Gets premises + tutor question + learner
    message (+ the tutor key marked UNTRUSTED), think enabled, token-capped: llm_call("judge", msgs, think=..., max_tokens<=600).
    Returns {"verdict": correct|partial|incorrect|uncertain, "reason": str, "missing": str|null}.
  * Triggered only when intent in (answer, insight, challenges_tutor) AND classifier correctness in (partial, incorrect, unknown).
  * active_session.judge is authoritative for the verdict: it replaces the tutor's own assessment.verdict
    (uncertain -> partial + no FSRS), feeds policy (turn_correctness) and puts a verdict-specific feedback
    instruction in policy.reason ("JUDGE (...)": FIRST sentence says right / what is right / does not hold / do not judge).
  * A misconception is stored ONLY after a confirmed error (judge incorrect, or a verified key); never from partial/uncertain.
  * Repairs carry CONSTRAINTS (premises, learner-shown points, policy reason) and never say "different aspect" /
    "change one variable". Turns with intent challenges_tutor / learner_correction are NEVER rewritten
    (stats.gate_skipped / echo_skipped == "concede").
  * stats (and events): draft, draft_question, judge_called, judge_failed, judge{verdict,reason,missing}, reframe,
    reframe_failed, gate_skipped, echo_skipped.
  * reframe shows only points the LEARNER demonstrated ("Learner showed:"), plus the judge's view of their last claim.
"""
import json
from pathlib import Path

import pytest

import core
import debug_events
import policy as P
import tutor as T
from conftest import FIX, conversation_upto, main_result, make_state

TODO = lambda what: (lambda f: f)

STARTED = "2026-10-20T17:53:05"
REPAIR_TEXT = "Prima. Kijk eens naar de stap die daarna komt: wat verandert er aan het contactpunt?"
REFRAME_TEXT = "Je hebt gelijk dat we hier vastlopen. Wat wil je nu: dieper op dit punt, een ander onderwerp, of afronden?"
ECHO_DRAFT = ("Geen probleem, dat is een lastig punt. De afstoting hangt af van de ladingen van de kernen. Hoe meer protonen "
              "in een kern, hoe groter de positieve lading. Een kern met meer protonen stoot een naderend proton dus "
              "sterker af. Kun je nu in één zin uitleggen waarom een kern met meer protonen een sterkere afstoting heeft?")
PREM = ["Twee sterren hebben dezelfde temperatuur", "Dezelfde CNO-concentratie", "Verschillende protonendichtheid"]


def density():
    return json.loads((FIX / "density_case.json").read_text(encoding="utf-8"))


def state_with(premises=None, shown=None, **act):
    st = make_state(started_at=STARTED, **act)
    if premises:
        st["active_session"]["open_threads"][0]["premises"] = list(premises)
    if shown:
        st["active_session"]["established_points"] = list(shown)
    return st


def turn(scripted_llm, script, *, state=None, msg="mijn antwoord", conv=None, draft=None, stream=False, cb=None):
    st = state or state_with()
    llm = scripted_llm(script, main_draft_text=draft)
    res = core.handle_turn(msg, state=st, conversation=conv if conv is not None else [], save=False, stream=stream,
                           on_response_delta=cb, user_id="george")
    return res, st, llm


def msgs_of(llm, role):
    return next(c for c in llm.calls if c[0] == role)[1]


def blob(llm, role):
    return "\n".join(m["content"] for m in msgs_of(llm, role))


def classify(intent, corr):
    return {"intent": intent, "correctness": corr}


def j(verdict, reason="r", missing=None):
    return {"verdict": verdict, "reason": reason, "missing": missing}


# =========================================================== 1. observability
def _log(tmp_path, monkeypatch, res):
    monkeypatch.setattr(debug_events, "DATA_DIR", tmp_path / "users")
    monkeypatch.setattr(debug_events, "BASE_DIR", tmp_path)
    monkeypatch.setattr(debug_events, "ENABLED", True)
    debug_events.log_turn("george", message="m", thread_from="zon", thread_to="zon", result=res)
    return debug_events.read_events("george", last=1)[0]


@TODO("keep the discarded draft in stats + events when a repair replaced it")
def test_events_keep_the_discarded_draft(fx, scripted_llm, tmp_path, monkeypatch):
    turns = {t["idx"]: t for t in fx["turns"]}
    draft = turns[36]["text"]
    res, st, llm = turn(scripted_llm, {"classify": classify("answer", "correct"), "main": main_result(draft),
                                       "repair": {"response": REPAIR_TEXT}},
                        msg=turns[35]["text"], conv=conversation_upto(fx, 35), draft=draft)
    assert res["stats"]["draft"].startswith(draft[:60]) and res["stats"]["draft_question"]
    ev = _log(tmp_path, monkeypatch, res)
    assert ev.get("gate_repair") == "repeat" and ev.get("draft", "").startswith(draft[:60])
    assert ev.get("draft_question")


@TODO("log reframe + judge fields in events and show them in tail_events")
def test_events_log_reframe_and_judge_and_the_tail_view_shows_them(scripted_llm, tmp_path, monkeypatch):
    res, st, llm = turn(scripted_llm, {"classify": classify("answer", "partial"), "judge": j("partial", "half", "the second effect"),
                                       "main": main_result("Wat gebeurt er met het tweede effect?", verdict="partial")},
                        state=state_with(PREM))
    ev = _log(tmp_path, monkeypatch, res)
    assert ev.get("judge_called") is True and ev.get("judge_verdict") == "partial"
    assert ev.get("judge_missing") == "the second effect"
    txt = debug_events.format_events([ev])
    assert "judge: partial" in txt
    # reframe
    res2, *_ = turn(scripted_llm, {"classify": classify("frustration", "unknown"), "reframe": {"response": REFRAME_TEXT}})
    ev2 = _log(tmp_path, monkeypatch, res2)
    assert ev2.get("reframe") == "policy" and "reframe=policy" in debug_events.format_events([ev2])


# =========================================================== 2. repairs with policy
@TODO("repairs carry premises, learner-shown points and the policy reason; never 'different aspect'")
def test_gate_repair_carries_the_constraints(fx, scripted_llm):
    turns = {t["idx"]: t for t in fx["turns"]}
    draft = turns[36]["text"]
    st = state_with(PREM, shown=["Learner showed: UNIQUE-POINT-ONE", "Established: TUTOR-ASSERTION-X"])
    res, st, llm = turn(scripted_llm, {"classify": classify("answer", "correct"), "main": main_result(draft),
                                       "repair": {"response": REPAIR_TEXT}},
                        state=st, msg=turns[35]["text"], conv=conversation_upto(fx, 35), draft=draft)
    b = msgs_of(llm, "repair")[-1]["content"]          # the repair instruction itself
    assert "CONSTRAINTS" in b and "POLICY:" in b
    assert all(p in b for p in PREM) and "UNIQUE-POINT-ONE" in b
    assert "TUTOR-ASSERTION-X" not in b, "tutor assertions are not 'shown by the learner'"
    assert "different aspect" not in b.lower() and "change one variable" not in b.lower()


@TODO("echo repair carries constraints and no longer tells the model to change the setup")
def test_echo_repair_carries_the_constraints(scripted_llm):
    st = state_with(PREM)
    assert T.response_looks_like_echo_teach(ECHO_DRAFT), "fixture draft must trigger the echo heuristic"
    res, st, llm = turn(scripted_llm, {"classify": classify("answer", "correct"), "main": main_result(ECHO_DRAFT),
                                       "repair": {"response": REPAIR_TEXT, "assessment": {}, "session": {}}}, state=st)
    assert llm.roles() == ["classify", "main", "repair"]
    b = msgs_of(llm, "repair")[-1]["content"]
    assert "CONSTRAINTS" in b and all(p in b for p in PREM)
    assert "change one variable" not in b.lower() and "different case" not in b.lower()


@TODO("concede turns are not rewritten by the GENERIC repair (an exact duplicate gets the concede-specific reply, see v5)")
@pytest.mark.parametrize("kind", ["gate", "echo"])
def test_concede_turns_are_never_rewritten(fx, scripted_llm, kind, monkeypatch):
    turns = {t["idx"]: t for t in fx["turns"]}
    draft = turns[36]["text"] if kind == "gate" else ECHO_DRAFT
    conv = conversation_upto(fx, 35) if kind == "gate" else []
    msg = turns[35]["text"] if kind == "gate" else "Dat zei ik toch al, de temperatuur was gelijk"
    if kind == "gate":
        # a gate block that is NOT a near-exact duplicate (score 0.9): on a concede turn it must not be rewritten.
        # (an exact duplicate is handled by the concede-specific reply; see test_contracts_v5)
        import guards
        monkeypatch.setattr(core.G, "repeat_gate", lambda cand, hist, **k: guards.GateResult(True, "repeat", 0.9, 1))
    script = lambda intent: {"classify": classify(intent, "correct"), "main": main_result(draft),
                             "repair": {"response": REPAIR_TEXT, "assessment": {}, "session": {}}}
    # control: with a plain answer the repair DOES run (otherwise this test would pass vacuously)
    _, _, ctl = turn(scripted_llm, script("answer"), state=state_with(PREM), msg=msg, conv=conv, draft=draft)
    assert "repair" in ctl.roles()
    res, st, llm = turn(scripted_llm, script("challenges_tutor"), state=state_with(PREM), msg=msg, conv=conv, draft=draft)
    assert "repair" not in llm.roles() and "reframe" not in llm.roles()
    assert res["response"] == draft
    assert res["stats"].get("gate_skipped" if kind == "gate" else "echo_skipped") == "concede"


# =========================================================== 3. the judge
@TODO("judge runs BEFORE main on contested answers, with premises, think and a token cap")
def test_judge_runs_before_main_with_premises_think_and_a_cap(scripted_llm):
    d = density()
    st = state_with(d["premises"])
    res, st, llm = turn(scripted_llm, {"classify": classify("answer", "partial"), "judge": j("correct", "ratio is rho-independent"),
                                       "main": main_result("Klopt. Wat verandert er als de temperatuur stijgt?")},
                        state=st, msg=d["learner_answer"], conv=[{"role": "assistant", "content": d["tutor_question"]}])
    assert llm.roles() == ["classify", "judge", "main"]
    b = blob(llm, "judge")
    assert all(p in b for p in d["premises"]) and d["learner_answer"][:40] in b and d["tutor_question"][:40] in b
    assert len(b) < 2500, "the judge must not carry the big system prompt"
    opts = next(c for c in llm.calls if c[0] == "judge")[2]
    style = getattr(core, "JUDGE_STYLE", "think")        # patch 3: "think"; patch 4: default "cot_json"
    if style == "think":
        assert opts.get("think") and 0 < int(opts.get("max_tokens") or 0) <= 1500
    else:
        assert not opts.get("think") and 0 < int(opts.get("max_tokens") or 0) <= 600


@TODO("judge only where the tutor would grade its own homework")
@pytest.mark.parametrize("intent,corr", [("answer", "correct"), ("asks_hint", "unknown"), ("asks_reveal", "unknown"),
                                         ("side_question", "unknown")])
def test_judge_is_not_called_when_not_needed(scripted_llm, intent, corr):
    ctl_script = {"classify": classify("answer", "partial"), "judge": j("correct"), "main": main_result("Vraag?")}
    _, _, ctl = turn(scripted_llm, ctl_script)
    assert ctl.roles() == ["classify", "judge", "main"], "control: a contested answer DOES call the judge"
    _, _, llm = turn(scripted_llm, {"classify": classify(intent, corr), "main": main_result("Een nieuwe vraag over iets anders?")})
    assert "judge" not in llm.roles()


@TODO("judge verdict overrides the tutor's partial verdict and counts for the streak; instruction reaches the prompt")
def test_judge_correct_overrides_partial_and_instructs_a_clear_confirmation(scripted_llm):
    d = density()
    res, st, llm = turn(scripted_llm, {"classify": classify("answer", "partial"), "judge": j("correct", "both scale with rho^2"),
                                       "main": main_result("Een andere vraag?", verdict="partial", signal="partial")},
                        state=state_with(d["premises"]), msg=d["learner_answer"])
    a = st["active_session"]
    assert a["last_verdict"] == "correct" and a["correct_streak"] == 1
    sysmsg = msgs_of(llm, "main")[0]["content"]
    assert "JUDGE" in sysmsg and "CORRECT" in sysmsg and "Do not add a 'but'" in sysmsg
    assert "missing piece" in sysmsg.lower()      # "...do not ask for a 'missing piece'"
    assert res["stats"]["judge"]["verdict"] == "correct"


@TODO("a partial judge hands the missing piece to the prompt without stating it")
def test_partial_judge_passes_the_missing_piece(scripted_llm):
    res, st, llm = turn(scripted_llm, {"classify": classify("answer", "unknown"),
                                       "judge": j("partial", "half there", "MISSING-XYZ-PIECE"),
                                       "main": main_result("Wat gebeurt er met het tweede effect?", verdict="partial")})
    sysmsg = msgs_of(llm, "main")[0]["content"]
    assert "MISSING-XYZ-PIECE" in sysmsg and "WITHOUT stating it" in sysmsg


@TODO("an uncertain judge forbids calling the answer right or wrong and spares FSRS")
def test_uncertain_judge_forbids_judging_and_spares_fsrs(scripted_llm):
    res, st, llm = turn(scripted_llm, {"classify": classify("answer", "unknown"), "judge": j("uncertain", "subtle"),
                                       "main": main_result("Hoe kwam je daarop?", verdict="incorrect", signal="partial")})
    assert "Do not call the answer right or wrong" in msgs_of(llm, "main")[0]["content"]
    c = st["subjects"]["kernfusie"]["concepts"]["kernfusie"]
    assert int(c.get("lapses") or 0) == 0 and c.get("last_grade") != 1
    assert st["active_session"]["last_verdict"] != "incorrect"


@TODO("misconceptions only after a CONFIRMED error — also closes the 'partial verdict' hole")
@pytest.mark.parametrize("cls,judge,verdict,expect", [
    (classify("answer", "partial"), j("incorrect", "does not hold"), "incorrect", 1),
    (classify("answer", "partial"), j("uncertain"), "partial", 0),
    (classify("answer", "partial"), j("partial", "half", "x"), "partial", 0),
    (classify("answer", "correct"), None, "partial", 0),          # judge not triggered, tutor says partial + proposes an MC
])
def test_misconception_needs_a_confirmed_error(scripted_llm, cls, judge, verdict, expect):
    mc = {"misconceptions": [{"description": "attributes the effect to size rather than temperature", "severity": "medium", "status": "open"}]}
    script = {"classify": cls, "main": main_result("Een vraag?", verdict=verdict, signal="partial", assessment=mc)}
    if judge:
        script["judge"] = judge
    res, st, llm = turn(scripted_llm, script, state=state_with(PREM))
    assert res["ok"]
    assert len(T.open_misconceptions(st["subjects"]["kernfusie"]["concepts"]["kernfusie"])) == expect
    # control inside the parametrization: the incorrect case proves the MC path itself is alive
    if expect:
        assert llm.roles() == ["classify", "judge", "main"]


@TODO("a broken judge must not break the turn and must be visible")
def test_a_failing_judge_is_visible_and_harmless(scripted_llm):
    res, st, llm = turn(scripted_llm, {"classify": classify("answer", "partial"), "judge": {"verdict": "maybe"},
                                       "main": main_result("Een vraag?", verdict="partial")})
    roles = llm.roles()
    assert res["ok"] and roles[0] == "classify" and roles[-1] == "main" and roles.count("judge") in (1, 2)   # (patch 6: one cheap retry)
    assert res["stats"].get("judge_called") is True and res["stats"].get("judge_failed") is True
    assert not st["active_session"].get("judge")


@TODO("a challenge the judge finds INCORRECT is not conceded")
def test_incorrect_challenge_is_explained_not_conceded(scripted_llm):
    res, st, llm = turn(scripted_llm, {"classify": classify("challenges_tutor", "unknown"),
                                       "judge": j("incorrect", "the premise says the temperature is fixed"),
                                       "main": main_result("Kijk nog eens naar de premisse?", verdict="partial")},
                        state=state_with(PREM))
    sysmsg = msgs_of(llm, "main")[0]["content"]
    assert "does NOT hold" in sysmsg and "LEARNER CORRECTION / challenges_tutor" not in sysmsg
    assert not st["active_session"].get("learner_correction")


@TODO("policy.reason carries the verdict-specific feedback instruction (pure policy level)")
@pytest.mark.parametrize("verdict,needle", [("correct", "FIRST sentence: say clearly that it is right"),
                                            ("partial", "ONLY for the missing piece"),
                                            ("incorrect", "does NOT hold"),
                                            ("uncertain", "Do not call the answer right or wrong")])
def test_policy_reason_has_a_directive_per_verdict(verdict, needle):
    st = state_with(PREM, turn_intent="answer", turn_correctness="partial", judge=j(verdict, "why", "the rest"))
    reason = P.suggest_next_activity(st)["reason"]
    assert "JUDGE" in reason and needle in reason
    assert "Open with approval" in reason          # approval only on CORRECT; 'my mistake' for tutor errors


# =========================================================== 4. the human-checked density case (end to end wiring)
@TODO("a correct answer (as judged) must not become 'ask the missing piece'")
def test_density_case_correct_answer_is_confirmed_not_challenged(scripted_llm):
    d = density()
    assert d["correct_verdict"] == "correct" and d["ambiguous"]      # wiring test; the TRUTH depends on the reading
    # exactly what production did: classifier 'partial', tutor model verdict 'partial', draft challenges the learner
    res, st, llm = turn(scripted_llm, {"classify": classify("answer", d["observed_in_production"]["classifier_correctness"]),
                                       "judge": j(d["correct_verdict"], "at fixed T and composition both rates scale with rho^2"),
                                       "main": main_result(d["bad_tutor_reply"], verdict="partial", signal="partial",
                                                           assessment={"misconceptions": [{"description": "size rather than temperature", "severity": "medium", "status": "open"}]})},
                        state=state_with(d["premises"]), msg=d["learner_answer"],
                        conv=[{"role": "assistant", "content": d["tutor_question"]}])
    sysmsg = msgs_of(llm, "main")[0]["content"]
    assert "the learner's answer is CORRECT" in sysmsg and "do not ask for a 'missing piece'" in sysmsg
    a = st["active_session"]
    assert a["last_verdict"] == "correct" and a["correct_streak"] == 1
    assert not T.open_misconceptions(st["subjects"]["kernfusie"]["concepts"]["kernfusie"])


# =========================================================== 5. reframe honesty
@TODO("reframe summarises only what the LEARNER showed and gives the judge's view of the last claim")
def test_reframe_uses_learner_points_and_the_judge_view(fx, scripted_llm):
    turns = {t["idx"]: t for t in fx["turns"]}
    draft = turns[36]["text"]
    st = state_with(PREM, shown=["Learner showed: LEARNER-POINT-A", "Established: TUTOR-CLAIM-B"])
    res, st, llm = turn(scripted_llm, {"classify": classify("answer", "partial"), "judge": j("partial", "half there", "the rest"),
                                       "main": main_result(draft), "repair": {"response": draft}, "reframe": {"response": REFRAME_TEXT}},
                        state=st, msg=turns[35]["text"], conv=conversation_upto(fx, 35), draft=draft)
    assert llm.roles() == ["classify", "judge", "main", "repair", "reframe"] and res["response"] == REFRAME_TEXT
    b = blob(llm, "reframe")
    assert "LEARNER-POINT-A" in b and "TUTOR-CLAIM-B" not in b
    assert "JUDGE ON THEIR LAST CLAIM" in b and "partial" in b


# =========================================================== 6. memory hygiene: tutor assertions are not 'shown by the learner'
@TODO("tutor-asserted points are stored as unverified")
def test_tutor_assertions_are_stored_as_unverified():
    st = state_with(PREM)
    T.update_session_memory(st, {"session_thread": "Established: this mechanism dominates under condition X. Open: the next step",
                                 "last_question": "Wat denk je van stap twee, en waarom?"}, "antwoord")
    T.update_session_memory(st, {"session_thread": "Learner showed: they derived the scaling on their own. Open: more",
                                 "last_question": "En wat als de waarde verdubbelt, wat dan?"}, "antwoord")
    pts = st["active_session"]["established_points"]
    assert any(x.startswith("Tutor-asserted (unverified):") and "mechanism dominates" in x for x in pts)
    assert any(x.startswith("Learner showed:") for x in pts)
    assert T.is_learner_point("Learner showed: x") and not T.is_learner_point("Tutor-asserted (unverified): x")
    assert not T.is_learner_point("Established: x")     # legacy state lines without a prefix are treated as tutor-asserted


@TODO("the prompt lists ONLY learner points as 'shown by the learner'")
def test_prompt_lists_only_learner_points_as_shown_by_the_learner(scripted_llm):
    st = state_with(PREM, shown=["Learner showed: AAA-LEARNER", "Established: BBB-LEGACY-TUTOR",
                                 "Tutor-asserted (unverified): CCC-TUTOR"])
    res, st, llm = turn(scripted_llm, {"classify": classify("answer", "correct"), "main": main_result("Een vraag over iets?")}, state=st)
    sysmsg = msgs_of(llm, "main")[0]["content"]
    i = sysmsg.index("Already shown by the LEARNER")
    seg = sysmsg[i:i + 700]
    assert "AAA-LEARNER" in seg and "BBB-LEGACY-TUTOR" not in seg and "CCC-TUTOR" not in seg
