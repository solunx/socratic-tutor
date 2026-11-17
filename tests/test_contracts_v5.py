"""
CONTRACT TESTS v5 — the three patterns of the EMS pilots (sim run pilot_ems_v3, beginner x ems_eco_basics, 8 turns) + chaos findings.

  P1  FALSE REJECT. The learner states TRUE things from the knowledge sheet ("a stationary car does not start below 6 A"), the judge says
      `incorrect` because the statement does not literally answer the tutor's probe ("The learner answered a different question").
      Cause (core._JUDGE_SYSTEM): "correct" was defined as "answers the tutor's question correctly" and the judge never saw the sources.
      Fix: the judge reports TWO things (is the claim TRUE per setup + SOURCE EVIDENCE; does it ANSWER the question); Python derives the
      verdict: false -> incorrect, unverified -> uncertain, true+yes -> correct, true+partly -> partial, true+no -> off_target.
      A true statement that does not answer is NEVER incorrect: confirm it, then build on it.
  P2  PROBE LOOP. The tutor asked the 300 W question five turns in a row (reworded), then the identical final question with a new preamble
      (turn 7), the lexical gate scored 0.67 and let it through.
      Fix: (a) rule `identical_question`: the SAME final question as one the learner already answered (<=6 turns), whatever the preamble;
      (b) off-target streak: two consecutive plain answers that do not address the question -> LEARNER THREAD directive (drop the probe,
      follow their point) + the last probes are banned (`dead_probe`).
  P3  CHALLENGE IGNORED. Turn 8 was byte-identical to turn 7 in reply to a challenge: concede turns were exempt from the gate (my own
      design flaw), so the exact duplicate went out.  Fix: an exact duplicate on a concede turn gets a concede-specific reply.

  Chaos (tools/fuzz_turns.py): a model returning `"session": "text"` crashed handle_turn (untyped result fields).
  Order: the FOCUS MODE line lagged one turn (policy ran before update_session_focus); evidence is gathered BEFORE the judge.
"""
import json
import sys
import types

import pytest

import core
import guards as G
import policy as P
import tutor as T
from conftest import FIX, history_upto, main_result, make_state

TODO = lambda what: (lambda f: f)

T4 = ("Kleine hint: 6A is een drempel, geen extra vermogen. Als de zon onder 300 W zakt, mag een lopende auto stoppen. "
      "Wat is het doel van die drempel, en waarom mag een lopende auto dan wel stoppen?")
T6 = ("Dat klopt niet helemaal: de bron zegt dat een lopende auto stopt als de zon onder 300 W zakt, niet als de batterij tot de reserve "
      "leegtrekt. Wat is het verschil tussen een drempel voor de zon en een drempel voor de batterij?")
T7 = ("Dat klopt: een stilstaande auto start niet onder 6 A, ook niet als de som net boven de reserve zit. Je hebt net gezegd dat de "
      "6A-rem voorkomt dat de auto 's nachts de batterijen leegtrekt tot de reserve. Maar de bron zegt dat een lopende auto stopt als de "
      "zon onder 300 W zakt. Wat is het verschil tussen een drempel voor de zon en een drempel voor de batterij?")
STILSTAAND = "Een stilstaande auto start niet onder 6 A, ook als d som net boven d reserve zit."
CHALLENGE = "Je stelde een vraag over EV Boost; dat zijn andere regels. Je vroeg net iets anders."
REPAIR_TEXT = "Prima. Kijk eens naar de stap die daarna komt: wat verandert er aan het contactpunt?"
CONCEDE_TEXT = "Je hebt gelijk dat ik over iets anders begon. Laten we bij Boost blijven: wanneer mag de auto daar laden?"
STARTED = "2026-11-05T10:00:00"


def st_with(**act):
    st = make_state(started_at=STARTED, **act)
    return st


def classify(intent, corr):
    return {"intent": intent, "correctness": corr}


def judge(claim, responsive, reason="r", missing=None, support=None):
    return {"analysis": "a", "claim": claim, "support": support, "responsive": responsive, "reason": reason, "missing": missing}


