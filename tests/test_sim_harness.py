"""Student-agent harness — pure, no Ollama."""
from sim.intent import choose_intent
from sim.student import student_utterance, StudentMemory
from sim.runner import run_session, run_batch, load_persona, load_scenario, _fresh_state
from sim.curriculum import curriculum_steps
import random


def test_intent_distribution_stable_with_seed():
    p = load_persona("beginner")
    rng = random.Random(42)
    a = [choose_intent(p, i, rng) for i in range(20)]
    rng2 = random.Random(42)
    b = [choose_intent(p, i, rng2) for i in range(20)]
    assert a == b


def test_anti_repeat_and_escalation():
    sheet = {
        "knows": ["Stilstaande auto start niet onder 6A."],
        "half_knows": ["iets met 6A en zon"],
        "misconceptions": [{"statement": "6A is extra batterijvermogen"}],
        "topic": "EMS",
    }
    mem = StudentMemory()
    a = student_utterance(intent="hint_request", sheet=sheet, tutor_last="Waarom 6A?", persona={"style": "plain"}, memory=mem)
    b = student_utterance(intent="hint_request", sheet=sheet, tutor_last="Waarom 6A?", persona={"style": "plain"}, memory=mem)
    c = student_utterance(intent="hint_request", sheet=sheet, tutor_last="Waarom 6A?", persona={"style": "plain"}, memory=mem)
    assert a != b
    assert "6A" in c or "Stilstaande" in c


def test_curriculum_ems_length():
    steps = curriculum_steps("ems_eco_basics")
    assert steps and len(steps) >= 6


def test_state_is_passed_across_turns():
    """Harness must keep the same state object (save=False must not rely on disk)."""
    seen = []

    def stub(message, state=None, conversation=None, **kw):
        assert state is not None, "state must be provided"
        seen.append(id(state))
        state.setdefault("active_session", {})["active"] = True
        state["active_session"]["subject_id"] = "ems"
        if conversation is not None:
            conversation.append({"role": "user", "content": message})
            conversation.append({"role": "assistant", "content": "Wat is de reserve?"})
        return {
            "ok": True, "kind": "message", "response": "Wat is de reserve?",
            "subject_id": "ems", "stats": {},
        }

    persona = load_persona("beginner")
    scenario = load_scenario("ems_eco_basics")
    out = run_session(
        persona=persona, scenario=scenario, handle_turn=stub,
        seed=0, max_turns=4, do_setup=True, use_curriculum=True, require_active=True,
    )
    assert not out.get("setup_failed")
    assert len(seen) >= 4
    assert len(set(seen)) == 1, "same state object every call"
    assert out["sim_health"]["n_active_turns"] == out["sim_health"]["n_turns"]


def test_setup_abort_when_never_active():
    def stub(message, state=None, conversation=None, **kw):
        # never activates session
        return {"ok": True, "kind": "new_subject", "response": "Nieuw onderwerp?", "subject_id": "x", "stats": {}}

    out = run_session(
        persona=load_persona("beginner"),
        scenario=load_scenario("ems_eco_basics"),
        handle_turn=stub,
        seed=0, max_turns=4, require_active=True,
    )
    assert out.get("setup_failed") is True
    assert out["turns"] == []


def test_batch_dry_write(tmp_path, monkeypatch):
    def stub(message, state=None, conversation=None, **kw):
        if state is not None:
            state.setdefault("active_session", {})["active"] = True
        return {"ok": True, "kind": "message", "response": "?", "subject_id": "ems", "stats": {}}

    import sim.runner as R
    monkeypatch.setattr(R, "RUNS_DIR", tmp_path)
    rep = run_batch(
        persona_names=["beginner"],
        scenario_names=["ems_eco_basics"],
        seeds=[0],
        handle_turn=stub,
        max_turns=6,
        write_run=True,
        run_id="test_run",
    )
    assert rep["n_sessions"] == 1
    assert rep["n_setup_failed"] == 0
    assert (tmp_path / "test_run" / "sessions.jsonl").exists()
    assert (tmp_path / "test_run" / "all_transcripts.txt").exists()
