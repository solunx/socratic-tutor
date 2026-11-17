"""
CONTRACT TESTS v6 — the stateful EMS run (reg_stateful_001: 6 sessions x 8 turns, judge on 33 of 48 turns) + step 3.

  Judge reliability (confusion matrix, sheet label x verdict): of 27 TRUE answers/challenges, 4 were judged INCORRECT (15%), 7 were
  not judged, 7 were uncertain; of 6 misconceptions 1 was judged CORRECT, 2 incorrect, 2 TRUNCATED (both on the Boost misconception).
  On the 11 decisive verdicts the judge was right 6 times (55%). Fix: a true/false claim needs a VERBATIM quote (`support`) from the
  setup or the evidence, otherwise it is unverified (-> uncertain, soft feedback); a truncated/unusable first answer gets ONE cheap
  retry without analysis; cot budget 600 tokens.
  Loop breaker: 5 of 6 "shall we wrap up / summary?" offers and the claims "you have shown nothing" came from OUR reframe (a menu with a
  wrap-up option, and "(nothing recorded)"). The loop breaker is now a continue-reply; the thread directive never invites closing.
  Learner echo: one reply was the learner's own earlier message. Evidence discipline: invented terms/thresholds/quotes ("6A-rem",
  "geen 6A-startdrempel op zon", "minimumlaadstand") and a hallucinated sign convention on a turn WITHOUT evidence.
"""
import sys
import types

import pytest

import core
import guards as G
import policy as P
import tutor as T
from conftest import history_upto, main_result, make_state

TODO = lambda what: (lambda f: f)  # implemented in patch 6

STARTED = "2026-11-10T10:00:00"
REPAIR_TEXT = "Prima. Kijk eens naar de stap die daarna komt: wat verandert er aan het contactpunt?"
CONTINUE_TEXT = "Dat klopt, die regel geldt voor een stilstaande auto. Wat gebeurt er dan met een auto die al laadt?"
LEARNER_A = "De 6A-rem voorkomt dat de auto 's nachts de batterijen leegtrekt tot de reserve. — daar ben ik nog onzeker over."
LEARNER_B = "Een stilstaande auto start niet onder 6 A, ook als de som net boven de reserve zit. (nog eens kort)"
Q_OLD = "Wat is het verschil tussen een stilstaande auto die moet starten en een lopende auto die stopt?"
BOOST_EVIDENCE = "Boost laadt de auto 's avonds en 's nachts uit de packs, zolang de packs boven de ingestelde rest staan."
MISCONCEPTION = "EV Boost kan alleen als er zonneproductie is"


def st_with(**act):
    return make_state(started_at=STARTED, **act)


def classify(intent, corr):
    return {"intent": intent, "correctness": corr}


def run(scripted_llm, script, *, state=None, msg="antwoord", conv=None, draft=None):
    st = state or st_with()
    llm = scripted_llm(script, main_draft_text=draft)
    res = core.handle_turn(msg, state=st, conversation=conv if conv is not None else [], save=False, stream=False, user_id="george")
    return res, st, llm


def sysmsg(llm, role="main"):
    return next(c for c in llm.calls if c[0] == role)[1][0]["content"]


def fake_knowledge(monkeypatch, snippets=None, enabled=True):
    pkg = types.ModuleType("knowledge"); pkg.__path__ = []
    te = types.ModuleType("knowledge.turn_evidence")
    fo = types.ModuleType("knowledge.focus")
    te.gather_turn_evidence = lambda state, text, tutor_mod=None: {
        "status": "found" if snippets else "not_found", "snippets": list(snippets or []), "enabled": enabled,
        "query": "q", "wiki_ok": False, "local_ok": True}
    te.inject_evidence_into_messages = lambda messages, ev: messages
    te.sources_payload = lambda ev: []
    te.format_sources_footer = lambda ev: ""
    fo.update_session_focus = lambda state, text: None
    fo.focus_policy_line = lambda state: ""
    for name, mod in (("knowledge", pkg), ("knowledge.turn_evidence", te), ("knowledge.focus", fo)):
        monkeypatch.setitem(sys.modules, name, mod)


def jd(claim, responsive, support=None, reason="r"):
    return {"analysis": "a", "claim": claim, "support": support, "responsive": responsive, "reason": reason, "missing": None}