def run(scripted_llm, script, *, state=None, msg="antwoord", conv=None, draft=None):
    st = state or st_with()
    llm = scripted_llm(script, main_draft_text=draft)
    res = core.handle_turn(msg, state=st, conversation=conv if conv is not None else [], save=False, stream=False, user_id="george")
    return res, st, llm


def sysmsg(llm, role="main"):
    return next(c for c in llm.calls if c[0] == role)[1][0]["content"]


def install_fake_knowledge(monkeypatch, snippets=None, on_gather=None):
    pkg = types.ModuleType("knowledge"); pkg.__path__ = []
    te = types.ModuleType("knowledge.turn_evidence")
    fo = types.ModuleType("knowledge.focus")

    def gather(state, text, tutor_mod=None):
        if on_gather:
            on_gather()
        return {"status": "found" if snippets else "not_found", "snippets": list(snippets or []), "enabled": True,
                "query": "q", "wiki_ok": False, "local_ok": True}
    te.gather_turn_evidence = gather
    te.inject_evidence_into_messages = lambda messages, ev: messages
    te.sources_payload = lambda ev: []
    te.format_sources_footer = lambda ev: ""
    fo.update_session_focus = lambda state, text: state["active_session"].__setitem__("focus_mode", "eco")
    fo.focus_policy_line = lambda state: ("FOCUS MODE: " + state["active_session"]["focus_mode"]) if state["active_session"].get("focus_mode") else ""
    for name, mod in (("knowledge", pkg), ("knowledge.turn_evidence", te), ("knowledge.focus", fo)):
        monkeypatch.setitem(sys.modules, name, mod)


# ======================================================================= P1: truth vs responsiveness
@TODO("verdict is derived from (claim, responsive)")
@pytest.mark.parametrize("claim,resp,expect", [
    ("true", "yes", "correct"), ("true", "partly", "partial"), ("true", "no", "off_target"),
    ("false", "yes", "incorrect"), ("false", "no", "incorrect"), ("unverified", "yes", "uncertain"), ("unverified", "no", "uncertain")])
def test_verdict_is_derived_from_claim_and_responsiveness(claim, resp, expect):
    assert core._derive_verdict({"claim": claim, "responsive": resp}) == expect


@TODO("legacy `verdict` still works; garbage does not")
def test_legacy_verdict_fallback_and_garbage():
    assert core._derive_verdict({"verdict": "partial"}) == "partial"
    assert core._derive_verdict({"claim": "true", "responsive": "maybe", "verdict": "correct"}) == "correct"
    assert core._derive_verdict({"verdict": "maybe"}) is None and core._derive_verdict({}) is None


@TODO("a TRUE statement that does not answer the question is off_target, never incorrect (pilot turns 5-7)")
def test_true_but_unrelated_statement_is_not_a_false_reject(scripted_llm, monkeypatch):
    # a verdict needs a QUOTE from the setup/evidence (patch 6): the evidence says it, the judge quotes it
    install_fake_knowledge(monkeypatch, snippets=["Onder 6 A blijft een stilstaande auto uit, ook als de som net boven de reserve zit."])
    mc = {"misconceptions": [{"description": "confuses the start threshold with the stop threshold", "severity": "medium", "status": "open"}]}
    res, st, llm = run(scripted_llm, {"classify": classify("answer", "partial"), "judge": judge("true", "no", "true, but about starting", support="Onder 6 A blijft een stilstaande auto uit"),
                                      "main": main_result("Klopt. Wat gebeurt er als de auto al rijdt?", verdict="incorrect", assessment=mc)},
                       msg=STILSTAAND, conv=[{"role": "assistant", "content": T4}])
    assert res["stats"]["judge"]["verdict"] == "off_target"
    a = st["active_session"]
    assert a["last_verdict"] != "incorrect"
    c = st["subjects"]["kernfusie"]["concepts"]["kernfusie"]
    assert not T.open_misconceptions(c) and int(c.get("lapses") or 0) == 0
    s = sysmsg(llm)
    assert "is TRUE" in s and "does not answer your question" in s and "builds on THEIR statement" in s
    assert "BASE POLICY (secondary" in s, "the directive must replace the base probe instruction, not trail behind it"


