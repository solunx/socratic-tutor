"""
CONTRACT TESTS for the new behaviour. All are xfail(strict=True) until implemented.
When one XPASSes, strict mode fails the run -> delete its marker (that is how you "check it off").

The contract (see README_FOR_GROK.md):
  core.llm_call(role, messages, **opts) -> SimpleNamespace(raw, parsed, stats)   # ONLY way to call a model
  roles: main | classify | evaluate | repair
  handle_turn order:  classify -> (policy) -> main -> [repeat/leak gate -> repair?] -> [correction gate: evaluate?]
  UI callback (on_response_delta) only ever receives text that passed the gates ("hold and flush").
  After a repair, state describes what the learner SAW (last_tutor_question = final question).
"""
import pytest

import core
import guards as G
import tutor as T
from conftest import conversation_upto, main_result, make_state

TODO = lambda what: pytest.mark.xfail(strict=True, reason=f"NOT IMPLEMENTED: {what}")

STARTED = "2026-10-17T16:38:33"
REPAIR_TEXT = "Je redenering is helder. Wat betekent dat voor een kern met acht protonen in plaats van één?"
CLASSIFY_ANSWER = {"intent": "answer", "correctness": "correct"}


def run_turn(fx, idx, scripted_llm, *, script, state=None, draft=None, stream=True):
    """Replay the learner message that precedes tutor turn `idx`; the stub 'model' drafts turn `idx`."""
    turns = {t["idx"]: t for t in fx["turns"]}
    learner_msg = turns[idx - 1]["text"]
    conv = conversation_upto(fx, idx - 1)
    state = state or make_state(started_at=STARTED)
    draft = draft if draft is not None else turns[idx]["text"]
    llm = scripted_llm({"classify": CLASSIFY_ANSWER, **script}, main_draft_text=draft)
    seen = []
    res = core.handle_turn(learner_msg, state=state, conversation=conv, save=False, stream=stream,
                           on_response_delta=lambda partial, delta: seen.append(partial), user_id="vriend1")
    return res, state, llm, seen, draft


# ----------------------------------------------------------------------------- 1. repeat gate
@pytest.mark.parametrize("idx", [36, 38, 42, 46, 50])
def test_repeat_is_repaired_before_the_learner_sees_it(fx, scripted_llm, idx):
    draft = {t["idx"]: t for t in fx["turns"]}[idx]["text"]
    res, state, llm, seen, _ = run_turn(
        fx, idx, scripted_llm, script={"main": main_result(draft), "repair": {"response": REPAIR_TEXT}})
    assert llm.roles() == ["classify", "main", "repair"]
    assert res["response"] == REPAIR_TEXT
    assert seen and seen[-1] == REPAIR_TEXT, "UI must only receive the approved text"
    assert all(G.final_question(draft) not in s for s in seen), "draft leaked to the UI"
    assert res["stats"].get("gate_repair") in ("repeat", "template_cap")


def test_state_matches_the_text_the_learner_saw_after_repair(fx, scripted_llm):
    draft = {t["idx"]: t for t in fx["turns"]}[36]["text"]
    res, state, llm, seen, _ = run_turn(
        fx, 36, scripted_llm, script={"main": main_result(draft), "repair": {"response": REPAIR_TEXT}})
    a = state["active_session"]
    assert a["last_tutor_question"] == G.final_question(REPAIR_TEXT)
    assert all(G.final_question(draft) not in q for q in (a.get("recent_questions") or []))
    assert a.get("last_verdict") == "correct", "assessment of the FIRST call must be kept"


@pytest.mark.parametrize("idx", [60, 64, 66, 68, 70])
def test_legitimate_turns_pass_untouched(fx, scripted_llm, idx):
    draft = {t["idx"]: t for t in fx["turns"]}[idx]["text"]
    res, state, llm, seen, _ = run_turn(fx, idx, scripted_llm, script={"main": main_result(draft)})
    assert llm.roles() == ["classify", "main"]
    assert res["response"] == draft and seen and seen[-1] == draft


def test_leaked_answer_is_repaired(fx, scripted_llm):
    draft = {t["idx"]: t for t in fx["turns"]}[34]["text"]
    main = main_result(draft, assessment={"gap_card": {"expected_answer": fx["expected_answers"]["34"]}})
    res, *_ = run_turn(fx, 34, scripted_llm, script={"main": main, "repair": {"response": REPAIR_TEXT}})
    assert res["stats"].get("gate_repair") == "leak"


def test_failed_repair_falls_back_to_draft_and_is_logged(fx, scripted_llm):
    draft = {t["idx"]: t for t in fx["turns"]}[36]["text"]
    res, *_ = run_turn(fx, 36, scripted_llm, script={"main": main_result(draft), "repair": {"response": ""}})
    assert res["response"] == draft
    assert res["stats"].get("gate_repair_failed")