# ================================================================ judge: a verdict needs a quote
@TODO("support quote is verified against the setup/evidence")
def test_verify_support_unit():
    hay = ["Boost laadt de auto 's avonds en 's nachts uit de packs.", "Twee sterren hebben dezelfde temperatuur"]
    assert core._verify_support("laadt de auto 's avonds en 's nachts uit de packs", hay)          # verbatim fragment
    assert core._verify_support("  DEZELFDE   temperatuur ", hay)                                  # case / spacing insensitive
    assert core._verify_support("“twee sterren hebben dezelfde temperatuur”", hay)                  # typographic quotes are noise
    assert not core._verify_support("EV Boost is gebonden aan zonneproductie", hay)               # the judge's own words
    assert not core._verify_support("packs", hay), "a fragment under 12 characters proves nothing"
    assert not core._verify_support(None, hay) and not core._verify_support("", hay) and not core._verify_support("null", hay)
    assert not core._verify_support("laadt de auto 's avonds", [])                                  # no evidence, no quote


@TODO("a true verdict with a verifiable quote is kept; without one it is downgraded to uncertain")
def test_true_claim_needs_a_quote(scripted_llm, monkeypatch):
    fake_knowledge(monkeypatch, snippets=[BOOST_EVIDENCE])
    ok = jd("true", "yes", support="laadt de auto 's avonds en 's nachts uit de packs")
    res, st, llm = run(scripted_llm, {"classify": classify("answer", "partial"), "judge": ok, "main": main_result("Klopt. En dan?")})
    j = res["stats"]["judge"]
    assert j["verdict"] == "correct" and j["support_verified"] is True and not j.get("downgraded")
    assert "CORRECT" in sysmsg(llm)
    bad = jd("true", "yes", support="EV Boost is gebonden aan zonneproductie")           # the judge's own words, not in the evidence
    res2, st2, llm2 = run(scripted_llm, {"classify": classify("answer", "partial"), "judge": bad, "main": main_result("Hoe kwam je daarop?")},
                          msg=MISCONCEPTION)
    j2 = res2["stats"]["judge"]
    assert j2["verdict"] == "uncertain" and j2["support_verified"] is False and j2["downgraded"] is True
    assert res2["stats"].get("judge_downgraded") is True
    s2 = sysmsg(llm2)
    assert "the learner's answer is CORRECT" not in s2 and "Do not call the answer right or wrong" in s2


@TODO("a false verdict needs a quote too: a rejection without grounds is a guess (4 of 27 true answers were rejected)")
def test_false_claim_needs_a_quote(scripted_llm, monkeypatch):
    fake_knowledge(monkeypatch, snippets=[BOOST_EVIDENCE])
    mc = {"misconceptions": [{"description": "thinks boost needs sun", "severity": "medium", "status": "open"}]}
    # grounded: the quote contradicts nothing here, but it IS in the evidence -> incorrect, misconception allowed
    res, st, llm = run(scripted_llm, {"classify": classify("answer", "partial"),
                                      "judge": jd("false", "yes", support="uit de packs, zolang de packs boven de ingestelde rest staan"),
                                      "main": main_result("Dat klopt niet. Wat gebeurt er 's nachts?", verdict="incorrect", assessment=mc)},
                       msg=MISCONCEPTION)
    assert res["stats"]["judge"]["verdict"] == "incorrect" and "does NOT hold" in sysmsg(llm)
    assert len(T.open_misconceptions(st["subjects"]["kernfusie"]["concepts"]["kernfusie"])) == 1
    # ungrounded: no quote -> unverified -> no rejection, no misconception
    res2, st2, llm2 = run(scripted_llm, {"classify": classify("answer", "partial"), "judge": jd("false", "yes", support=None),
                                         "main": main_result("Hoe kwam je daarop?", verdict="incorrect", assessment=mc)},
                          msg="Bij EV Boost mag de auto laden zonder zon")
    assert res2["stats"]["judge"]["verdict"] == "uncertain" and "does NOT hold" not in sysmsg(llm2)
    assert not T.open_misconceptions(st2["subjects"]["kernfusie"]["concepts"]["kernfusie"])


@TODO("a truncated first judge answer gets ONE cheap retry without analysis; both failing is visible")
def test_truncated_judge_is_retried_once(scripted_llm, monkeypatch):
    fake_knowledge(monkeypatch, snippets=[BOOST_EVIDENCE])
    good = jd("true", "yes", support="laadt de auto 's avonds en 's nachts uit de packs")
    res, st, llm = run(scripted_llm, {"classify": classify("answer", "partial"), "judge": [{"__truncated__": True}, good],
                                      "main": main_result("Klopt. En dan?")})
    assert llm.roles() == ["classify", "judge", "judge", "main"]
    assert res["stats"]["judge"]["verdict"] == "correct"
    assert res["stats"].get("judge_truncated") is True and res["stats"].get("judge_retry") is True and not res["stats"].get("judge_failed")
    calls = [c for c in llm.calls if c[0] == "judge"]
    assert '"analysis"' in calls[0][1][0]["content"] and '"analysis"' not in calls[1][1][0]["content"]
    assert not calls[1][2].get("think") and 0 < int(calls[1][2].get("max_tokens") or 0) <= 300
    res2, st2, llm2 = run(scripted_llm, {"classify": classify("answer", "partial"), "judge": {"__truncated__": True},
                                         "main": main_result("Een vraag?", verdict="partial")})
    assert res2["ok"] and res2["stats"].get("judge_failed") and res2["stats"].get("judge_truncated") and res2["stats"].get("judge_retry")