@TODO("the judge sees the SOURCE EVIDENCE and the truth-vs-relevance rule; evidence is gathered BEFORE the judge")
def test_judge_prompt_has_evidence_and_the_rule(scripted_llm, monkeypatch):
    seen = {}
    holder = {}
    install_fake_knowledge(monkeypatch, snippets=[{"text": "EVIDENCE-LINE-ONE: een stilstaande auto start niet onder 6 A"}, "EVIDENCE-LINE-TWO"],
                           on_gather=lambda: seen.__setitem__("calls_before", len(holder["llm"].calls)))
    llm = scripted_llm({"classify": classify("answer", "partial"), "judge": judge("true", "no"), "main": main_result("Een vraag?")})
    holder["llm"] = llm
    st = st_with()
    core.handle_turn(STILSTAAND, state=st, conversation=[{"role": "assistant", "content": T4}], save=False, stream=False, user_id="george")
    msgs = next(c for c in llm.calls if c[0] == "judge")[1]
    user, system = msgs[1]["content"], msgs[0]["content"]
    assert "EVIDENCE-LINE-ONE" in user and "EVIDENCE-LINE-TWO" in user
    assert "absence of evidence is not proof of falsity" in user
    assert "it is NOT false" in system and "responsive" in system
    assert seen["calls_before"] == 1, "evidence must be gathered after classify and BEFORE the judge"


# ======================================================================= P2: probe loop
@TODO("two off-target answers in a row -> LEARNER THREAD directive; a responsive answer resets it")
def test_two_off_target_answers_drop_the_probe(scripted_llm):
    q = ["Wat gebeurt er als de zon onder 300 W zakt, en waarom?", "Welke regel geldt er voor een stilstaande auto, en waarom?",
         "En hoe zit het met de batterij in de nacht, wat denk je?", "Wat is het doel van die reserve, volgens jou?"]
    llm = scripted_llm({"classify": [classify("answer", "partial")] * 4,
                        "judge": [judge("true", "no"), judge("true", "no"), judge("true", "no"), judge("true", "yes")],
                        "main": [main_result(x, verdict="partial", signal="partial") for x in q]})
    st = st_with()
    conv = [{"role": "assistant", "content": T4}]
    for k in range(4):
        core.handle_turn(f"antwoord {k}", state=st, conversation=conv, save=False, stream=False, user_id="george")
    mains = [c for c in llm.calls if c[0] == "main"]
    s = lambda i: mains[i][1][0]["content"]
    # the streak is updated BEFORE the policy: the reply to the 2nd off-target answer already drops the probe
    assert "LEARNER THREAD" not in s(0)                                          # 1st off-target answer: not yet
    assert "LEARNER THREAD" in s(1) and "DROP your current line of questions" in s(1) and "2 times in a row" in s(1)
    assert "LEARNER THREAD" in s(2) and "3 times in a row" in s(2)
    assert "LEARNER THREAD" not in s(3)                                          # 4th answer was responsive: streak reset
    assert st["active_session"]["off_target_streak"] == 0


@TODO("the SAME final question with a new preamble is blocked (pilot turn 7: the lexical gate scored 0.67)")
def test_identical_final_question_with_a_new_preamble_is_blocked(scripted_llm):
    conv = [{"role": "user", "content": "x"}, {"role": "assistant", "content": T6}]
    assert not G.repeat_gate(T7, [{"role": h["role"], "text": h["content"]} for h in conv] + [{"role": "user", "text": STILSTAAND}]).blocked
    res, st, llm = run(scripted_llm, {"classify": classify("answer", "correct"), "main": main_result(T7),
                                      "repair": {"response": REPAIR_TEXT}}, msg=STILSTAAND, conv=conv, draft=T7)
    assert "repair" in llm.roles() and res["stats"].get("gate_repair") == "identical_question"
    assert res["response"] == REPAIR_TEXT


