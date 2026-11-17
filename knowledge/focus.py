"""Learner focus hints for policy + path boosts — no domain topic tables.

Focus is derived generically from the learner message (phrases after
over/about/naar/...) and from the active subject_id. There is no hardcoded
list of modes (eco, EV Boost, …).
"""
from __future__ import annotations

import re
from typing import Any

# "over X", "about X", "naar de X", "concerning X" — capture a short label
_FOCUS_PHRASE = re.compile(
    r"(?:over|about|concerning|naar(?:\s+de|\s+het)?|regarding)\s+"
    r"(?:de\s+|het\s+|the\s+|a\s+|an\s+)?"
    r"([A-Za-zÀ-öø-ÿ][\wÀ-öø-ÿ]+(?:[\s_-]+[\wÀ-öø-ÿ]+){0,4})",
    re.I,
)
_WORD = re.compile(r"[a-zA-Zà-öø-ÿÀ-ÖØ-ß0-9]{3,}")


def _slug(label: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (label or "").strip().lower())
    return s.strip("_")[:48] or "focus"


def detect_focus_mode(text: str) -> dict[str, str] | None:
    """Return {id, label} from a generic 'over/about …' phrase, else None."""
    raw = (text or "").strip()
    if not raw:
        return None
    m = _FOCUS_PHRASE.search(raw)
    if not m:
        return None
    label = re.sub(r"\s+", " ", m.group(1).strip())
    # Drop trailing glue words that are not part of a name
    label = re.sub(
        r"\s+(onder|in|binnen|from|with|met|van|and|en)\b.*$",
        "",
        label,
        flags=re.I,
    ).strip()
    if len(label) < 2:
        return None
    return {"id": _slug(label), "label": label}


def update_session_focus(state: dict, learner_text: str) -> dict[str, str] | None:
    """If the learner names a focus phrase, store it on active_session.focus_mode."""
    mode = detect_focus_mode(learner_text)
    if not mode:
        return None
    act = state.setdefault("active_session", {})
    act["focus_mode"] = mode
    return mode


def focus_policy_line(state: dict | None) -> str:
    """One line for policy.reason — keep questions inside the named focus."""
    act = (state or {}).get("active_session") or {}
    fm = act.get("focus_mode")
    if not isinstance(fm, dict) or not fm.get("label"):
        return ""
    label = str(fm["label"])
    return (
        f"FOCUS MODE: the learner is working on «{label}». "
        f"Do not ask about a different mode's rules unless they switch. "
        f"If they asked for a summary, give the summary; do not only offer a choice to summarise."
    )


def path_boost_tokens(state: dict | None, learner_text: str = "") -> list[str]:
    """
    Path fragments to prefer in local doc ranking.

    Sources (all generic):
    - subject_id segments
    - focus_mode id/label tokens
    - significant tokens from the current learner message
    """
    tokens: list[str] = []
    act = (state or {}).get("active_session") or {}
    sid = str(act.get("subject_id") or "").lower().replace("_", " ")
    for part in sid.split():
        if len(part) >= 3:
            tokens.append(part)

    fm = act.get("focus_mode") if isinstance(act.get("focus_mode"), dict) else None
    if fm:
        if fm.get("id"):
            tokens.append(str(fm["id"]).replace("_", ""))
        if fm.get("label"):
            for w in _WORD.findall(str(fm["label"])):
                tokens.append(w.lower())

    mode = detect_focus_mode(learner_text or "")
    if mode:
        tokens.append(mode["id"].replace("_", ""))
        for w in _WORD.findall(mode.get("label") or ""):
            tokens.append(w.lower())

    for w in _WORD.findall(learner_text or ""):
        wl = w.lower()
        if len(wl) >= 4:
            tokens.append(wl)

    out: list[str] = []
    for t in tokens:
        t = re.sub(r"[^a-z0-9]+", "", t.lower())
        if t and t not in out:
            out.append(t)
    return out
