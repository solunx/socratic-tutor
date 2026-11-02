"""
BUG REPRODUCTIONS of the policy lag, taken from the real event log (vriend1, 2026-10-17).

Contract introduced by the fix (STEP 3 of the brief): BEFORE calling suggest_next_activity for
the current learner message, handle_turn writes

    active_session["turn_intent"]       in {answer, asks_hint, asks_reveal, asks_clarification,
                                            side_question, insight, challenges_tutor, frustration, off_topic}
    active_session["turn_correctness"]  in {correct, partial, incorrect, unknown}

for THAT message (from a tiny same-turn classifier call, phrase lists only as fallback),
and policy.suggest_next_activity() must prefer them over signals left over from the previous turn.

Each test is marked xfail(strict=True): today they fail (that IS the bug). When you fix the
bug the test XPASSes, strict mode turns that into a failure -> delete the marker. That is the
signal you are done with that case. Never loosen an assertion to make it pass.
"""
import pytest

import policy as P
from conftest import make_state

BUG = lambda why: pytest.mark.xfail(strict=True, reason=f"BUG-REPRO: {why}")


def pol(**active):
    return P.suggest_next_activity(make_state(**active))


def reason(p):
    return str(p.get("reason") or "").lower()


def test_loop_breaker_must_not_fire_on_an_answer_to_an_open_hint():
    p = pol(same_activity_streak=3, last_learner_signal="partial",
            turn_intent="answer", turn_correctness="correct")
    assert "must advance" not in reason(p) and "broke loop" not in reason(p)


def test_hint_request_gets_a_scaffold_not_a_new_situation():
    p = pol(must_advance=True, repeat_streak=3, turn_intent="asks_hint")
    assert p.get("scaffold") or p.get("full_teach")
    assert "new situation" not in reason(p)


def test_challenge_uses_the_correction_path_even_with_a_stale_correct_signal():
    p = pol(last_learner_signal="correct", last_verdict="correct", turn_intent="challenges_tutor")
    assert "correction" in reason(p)


def test_stale_confused_flag_does_not_leak_into_a_correct_answer():
    p = pol(confused_by_tutor=True, last_learner_signal="confused_by_tutor",
            turn_intent="answer", turn_correctness="correct")
    assert "confused" not in reason(p)


def test_explicit_reveal_request_takes_the_reveal_path():
    p = pol(turn_intent="asks_reveal")
    assert p.get("full_teach") is True


def test_wrong_answer_is_not_met_with_a_new_situation():
    p = pol(last_learner_signal="correct", last_verdict="correct", must_advance=True,
            turn_intent="answer", turn_correctness="incorrect")
    assert "new situation" not in reason(p)