@TODO("the identical-question rule has no false positives on the labelled legitimate turns of a real session")
def test_identical_question_rule_has_no_false_positives(fx):
    st = {"active_session": {"active": True}}
    texts = {t["idx"]: t for t in fx["turns"]}
    for idx in fx["must_allow"]:
        if texts[idx]["role"] != "assistant":
            continue
        hist = history_upto(fx, idx)
        assert core._extra_gate(texts[idx]["text"], hist, st) is None, f"false positive on legitimate turn {idx}"
    # control: the rule exists and fires on a real repeat
    assert core._extra_gate(T7, [{"role": "assistant", "text": T6}, {"role": "user", "text": "x"}], st).reason == "identical_question"


@TODO("a probe the learner has twice not answered is banned, even reworded")
def test_dead_probe_is_banned_after_two_unanswered():
    hist = [{"role": "assistant", "text": "Intro."}, {"role": "user", "text": "x"}]
    probes = ["Wat is het doel van de 300 W-drempel, en waarom mag een lopende auto dan wel stoppen?"]
    reworded = "Waarom mag een lopende auto stoppen, en wat is het doel van die 300 W drempel?"
    other = "Welke kleur heeft de omvormer?"
    mk = lambda streak: {"active_session": {"active": True, "off_target_streak": streak, "recent_probes": probes}}
    assert core._extra_gate(reworded, hist, mk(2)).reason == "dead_probe"
    assert core._extra_gate(reworded, hist, mk(1)) is None, "not banned before two unanswered replies"
    assert core._extra_gate(other, hist, mk(2)) is None


# ======================================================================= P3: challenge ignored
@TODO("an exact duplicate on a concede turn gets a concede-specific reply (pilot turn 8)")
def test_concede_turn_with_a_bare_duplicate(scripted_llm):
    conv = [{"role": "assistant", "content": T7}]
    script = lambda: {"classify": classify("challenges_tutor", "correct"), "main": main_result(T7),
                      "repair": {"response": CONCEDE_TEXT}}
    res, st, llm = run(scripted_llm, script(), msg=CHALLENGE, conv=[dict(m) for m in conv], draft=T7)
    assert llm.roles() == ["classify", "main", "repair"] and res["response"] == CONCEDE_TEXT
    assert res["stats"].get("gate_repair") == "concede_duplicate"
    s = next(c for c in llm.calls if c[0] == "repair")[1]
    assert "corrected or challenged you" in s[0]["content"] and "YOUR PREVIOUS REPLY" in s[1]["content"] and CHALLENGE[:30] in s[1]["content"]
    # control: a concede turn with a DIFFERENT draft is left alone
    new = "Je hebt gelijk. Wanneer mag de auto bij Boost laden zonder zon, volgens jou?"
    res2, _, llm2 = run(scripted_llm, {"classify": classify("challenges_tutor", "correct"), "main": main_result(new)},
                        msg=CHALLENGE, conv=[dict(m) for m in conv], draft=new)
    assert llm2.roles() == ["classify", "main"] and res2["response"] == new


@TODO("if even the concede reply fails, the draft goes out but the failure is visible")
def test_failed_concede_reply_is_visible(scripted_llm):
    res, st, llm = run(scripted_llm, {"classify": classify("challenges_tutor", "correct"), "main": main_result(T7),
                                      "repair": {"response": ""}}, msg=CHALLENGE, conv=[{"role": "assistant", "content": T7}], draft=T7)
    assert res["response"] == T7 and res["stats"].get("concede_repair_failed") and res["stats"].get("gate_skipped") == "concede"


