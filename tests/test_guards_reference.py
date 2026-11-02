"""
GREEN today. These test guards.py (the reference implementation) against the REAL session.
If you re-implement or change thresholds, these must stay green; add fixtures for other
languages/subjects instead of loosening them.
"""
import json
import pytest

import guards as G
from conftest import history_upto


def _assistant_turns(fx, ids):
    T = {t["idx"]: t for t in fx["turns"]}
    return [(i, T[i]) for i in ids if T[i]["role"] == "assistant"]


# ------------------------------------------------------------------ repeat gate
def test_gate_blocks_every_real_repeat(fx):
    for i, t in _assistant_turns(fx, fx["must_block"]):
        r = G.repeat_gate(t["text"], history_upto(fx, i))
        assert r.blocked, f"turn {i} should be blocked (score={r.score:.2f}, uses={r.detail})"


def test_gate_never_blocks_a_legitimate_turn(fx):
    """Includes: 'but why?' follow-up (34), same question + hint (64), 2nd hint (66), re-ask after objection (70)."""
    for i, t in _assistant_turns(fx, fx["must_allow"]):
        r = G.repeat_gate(t["text"], history_upto(fx, i))
        assert not r.blocked, f"turn {i} wrongly blocked: {r.reason} score={r.score:.2f}"


def test_threshold_margin_is_real(fx):
    """The repeat threshold must sit clearly above every legitimate score (no knife-edge tuning)."""
    worst_allowed = 0.0
    for i, t in _assistant_turns(fx, fx["must_allow"]):
        worst_allowed = max(worst_allowed, G.repeat_gate(t["text"], history_upto(fx, i)).score)
    assert worst_allowed <= 0.85 - 0.10, f"legit score {worst_allowed:.2f} too close to threshold 0.85"


def test_cosmetic_swap_is_caught_by_template_cap(fx):
    """Turns 38/46/50 swap the scenario words but keep the question template."""
    T = {t["idx"]: t for t in fx["turns"]}
    for i in (38, 46, 50):
        r = G.repeat_gate(T[i]["text"], history_upto(fx, i))
        assert r.blocked and r.reason in ("template_cap", "repeat")


def test_gate_ignores_unanswered_tutor_turns():
    h = [{"role": "assistant", "text": "Stel A en B. Welk is groter, en waarom?"}]   # learner never replied
    r = G.repeat_gate("Stel A en B. Welk is groter, en waarom?", h)
    assert not r.blocked


# ------------------------------------------------------------------ language agnostic
@pytest.mark.parametrize("q1,q2,legit", [
    # English: cosmetic repeat vs legitimate follow-up
    ("Imagine a cube and a sphere with equal volume. Which one has more surface area, and why?",
     "Imagine a sphere and a cube with equal volume. Which one has more surface area, and why?", False),
    ("Imagine a cube and a sphere with equal volume. Which one has more surface area, and why?",
     "Right, the sphere minimises it. But why does a smaller surface area matter for heat loss?", True),
    # French
    ("Imaginez un cube et une sphère de même volume. Lequel a la plus grande surface, et pourquoi ?",
     "Imaginez une sphère et un cube de même volume. Lequel a la plus grande surface, et pourquoi ?", False),
    ("Imaginez un cube et une sphère de même volume. Lequel a la plus grande surface, et pourquoi ?",
     "Exact. Que se passe-t-il alors avec la perte de chaleur d'un animal très petit ?", True),
])
def test_language_agnostic_repeat(q1, q2, legit):
    h = [{"role": "assistant", "text": "Bonjour, commençons."},
         {"role": "user", "text": "ok"},
         {"role": "assistant", "text": q1}, {"role": "user", "text": "mon avis / my answer"}]
    assert G.repeat_gate(q2, h).blocked == (not legit)


# ------------------------------------------------------------------ leak check
def _leak(fx, i, expected=None):
    T = {t["idx"]: t for t in fx["turns"]}
    exp = expected or fx["expected_answers"][str(i)]
    prior = [t["text"] for t in fx["turns"] if t["role"] == "assistant" and t["idx"] < i]
    pre = " ".join(G.split_sentences(T[i]["text"])[:-1])
    return G.leak_check(exp, prior, candidate_pre_question=pre)


def test_leak_detected_when_answer_was_already_given(fx):
    for i in fx["leak_positive"]:
        assert _leak(fx, i).leaked, f"turn {i}: answer was stated by the tutor earlier"


def test_no_leak_on_genuine_questions(fx):
    for i in fx["leak_negative"]:
        assert not _leak(fx, i).leaked, f"turn {i} is a genuine question"


def test_borderline_hint_is_allowed_but_documented(fx):
    b = fx["leak_borderline"]["idx"]
    r = _leak(fx, b)
    assert not r.leaked and 0.5 <= r.coverage < 0.75, r


@pytest.mark.xfail(strict=True, reason="KNOWN LIMIT: lexical only; the answer 'more energy' is implied, not stated (turn 70)")
def test_known_miss_paraphrased_leak(fx):
    i = fx["leak_known_miss"][0]
    assert _leak(fx, i, "meer energie nodig om de afstoting te overwinnen").leaked


# ------------------------------------------------------------------ response-field buffer
SAMPLE = {
    "routing": {"action": "continue", "response": "NOT THIS ONE (nested)"},
    "response": 'Hij zei: "ja" \\ en ça va — 😀\nTweede regel?',
    "assessment": {"verdict": "correct"},
}
RAW = json.dumps(SAMPLE, ensure_ascii=True)       # \uXXXX escapes + surrogate pair for the emoji


def test_buffer_returns_top_level_response_only():
    b = G.ResponseFieldBuffer()
    assert b.feed(RAW) == SAMPLE["response"]


@pytest.mark.parametrize("cut", range(1, len(RAW), 7))
def test_buffer_any_chunk_boundary(cut):
    b = G.ResponseFieldBuffer()
    first = b.feed(RAW[:cut])
    second = b.feed(RAW[cut:])
    assert (first or second) == SAMPLE["response"]


def test_buffer_releases_at_closing_quote_before_rest_arrives():
    """The point of the buffer: guards can run while assessment/session are still streaming."""
    end = RAW.index('"assessment"')
    b = G.ResponseFieldBuffer()
    assert b.feed(RAW[:end]) == SAMPLE["response"]
    assert b.closed


def test_buffer_incomplete_returns_none():
    b = G.ResponseFieldBuffer()
    assert b.feed(RAW[: RAW.index("Tweede") ]) is None
    assert not b.closed


def test_buffer_response_key_inside_other_value_is_ignored():
    raw = '{"note": "he typed \\"response\\": \\"x\\"", "response": "echt"}'
    assert G.ResponseFieldBuffer().feed(raw) == "echt"