@TODO("the cot budget is 600 tokens (400 truncated 2 of 3 misconception judgements)")
def test_cot_budget_is_600(scripted_llm):
    res, st, llm = run(scripted_llm, {"classify": classify("answer", "partial"), "judge": jd("unverified", "no"),
                                      "main": main_result("Een vraag?")})
    opts = next(c for c in llm.calls if c[0] == "judge")[2]
    assert opts.get("max_tokens") == 600 and not opts.get("think")
    assert '"support"' in sysmsg(llm, "judge") and "VERBATIM" in sysmsg(llm, "judge")


# ================================================================ learner echo
@TODO("a reply that repeats the learner's own recent words is blocked and repaired")
def test_learner_echo_is_blocked(scripted_llm):
    conv = [{"role": "user", "content": LEARNER_A}, {"role": "assistant", "content": "Je koppelt de 6A-rem aan de batterij-reserve. " + Q_OLD}]
    hist = [{"role": m["role"], "text": m["content"]} for m in conv] + [{"role": "user", "text": LEARNER_B}]
    assert not G.repeat_gate(LEARNER_A, hist).blocked, "precondition: the old gate does not see it"
    res, st, llm = run(scripted_llm, {"classify": classify("answer", "correct"), "main": main_result(LEARNER_A),
                                      "repair": {"response": REPAIR_TEXT}}, msg=LEARNER_B, conv=[dict(m) for m in conv], draft=LEARNER_A)
    assert "repair" in llm.roles() and res["stats"].get("gate_repair") == "learner_echo" and res["response"] == REPAIR_TEXT
    assert "ONLY REPEATED THE LEARNER'S OWN WORDS" in next(c for c in llm.calls if c[0] == "repair")[1][-1]["content"]


@TODO("quoting the learner and adding content is NOT an echo")
def test_quoting_the_learner_is_not_an_echo():
    st = {"active_session": {"active": True}}
    hist = [{"role": "assistant", "text": "Wat doet het EMS bij 250 W?"}, {"role": "user", "text": "Een stilstaande auto start niet onder 6 A, ook als d som net boven d reserve zit."}]
    ok1 = ("Dat klopt: een stilstaande auto start niet onder 6 A, ook niet als de som net boven de reserve zit. Nu de lopende auto: "
           "als de bekende zon daalt naar 250 W, wat verwacht je dan dat het EMS doet?")
    ok2 = "Je hebt de stilstaande auto goed aangegeven, maar de vraag ging over de rijdende auto. Wat gebeurt er als die onder 300 W komt?"
    for ok in (ok1, ok2):
        g = core._extra_gate(ok, hist, st)
        assert g is None or g.reason != "learner_echo"
    # control: the rule exists
    assert core._extra_gate(hist[1]["text"], hist, st).reason == "learner_echo"


@TODO("the echo rule has no false positives on a real session")
def test_echo_rule_has_no_false_positives_on_a_real_session(fx):
    st = {"active_session": {"active": True}}
    # control: the rule exists (otherwise "no false positives" would pass vacuously on code without the rule)
    ctl_hist = [{"role": "user", "text": LEARNER_A}, {"role": "assistant", "text": "x"}, {"role": "user", "text": LEARNER_B}]
    assert core._extra_gate(LEARNER_A, ctl_hist, st).reason == "learner_echo"
    seen = 0
    for t in fx["turns"]:
        if t["role"] != "assistant":
            continue
        g = core._extra_gate(t["text"], history_upto(fx, t["idx"]), st)
        assert g is None or g.reason != "learner_echo", f"echo false positive on turn {t['idx']}"
        seen += 1
    assert seen > 10


@TODO("an exact echo on a concede turn gets the concede-specific reply")
def test_exact_echo_on_a_concede_turn(scripted_llm):
    conv = [{"role": "user", "content": LEARNER_A}, {"role": "assistant", "content": Q_OLD}]
    res, st, llm = run(scripted_llm, {"classify": classify("challenges_tutor", "correct"), "main": main_result(LEARNER_A),
                                      "repair": {"response": CONTINUE_TEXT}}, msg="Je blijft hangen op die vraag", conv=conv, draft=LEARNER_A)
    assert res["stats"].get("gate_repair") == "concede_duplicate" and res["response"] == CONTINUE_TEXT