# ======================================================================= chaos findings + ordering
@TODO("a `session` of the wrong type must not crash the correction gate (chaos: AttributeError 'str' has no attribute 'get')")
@pytest.mark.parametrize("bad", ["text", ["x"], 5])
def test_session_of_the_wrong_type_does_not_crash_the_correction_gate(scripted_llm, bad):
    # the crash needs the path: verdict incorrect, no judge, gap status model_only -> core reads session_info.get("premises")
    main = main_result("Klopt niet helemaal. Wat denk je nu?", verdict="incorrect", signal="partial")
    main["session"] = bad
    res, st, llm = run(scripted_llm, {"classify": classify("answer", "correct"), "main": main,
                                      "evaluate": {"verdict": "uncertain", "reason": "r"}})
    assert res["ok"] and res["response"].startswith("Klopt niet")


@pytest.mark.parametrize("bad", [{"assessment": "garbage"}, {"routing": []}, {"assessment": [1], "session": 5}])
def test_other_wrongly_typed_model_fields_do_not_crash(scripted_llm, bad):
    """Regression (green before and after): type confusion in the other result fields."""
    main = {"routing": {"action": "continue"}, "response": "Klopt. Wat nu?", "assessment": {}, "session": {}}
    main.update(bad)
    res, st, llm = run(scripted_llm, {"classify": classify("answer", "correct"), "main": main})
    assert res["ok"] and res["response"].startswith("Klopt")


@TODO("a model result that is not even an object is handled as a parse failure")
def test_non_object_model_result_is_a_parse_failure(scripted_llm):
    res, st, llm = run(scripted_llm, {"classify": classify("answer", "correct"), "main": ["not", "an", "object"]})
    assert isinstance(res["response"], str) and res.get("ok") is False


@TODO("focus mode is updated BEFORE the policy line is built (it lagged one turn)")
def test_focus_mode_is_current_in_the_same_turn(scripted_llm, monkeypatch):
    install_fake_knowledge(monkeypatch)
    res, st, llm = run(scripted_llm, {"classify": classify("answer", "correct"), "main": main_result("Een vraag?")})
    assert "FOCUS MODE: eco" in sysmsg(llm)


@TODO("the chaos fuzzer finds no invariant violation")
def test_chaos_fuzz_has_no_violations():
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "tools"))
    import fuzz_turns
    bad = []
    for seed in range(120):
        bad += fuzz_turns.run_sequence(seed, 8)
    assert not bad, bad[:3]


# ======================================================================= more chaos findings (48,000 random turns, 0 violations after these)
@TODO("an EMPTY model reply gets one small retry; it is never replaced by a hardcoded sentence")
def test_empty_model_reply_gets_a_small_retry(scripted_llm):
    st = st_with()
    res, st, llm = run(scripted_llm, {"classify": classify("answer", "correct"), "main": main_result(""),
                                      "repair": {"response": "Prima gedaan. Wat gebeurt er dan met het contactpunt?"}}, state=st)
    assert llm.roles() == ["classify", "main", "repair"]
    assert res["response"].startswith("Prima gedaan") and res["stats"].get("empty_fallback") == "model"
    assert "in eigen woorden" not in res["response"]


@TODO("if the retry also fails, the open question is re-asked (language-correct), not a hardcoded Dutch sentence")
def test_empty_reply_falls_back_to_the_open_question(scripted_llm):
    st = st_with()
    q = st["active_session"]["open_threads"][0]["q"]
    res, st, llm = run(scripted_llm, {"classify": classify("answer", "partial"),
                                      "main": main_result("", verdict="partial", signal="partial"),   # partial keeps the thread open
                                      "repair": {"response": ""}}, state=st)
    assert res["response"] == q and res["stats"].get("empty_fallback") == "reask"


@TODO("a byte-identical reply is a repeat even when it contains NO question")
def test_identical_reply_without_a_question_is_blocked(scripted_llm):
    prev = "Je hebt gelijk. De afstoting groeit met de lading van beide kernen."
    conv = [{"role": "user", "content": "x"}, {"role": "assistant", "content": prev}]
    assert G.final_question(prev) is None and not G.repeat_gate(prev, [{"role": m["role"], "text": m["content"]} for m in conv]).blocked
    res, st, llm = run(scripted_llm, {"classify": classify("answer", "correct"), "main": main_result(prev),
                                      "repair": {"response": REPAIR_TEXT}}, msg="en nu?", conv=conv, draft=prev)
    assert "repair" in llm.roles() and res["stats"].get("gate_repair") == "identical_reply" and res["response"] == REPAIR_TEXT


