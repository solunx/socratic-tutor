"""
Central intent / phrase lists for the Socratic tutor.

Domain-agnostic: no subject-specific content. Used by core.handle_turn and
tutor routing helpers so NL and EN (and later FR) can grow in one place.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Learner signals → session flags (core.handle_turn)
# ---------------------------------------------------------------------------

# "We already did this" → must_advance + repeat_streak
REPEAT_ADVANCE_PHRASES = (
    "dat hebben we",
    "hebben we al",
    "al gedaan",
    "al uitgerekend",
    "al toegelicht",
    "al uitgelegd",
    "daarnet gedaan",
    "net gedaan",
    "duizend keer",
    "al 10 keer",
    "al gezegd",
    "geen correctie nodig",
    "omg dat heb ik",
    "we had that",
    "already did",
    "already covered",
    "just did that",
)

# Skip aggressive resume / force progress
SKIP_RESUME_PHRASES = (
    "niet belangrijk",
    "wil geen",
    "wil niet",
    "ik wil begrijpen",
    "ik wil meer",
    "liever over",
    "overstappen",
    "iets anders",
    "not important",
    "something else",
)

# "I don't know" → productive struggle (scaffold), NOT full lecture
STUCK_PHRASES = (
    "geen idee",
    "geen idd",
    "weet ik niet",
    "weet het niet",
    "ik weet het niet",
    "no idea",
    "don't know",
    "i don't know",
    "idk",
    "geen flauw idee",
)

# Explicit "teach me / explain" → full teach (policy + session flags)
FULL_TEACH_PHRASES = (
    "leg uit",
    "leg het uit",
    "leg me uit",
    "wat bedoel je",
    "ik snap je vraag niet",
    "ik begrijp de vraag niet",
    "formule",
    "langzaam",
    "stap voor stap",
    "explain",
    "what do you mean",
    "i don't understand the question",
    "slow down",
)

# ---------------------------------------------------------------------------
# Subject list queries
# ---------------------------------------------------------------------------

LIST_SUBJECTS_PATTERNS = (
    r"welke onderwerpen",
    r"welke subjects",
    r"wat voor onderwerpen",
    r"welke topics",
    r"toon (?:mijn )?onderwerpen",
    r"lijst (?:van )?onderwerpen",
    r"which subjects",
    r"what subjects",
    r"list (?:my )?subjects",
    r"show (?:my )?topics",
)

# ---------------------------------------------------------------------------
# Continue / switch subject (natural language)
# ---------------------------------------------------------------------------

SUBJECT_INTENT_PATTERNS = (
    r"(?:ik wil\s+)?(?:graag\s+)?(?:even\s+)?over\s+(.+?)\s+verder(?:\s+leren)?",
    r"(?:ik wil\s+)?(?:graag\s+)?(?:even\s+)?(?:verder|door)(?:gaan)?(?:\s+leren)?(?:\s+(?:met|over|over het onderwerp|van))\s+(.+)",
    r"(?:waar was ik (?:met|bij|over))\s+(.+)",
    r"(?:switch|wissel)(?:\s+(?:naar|over naar))?\s+(.+)",
    r"(?:open|hervat|resume)\s+(.+)",
    r"(?:ik wil\s+)?(?:graag\s+)?(?:even\s+)?(?:verder\s+)?leren\s+(?:over|van|met)\s+(.+)",
    r"(?:laten we\s+)?(?:graag\s+)?(?:verder\s+)?(?:gaan\s+)?(?:met|over)\s+(.+)",
    r"(?:ga\s+)?(?:verder\s+)?(?:met|over)\s+(.+)",
    r"^(?:verder\s+(?:met|over))\s+(.+)",
    r"^over\s+(.+?)\s+verder",
    # English
    r"(?:let'?s\s+)?(?:continue|resume|go on)\s+(?:with|on)\s+(.+)",
    r"(?:i want to\s+)?(?:learn|continue)\s+(?:about|with)\s+(.+)",
    r"(?:switch to|open)\s+(.+)",
)


def matches_any(text: str, phrases: tuple[str, ...] | list[str]) -> bool:
    low = (text or "").strip().lower()
    if not low:
        return False
    return any(p in low for p in phrases)


def is_list_subjects_query(user_input: str) -> bool:
    text = (user_input or "").strip().lower()
    for pat in LIST_SUBJECTS_PATTERNS:
        if re.search(pat, text):
            return True
    return False


def detect_subject_topic(user_input: str) -> str | None:
    """Return extracted topic string if user intends to continue/switch subject."""
    text = (user_input or "").strip().lower()
    for pat in SUBJECT_INTENT_PATTERNS:
        m = re.search(pat, text)
        if m:
            topic = (m.group(1) or "").strip(" .?!")
            if topic and len(topic) > 1:
                return topic
    return None