# ================================================================ no closing, no menu, no "nothing shown"
@TODO("the thread directive never invites closing or a menu")
def test_thread_directive_never_invites_closing():
    d = P.thread_directive(2)
    assert "Do NOT offer to end, summarise or wrap up" in d and "menu" in d and "which part" not in d.lower()
    assert P.thread_directive(1) == ""


@TODO("the loop breaker is a continue-reply (no menu, no wrap-up, no frustration framing) and carries the questions already asked")
def test_loop_breaker_is_not_a_menu(fx, scripted_llm):
    turns = {t["idx"]: t for t in fx["turns"]}
    draft = turns[36]["text"]
    st = st_with(recent_probes=["Welke grens geldt voor een lopende auto?", "Wat doet het EMS bij 250 W?"])
    res, st, llm = run(scripted_llm, {"classify": classify("answer", "correct"), "main": main_result(draft),
                                      "repair": {"response": draft}, "reframe": {"response": CONTINUE_TEXT}},
                       state=st, msg=turns[35]["text"], conv=[{"role": m["role"], "content": m["content"]} for m in
                                                              [{"role": t["role"], "content": t["text"]} for t in fx["turns"] if t["idx"] < 35]], draft=draft)
    assert llm.roles() == ["classify", "main", "repair", "reframe"] and res["response"] == CONTINUE_TEXT
    sysm, body = next(c for c in llm.calls if c[0] == "reframe")[1]
    assert "Do NOT offer to end, summarise or wrap up" in sysm["content"] and "ONE NEW question" in sysm["content"]
    assert "frustrated" not in sysm["content"].lower()
    b = body["content"]
    assert "OPTIONS" not in b and "wrap up" not in b and "YOUR PREVIOUS REPLY" in b
    assert "QUESTIONS ALREADY ASKED" in b and "Wat doet het EMS bij 250 W?" in b


@TODO("the policy reframe (learner frustration) still offers options, but never claims 'nothing recorded'")
def test_policy_reframe_never_says_nothing_recorded(scripted_llm):
    res, st, llm = run(scripted_llm, {"classify": classify("frustration", "unknown"), "reframe": {"response": CONTINUE_TEXT}},
                       state=st_with(established_points=[]), msg="je blijft in rondjes draaien")
    b = next(c for c in llm.calls if c[0] == "reframe")[1][1]["content"]
    assert "OPTIONS" in b and "(nothing recorded)" not in b and "do not say that they have or have not shown anything" in b
    res2, st2, llm2 = run(scripted_llm, {"classify": classify("frustration", "unknown"), "reframe": {"response": CONTINUE_TEXT}},
                          state=st_with(established_points=["Learner showed: LEARNER-POINT-A"]), msg="je blijft in rondjes draaien")
    b2 = next(c for c in llm2.calls if c[0] == "reframe")[1][1]["content"]
    assert "ALREADY SHOWN" in b2 and "LEARNER-POINT-A" in b2 and "do not say that they have or have not shown" not in b2


# ================================================================ evidence discipline
@TODO("evidence discipline: facts only from the evidence; no evidence -> no facts; disabled -> silent; in the dynamic tail")
def test_evidence_discipline(scripted_llm, monkeypatch):
    script = lambda: {"classify": classify("answer", "correct"), "main": main_result("Een vraag over iets?")}
    fake_knowledge(monkeypatch, snippets=[BOOST_EVIDENCE])
    _, _, l1 = run(scripted_llm, script())
    s1 = sysmsg(l1)
    assert "EVIDENCE DISCIPLINE" in s1 and "NO SOURCE EVIDENCE" not in s1 and "ask what they mean by it" in s1
    assert s1.index("EVIDENCE DISCIPLINE") / len(s1) >= 0.8, "must sit in the dynamic tail, not in the cached prefix"
    fake_knowledge(monkeypatch, snippets=[])
    _, _, l2 = run(scripted_llm, script())
    s2 = sysmsg(l2)
    assert "NO SOURCE EVIDENCE" in s2 and "EVIDENCE DISCIPLINE" not in s2 and "do NOT state facts" in s2
    fake_knowledge(monkeypatch, snippets=[], enabled=False)
    _, _, l3 = run(scripted_llm, script())
    s3 = sysmsg(l3)
    assert "NO SOURCE EVIDENCE" not in s3 and "EVIDENCE DISCIPLINE" not in s3


@TODO("the chaos fuzzer (now with learner-echo drafts and invariant I13) finds nothing")
def test_chaos_fuzz_with_echo_has_no_violations():
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))
    import fuzz_turns
    bad = []
    for seed in range(150):
        bad += fuzz_turns.run_sequence(seed, 8, echo=True)
    assert not bad, bad[:3]