@TODO("an unknown routing action from the model is a plain turn: the gates still run")
def test_unknown_routing_action_does_not_skip_the_gates(scripted_llm):
    conv = [{"role": "user", "content": "x"}, {"role": "assistant", "content": T6}]
    main = main_result(T6)
    main["routing"] = {"action": "bogus"}
    res, st, llm = run(scripted_llm, {"classify": classify("answer", "correct"), "main": main,
                                      "repair": {"response": REPAIR_TEXT}}, msg=STILSTAAND, conv=conv, draft=T6)
    assert "repair" in llm.roles() and res["response"] == REPAIR_TEXT


@TODO("a policy reframe is never the same text twice in a row; if it cannot be avoided it is logged")
def test_policy_reframe_is_not_repeated(scripted_llm):
    prev = "Je hebt gelijk dat we hier vastlopen. Wat wil je nu: dieper op dit punt, een ander onderwerp, of afronden?"
    conv = [{"role": "user", "content": "x"}, {"role": "assistant", "content": prev}]
    new = "Dat is terecht. Zullen we een ander onderdeel bekijken, of wil je een korte samenvatting?"
    res, st, llm = run(scripted_llm, {"classify": classify("frustration", "unknown"), "reframe": [{"response": prev}, {"response": new}]},
                       msg="je blijft in rondjes draaien", conv=[dict(m) for m in conv])
    assert llm.roles() == ["classify", "reframe", "reframe"] and res["response"] == new
    assert "do not repeat it" in next(c for c in llm.calls if c[0] == "reframe" and "YOUR PREVIOUS REPLY" in c[1][1]["content"])[1][1]["content"]
    res2, st2, llm2 = run(scripted_llm, {"classify": classify("frustration", "unknown"), "reframe": [{"response": prev}, {"response": prev}]},
                          msg="je blijft in rondjes draaien", conv=[dict(m) for m in conv])
    assert res2["response"] == prev and res2["stats"].get("reframe_repeat") is True            # last resort, visible


@TODO("save=False writes NOTHING, on every path (a quiet switch used to save via start_session)")
def test_save_false_never_writes_to_disk(scripted_llm, tmp_path, monkeypatch):
    import auth
    monkeypatch.setattr(auth, "DATA_DIR", tmp_path)
    st = st_with()
    T.ensure_subject(st, "tweede", "tweede")
    T.ensure_concept(st, "tweede", "tweede")
    main = main_result("Laten we daar naartoe gaan. Wat weet je er al van?")
    main["routing"] = {"action": "switch", "subject_id": "tweede"}
    res, st, llm = run(scripted_llm, {"classify": classify("answer", "correct"), "main": main}, state=st)
    written = [str(f) for f in tmp_path.rglob("*") if f.is_file()]
    assert not written, written
    assert T._PERSIST.get() is True, "the persistence switch must be restored after the turn"


@TODO("a contentless follow-up question ('And why?') is not a repeat just because the words are identical")
def test_contentless_followup_is_not_an_identical_question():
    hist = [{"role": "assistant", "text": "Dat klopt, de lading groeit mee. En waarom?"}, {"role": "user", "text": "omdat..."}]
    st = {"active_session": {"active": True}}
    assert core._extra_gate("Mooi, dat is de afstoting. En waarom?", hist, st) is None
    # control: a content-bearing identical question IS caught
    h2 = [{"role": "assistant", "text": "Hint. Welke keten levert dan de meeste energie, en waarom?"}, {"role": "user", "text": "x"}]
    assert core._extra_gate("Denk aan de temperatuur. Welke keten levert dan de meeste energie, en waarom?", h2, st).reason == "identical_question"
