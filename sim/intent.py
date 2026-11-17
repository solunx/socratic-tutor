"""Python chooses turn intent from persona profile — the model only wording (later)."""
from __future__ import annotations

import random
from typing import Any


INTENTS = (
    "answer",           # attempt the question from the truth sheet
    "partial",          # incomplete / half
    "wrong_confident",  # misconception on the sheet
    "challenge",        # push back if tutor contradicts sheet
    "hint_request",
    "reveal_request",
    "stuck",
    "already_said",
    "off_topic",
    "rename_meta",      # lobby/meta style
)


def choose_intent(persona: dict[str, Any], turn_index: int, rng: random.Random) -> str:
    weights = dict(persona.get("intent_weights") or {})
    # Default balanced profile
    if not weights:
        weights = {
            "answer": 0.35,
            "partial": 0.15,
            "wrong_confident": 0.15,
            "challenge": 0.1,
            "hint_request": 0.08,
            "stuck": 0.07,
            "already_said": 0.05,
            "off_topic": 0.03,
            "reveal_request": 0.02,
        }
    # Early turns: more answers
    if turn_index < 2:
        weights = {**weights, "answer": weights.get("answer", 0.3) + 0.2, "off_topic": 0.0}
    keys = [k for k in weights if k in INTENTS and weights[k] > 0]
    if not keys:
        return "answer"
    w = [float(weights[k]) for k in keys]
    return rng.choices(keys, weights=w, k=1)[0]
