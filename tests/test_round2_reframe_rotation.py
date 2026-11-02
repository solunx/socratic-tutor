"""Round 2: reframe on frustration, focus rotation after clean streak, abbreviation guard."""
from __future__ import annotations

import guards as G
import policy as P


def test_abbreviations_do_not_split_final_question():
    samples = [
        "Vergelijk dit t.o.v. de andere. Wat is groter?",
        "Bijv. waterstof en helium. Welke fuseert eerst?",
        "Zie e.g. de CNO-cyclus. Waarom schaalt die sterker?",
    ]
    for s in samples:
        fq = G.final_question(s)
        sq = G.setup_and_question(s)
        assert fq and fq.endswith("?"), s
        # setup+question must keep the abbreviation text, not drop the lead-in
        assert "t.o.v." in sq or "Bijv." in sq or "e.g." in sq or "CNO" in sq, (s, sq)


def test_frustration_policy_returns_reframe():
    state = {
        "active_session": {
            "active": True,
            "subject_id": "demo",
            "current_concept_id": "c1",
            "current_activity": "APPLICATION",
            "turn_intent": "frustration",
            "open_threads": [],
        },
        "subjects": {"demo": {"concepts": {"c1": {"title": "One"}}}},
    }
    p = P.suggest_next_activity(state)
    assert p.get("reframe") is True
    assert p.get("move") == "reframe"
    assert p.get("force") is True
    reason = (p.get("reason") or "").lower()
    assert "reframe" in reason
    assert "do not ask another quiz" in reason or "summarise" in reason


def test_correct_streak_rotates_focus():
    state = {
        "active_session": {
            "active": True,
            "subject_id": "demo",
            "current_concept_id": "c1",
            "current_activity": "APPLICATION",
            "correct_streak": 4,
            "turn_intent": "answer",
            "turn_correctness": "correct",
            "open_threads": [
                {"status": "open", "q": "old gap question about c1?"}
            ],
        },
        "subjects": {
            "demo": {
                "concepts": {
                    "c1": {
                        "title": "One",
                        "knowledge": 0.6,
                        "retrieval": 0.6,
                        "application": 0.5,
                        "explanation": 0.5,
                        "transfer": 0.4,
                    },
                    "c2": {
                        "title": "Two",
                        "knowledge": 0.15,
                        "retrieval": 0.1,
                        "application": 0.1,
                        "explanation": 0.1,
                        "transfer": 0.0,
                    },
                }
            }
        },
    }
    p = P.suggest_next_activity(state)
    assert p.get("move") == "rotate"
    assert p.get("concept_id") == "c2"
    assert state["active_session"].get("rotation_pending") is True
    assert state["active_session"].get("correct_streak") == 0
    parked = [
        t
        for t in state["active_session"]["open_threads"]
        if t.get("status") == "parked"
    ]
    assert len(parked) == 1