# ----------------------------------------------------------------------------- 2. correction gate
def _contested_turn(fx, scripted_llm, evaluate_verdict):
    """vriend1's isotope insight (turn 67) judged 'incorrect' by the main call under an unverified key."""
    state = make_state(started_at=STARTED)
    state["active_session"]["open_threads"][0].update({
        "premises": fx["premises_at_60"],
        "gap_card": {"status": "model_only", "key": "meer lading = hogere barrière", "expected_answer": "x"}})
    draft = {t["idx"]: t for t in fx["turns"]}[68]["text"]
    main = main_result(draft, verdict="incorrect", signal="partial", assessment={"misconceptions": [
        {"description": "larger nuclei fuse more easily", "severity": "high", "status": "open"}]})
    res, state, llm, *_ = run_turn(fx, 68, scripted_llm, state=state,
                                   script={"main": main, "evaluate": {"verdict": evaluate_verdict, "reason": "r"}})
    return res, state, llm


def test_uncertain_evaluation_creates_no_misconception_and_no_lapse(fx, scripted_llm):
    res, state, llm = _contested_turn(fx, scripted_llm, "uncertain")
    c = state["subjects"]["kernfusie"]["concepts"]["kernfusie"]
    assert llm.roles().count("evaluate") == 1
    ev_messages = [m["content"] for m in next(c_ for c_ in llm.calls if c_[0] == "evaluate")[1]]
    assert all(any(p in m for m in ev_messages) for p in fx["premises_at_60"]), "premises must reach the evaluator"
    assert not T.open_misconceptions(c), "no open misconception from an unconfirmed correction"
    assert int(c.get("lapses") or 0) == 0 and c.get("last_grade") != 1
    assert res["stats"].get("correction_gate") == "uncertain"


def test_confirmed_incorrect_still_records_the_misconception(fx, scripted_llm):
    res, state, llm = _contested_turn(fx, scripted_llm, "incorrect")
    c = state["subjects"]["kernfusie"]["concepts"]["kernfusie"]
    assert T.open_misconceptions(c)


def test_premises_are_stored_and_shown_next_turn(fx, scripted_llm):
    state = make_state(started_at=STARTED, open_threads=[])
    q = "Stel twee kernen met dezelfde lading, de ene zwaarder. Welke heeft meer energie nodig, en waarom?"
    first = main_result(q, session={"premises": fx["premises_at_60"]})
    scripted_llm({"classify": CLASSIFY_ANSWER, "main": first}, main_draft_text=q)
    core.handle_turn("ga verder", state=state, conversation=[], save=False, stream=False, user_id="vriend1")
    ot = T.get_open_thread(state["active_session"])
    assert ot and ot.get("premises") == fx["premises_at_60"]

    second = main_result("Waar leidt dat toe voor het contactpunt van de twee kernen?")
    llm2 = scripted_llm({"classify": CLASSIFY_ANSWER, "main": second})
    core.handle_turn("ik denk dat de afstand groter is", state=state, conversation=[], save=False,
                     stream=False, user_id="vriend1")
    system_msg = next(c_ for c_ in llm2.calls if c_[0] == "main")[1][0]["content"]
    assert "PREMISES" in system_msg and all(p in system_msg for p in fx["premises_at_60"])


# ----------------------------------------------------------------------------- 3. same-turn classifier
def test_classifier_runs_first_and_drives_the_prompt(fx, scripted_llm):
    state = make_state(started_at=STARTED, must_advance=True, repeat_streak=3, same_activity_streak=3)
    llm = scripted_llm({"classify": {"intent": "asks_hint", "correctness": "unknown"},
                        "main": main_result("Kijk eens naar wat er gebeurt met de afstand bij contact?")})
    res = core.handle_turn("Hint please", state=state, conversation=[], save=False, stream=False, user_id="vriend1")
    assert res["ok"]
    assert llm.roles()[:2] == ["classify", "main"]
    system_msg = next(c_ for c_ in llm.calls if c_[0] == "main")[1][0]["content"].lower()
    assert "broke loop" not in system_msg and "new situation" not in system_msg


def test_classifier_failure_never_reuses_a_stale_intent(fx, scripted_llm, monkeypatch):
    state = make_state(started_at=STARTED, turn_intent="asks_hint", turn_correctness="correct")
    llm = scripted_llm({"classify": {"intent": "nonsense"}, "main": main_result("Wat denk je zelf?")})
    res = core.handle_turn("ik denk van wel", state=state, conversation=[], save=False, stream=False, user_id="vriend1")
    assert res["ok"]
    assert not state["active_session"].get("turn_intent")


# ----------------------------------------------------------------------------- 4. FSRS
def test_fsrs_is_graded_once_per_session(fx, scripted_llm):
    state = make_state(started_at=STARTED)
    results = []
    for i in range(6):
        verdict = "correct" if i % 2 == 0 else "incorrect"
        scripted_llm({"classify": CLASSIFY_ANSWER, "evaluate": {"verdict": "incorrect"},
                      "main": main_result(f"Reactie {i}: bekijk variant nummer {i} van het probleem, wat valt je op?",
                                          verdict=verdict)})
        results.append(core.handle_turn(f"antwoord {i}", state=state, conversation=[], save=False,
                                        stream=False, user_id="vriend1"))
    assert all(r["ok"] for r in results), "the turns must really run (otherwise this test is vacuous)"
    c = state["subjects"]["kernfusie"]["concepts"]["kernfusie"]
    assert int(c.get("review_count") or 0) == 1, "exactly one grade for six turns in one session"
