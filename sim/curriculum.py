"""Fixed turn scripts — highest signal, no random intent loops."""
from __future__ import annotations

from typing import Any


# Each step: intent used for metrics + how to build the message
def curriculum_steps(scenario_id: str) -> list[dict[str, str]] | None:
    """Return ordered steps {intent, source} or None if free persona mode.

    source: opening | knows | half | wrong | challenge | hint | stuck | custom
    """
    scripts: dict[str, list[dict[str, str]]] = {
        "ems_eco_basics": [
            {"intent": "answer", "source": "opening"},
            {"intent": "wrong_confident", "source": "wrong"},
            {"intent": "partial", "source": "half"},
            {"intent": "hint_request", "source": "hint"},
            {"intent": "answer", "source": "knows"},
            {"intent": "partial", "source": "half"},
            {"intent": "answer", "source": "knows"},
            {"intent": "challenge", "source": "challenge"},
        ],
        "ems_ev_boost_under_parent": [
            {"intent": "answer", "source": "opening"},
            {"intent": "answer", "source": "knows"},
            {"intent": "wrong_confident", "source": "wrong"},
            {"intent": "challenge", "source": "challenge"},
            {"intent": "partial", "source": "half"},
            {"intent": "answer", "source": "knows"},
            {"intent": "already_said", "source": "custom",
             "text": "Dat zei ik al over Boost versus Eco — leg de prioriteit uit met een getalvoorbeeld."},
            {"intent": "answer", "source": "knows"},
        ],
        "open_topic_warmtepomp": [
            {"intent": "answer", "source": "opening"},
            {"intent": "stuck", "source": "stuck"},
            {"intent": "partial", "source": "half"},
            {"intent": "hint_request", "source": "hint"},
            {"intent": "answer", "source": "custom",
             "text": "Dus de warmtepomp verplaatst warmte in plaats van die alleen elektrisch op te wekken?"},
            {"intent": "challenge", "source": "challenge"},
        ],
    }
    return scripts.get(scenario_id)


def message_for_step(
    step: dict[str, str],
    *,
    opening: str,
    sheet: dict[str, Any],
    tutor_last: str,
    persona: dict[str, Any],
    memory: Any,
    student_utterance_fn,
) -> str:
    src = step.get("source") or "knows"
    if src == "opening":
        return opening
    if src == "custom" and step.get("text"):
        return memory.mark(step["text"])
    intent_map = {
        "wrong": "wrong_confident",
        "half": "partial",
        "knows": "answer",
        "challenge": "challenge",
        "hint": "hint_request",
        "stuck": "stuck",
    }
    intent = intent_map.get(src, step.get("intent") or "answer")
    return student_utterance_fn(
        intent=intent,
        sheet=sheet,
        tutor_last=tutor_last,
        persona=persona,
        memory=memory,
    )
