#!/usr/bin/env python3

import json
import math
import re
import requests
from pathlib import Path
from datetime import datetime, timedelta


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

SYSTEM_PROMPT_FILE = BASE_DIR / "system_prompt.md"
STATE_FILE = BASE_DIR / "learner_state.json"
CATALOG_FILE = BASE_DIR / "misconception_catalog.json"

# Native Ollama chat API — honors think:false more reliably than /v1 for Qwen3.8
OLLAMA_HOST = "http://localhost:11434"
MODEL_URL = f"{OLLAMA_HOST}/api/chat"

# Exact name from: ollama list
MODEL_NAME = "qwen3.8:27b"  # change to gemma4:e4b / qwen2.5:14b as needed

TEMPERATURE = 0.4
REQUEST_TIMEOUT = 900

# Thinking control for this tutor only (per request).
# For qwen3.8:27b supported values are typically: false, "low", "medium", "xhigh"
# false / "none" = no chain-of-thought (fastest)
THINK = False  # set to "low" if you want a little reasoning without full depth

# Stream tokens from Ollama and live-print the learner-facing "response" field.
# Structured JSON is still collected in full for routing/assessment.
STREAM = True

# Maximum amount of conversation kept in the active context.
# This prevents the prompt from growing forever.
MAX_CONVERSATION_MESSAGES = 12  # keep prompt lean for speed

# Session target. The tutor may continue longer if useful.
DEFAULT_SESSION_MINUTES = 30


# ============================================================
# TERMINAL UI
# ============================================================

USE_COLOR = True

RESET = "\033[0m"
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
GRAY = "\033[90m"
RED = "\033[91m"
MAGENTA = "\033[95m"
BOLD = "\033[1m"


def c(text, colour):
    if not USE_COLOR:
        return text
    return f"{colour}{text}{RESET}"


def divider():
    print(
        c(
            "────────────────────────────────────────────────────────────",
            GRAY
        )
    )


def show_you_prompt():
    divider()
    print(c("YOU", BOLD + CYAN))
    divider()


def show_tutor(answer, already_streamed=False):
    """
    Print the learner-facing tutor message.
    If already_streamed=True, only print a trailing blank line (body was live).
    """
    if already_streamed:
        print()
        return
    divider()
    print(c("TUTOR", BOLD + GREEN))
    divider()
    print()
    print((answer or "").strip())
    print()


def show_status(text):
    print(c(f"[{text}]", GRAY))


# ============================================================
# DEFAULT STATE
# ============================================================

def default_state():
    return {
        "version": 3,

        "learner": {
            "id": "george",
            "preferences": {
                "language": "nl",
                "tutor_style": "socratic",
                "session_length_minutes": DEFAULT_SESSION_MINUTES
            }
        },

        "active_session": {
            "active": False,
            "subject_id": None,
            "started_at": None,
            "last_activity": None,
            "question_number": 0,
            "hint_count": 0,
            "current_concept_id": None,
            "current_activity": None,
            "resume_instruction": None,
            "pending_goal": False,
            "just_resumed": False,
            # Working memory for continuity (not full history)
            "session_thread": None,      # 2–4 sentences: established / open
            "last_tutor_question": None,  # anti-repeat
            "recent_questions": [],       # last few questions (max 5)
        },

        "subjects": {},

        # Taxonomy + auto-curation (learner never sees this)
        "meta": {
            "pending_catalog": [],   # MC promotion candidates
            "taxonomy_events": [],   # link/merge proposals log (last N)
        },

        "review_queue": [],

        "history": []
    }


# ============================================================
# FILE / STATE MANAGEMENT
# ============================================================

def atomic_save_json(path, data):
    """
    Write JSON safely using a temporary file and atomic replace.
    """

    tmp = path.with_suffix(path.suffix + ".tmp")

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False
        )
        f.write("\n")

    tmp.replace(path)


def save_state(state):
    atomic_save_json(STATE_FILE, state)


def migrate_state(old):
    """
    Convert older learner_state formats into v2 where possible.
    """

    new = default_state()

    if not isinstance(old, dict):
        return new

    # Learner
    if isinstance(old.get("learner"), dict):
        new["learner"].update(old["learner"])

        old_preferences = old["learner"].get("preferences")

        if isinstance(old_preferences, dict):
            new["learner"]["preferences"].update(
                old_preferences
            )

    # Already v2
    if "subjects" in old:
        if isinstance(old["subjects"], dict):
            new["subjects"] = old["subjects"]

    # Old v1 concepts
    elif isinstance(old.get("concepts"), dict):

        if old["concepts"]:

            new["subjects"]["general"] = {
                "title": "General",
                "created_at": datetime.now().isoformat(),
                "concepts": {}
            }

            for name, value in old["concepts"].items():

                if not isinstance(value, dict):
                    value = {}

                new["subjects"]["general"]["concepts"][name] = {
                    "knowledge": value.get(
                        "knowledge",
                        value.get("status", 0.0)
                    ),
                    "retrieval": value.get(
                        "retrieval",
                        0.0
                    ),
                    "application": value.get(
                        "application",
                        0.0
                    ),
                    "explanation": value.get(
                        "explanation",
                        0.0
                    ),
                    "transfer": value.get(
                        "transfer",
                        0.0
                    ),
                    "misconceptions": value.get(
                        "misconceptions",
                        []
                    ),
                    "evidence": value.get(
                        "evidence",
                        []
                    ),
                    "last_practiced": value.get(
                        "last_practiced"
                    ),
                    "next_review": value.get(
                        "next_review"
                    )
                }

    # Active session
    if isinstance(old.get("active_session"), dict):
        new["active_session"].update(
            old["active_session"]
        )

    # Old session format
    elif isinstance(old.get("session"), dict):
        old_session = old["session"]

        for key in [
            "question_number",
            "questions_asked"
        ]:
            if key in old_session:
                new["active_session"]["question_number"] = (
                    old_session[key]
                )

        if "current_concept" in old_session:
            new["active_session"]["current_concept_id"] = (
                old_session["current_concept"]
            )

    # History
    if isinstance(old.get("history"), list):
        new["history"] = old["history"]

    # Review queue
    if isinstance(old.get("review_queue"), list):
        new["review_queue"] = old["review_queue"]

    # Meta (v3)
    if isinstance(old.get("meta"), dict):
        new.setdefault("meta", {})
        new["meta"].update({
            k: v for k, v in old["meta"].items()
            if k in ("pending_catalog", "taxonomy_events")
        })
        if not isinstance(new["meta"].get("pending_catalog"), list):
            new["meta"]["pending_catalog"] = []
        if not isinstance(new["meta"].get("taxonomy_events"), list):
            new["meta"]["taxonomy_events"] = []
    else:
        new["meta"] = {
            "pending_catalog": [],
            "taxonomy_events": [],
        }

    new["version"] = 3
    normalize_taxonomy_fields(new)
    return new


def normalize_taxonomy_fields(state):
    """
    Ensure every subject/concept has hierarchy + alias fields (v3).
    Safe to call repeatedly. Does not change learning scores.
    """
    state.setdefault("meta", {
        "pending_catalog": [],
        "taxonomy_events": [],
    })
    state["meta"].setdefault("pending_catalog", [])
    state["meta"].setdefault("taxonomy_events", [])

    for sid, sub in (state.get("subjects") or {}).items():
        if not isinstance(sub, dict):
            continue
        sub.setdefault("parent_id", None)
        sub.setdefault("related", [])
        sub.setdefault("aliases", [])
        if not isinstance(sub["related"], list):
            sub["related"] = []
        if not isinstance(sub["aliases"], list):
            sub["aliases"] = []
        # Drop invalid parent (self or missing)
        pid = sub.get("parent_id")
        if pid == sid or (pid and pid not in state["subjects"]):
            sub["parent_id"] = None
        for cid, concept in (sub.get("concepts") or {}).items():
            if not isinstance(concept, dict):
                continue
            concept.setdefault("parent_concept_id", None)
            concept.setdefault("aliases", [])
            concept.setdefault("prerequisites", [])
            if not isinstance(concept["aliases"], list):
                concept["aliases"] = []
            if not isinstance(concept["prerequisites"], list):
                concept["prerequisites"] = []
            pc = concept.get("parent_concept_id")
            if pc == cid or (pc and pc not in sub.get("concepts", {})):
                concept["parent_concept_id"] = None


def load_state():
    if not STATE_FILE.exists():
        state = default_state()
        save_state(state)
        return state

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            old = json.load(f)

    except Exception as e:
        print(
            c(
                f"WARNING: Could not read learner_state.json: {e}",
                RED
            )
        )

        backup = STATE_FILE.with_suffix(
            ".corrupt-" +
            datetime.now().strftime("%Y%m%d-%H%M%S") +
            ".json"
        )

        try:
            STATE_FILE.rename(backup)
            print(
                f"Broken state backed up to: {backup}"
            )
        except Exception:
            pass

        state = default_state()
        save_state(state)
        return state

    state = migrate_state(old)

    save_state(state)

    return state


# ============================================================
# SYSTEM PROMPT
# ============================================================

def load_system_prompt():

    if not SYSTEM_PROMPT_FILE.exists():
        return """
You are a Socratic learning tutor.
Teach through questions, retrieval, application and guided discovery.
Do not give the answer immediately when the learner can discover it.
"""

    return SYSTEM_PROMPT_FILE.read_text(
        encoding="utf-8"
    )


# ============================================================
# SUBJECT / CONCEPT HELPERS
# ============================================================

INVALID_IDS = {
    "", "none", "null", "unknown", "n/a", "na", "undefined",
    "n_a", "not_applicable", "geen", "onbekend"
}


def normalize_id(text):
    """Normalize text to a safe identifier. Returns empty string if invalid."""
    if text is None:
        return ""

    text = str(text).strip().lower()

    text = re.sub(
        r"[^a-z0-9à-ÿ]+",
        "_",
        text
    )

    text = text.strip("_")[:80]

    if text in INVALID_IDS:
        return ""

    return text


def is_valid_id(value):
    """Return True only if the value is a usable subject/concept id."""
    normalized = normalize_id(value)
    return bool(normalized) and normalized not in INVALID_IDS


def resolve_subject_id(state, subject_id, title=None):
    """
    Map id/title/alias to an existing subject_id when possible.
    Prevents duplicate subjects for the same topic (aliases / near titles).
    """
    sid = normalize_id(subject_id)
    subjects = state.get("subjects") or {}
    if sid and sid in subjects:
        return sid

    # Match by alias or title
    candidates = []
    if title:
        candidates.append(str(title).strip().lower())
    if sid:
        candidates.append(sid.replace("_", " "))
        candidates.append(sid)

    for existing_id, sub in subjects.items():
        if not isinstance(sub, dict):
            continue
        title_l = str(sub.get("title") or "").strip().lower()
        aliases = [str(a).strip().lower() for a in (sub.get("aliases") or [])]
        alias_ids = [normalize_id(a) for a in (sub.get("aliases") or [])]
        for c in candidates:
            if not c:
                continue
            c_norm = normalize_id(c)
            if c == title_l or c in aliases:
                return existing_id
            if c_norm and (c_norm == existing_id or c_norm in alias_ids):
                return existing_id
    return sid or ""


def ensure_subject(state, subject_id, title=None, parent_id=None):
    """
    Ensure a subject exists. Never create subjects from invalid or empty IDs.
    Resolves aliases to existing subjects to limit wildgroei.
    Returns the final subject_id (or 'general' as last resort).
    """
    resolved = resolve_subject_id(state, subject_id, title=title)
    subject_id = resolved if resolved else normalize_id(subject_id)

    if not subject_id:
        subject_id = "general"

    if subject_id not in state["subjects"]:
        clean_title = title
        if clean_title:
            clean_title = str(clean_title).strip()
            if len(clean_title) > 60 or clean_title.count(" ") > 6:
                clean_title = subject_id.replace("_", " ").title()
        else:
            clean_title = subject_id.replace("_", " ").title()

        state["subjects"][subject_id] = {
            "title": clean_title,
            "goal": None,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "parent_id": None,
            "related": [],
            "aliases": [],
            "concepts": {}
        }

    sub = state["subjects"][subject_id]
    sub.setdefault("parent_id", None)
    sub.setdefault("related", [])
    sub.setdefault("aliases", [])

    # Optional parent link (must exist, no cycles)
    if parent_id:
        pid = normalize_id(parent_id)
        if pid and pid in state["subjects"] and pid != subject_id:
            # prevent trivial cycle parent→child→parent
            if state["subjects"][pid].get("parent_id") != subject_id:
                sub["parent_id"] = pid

    # If title looks like an alternate name, keep as alias
    if title:
        t = str(title).strip()
        if t and t.lower() != str(sub.get("title") or "").lower():
            if len(t) <= 60 and t.count(" ") <= 6:
                aliases = sub.setdefault("aliases", [])
                if t not in aliases and normalize_id(t) != subject_id:
                    aliases.append(t)

    return subject_id


def ensure_concept(state, subject_id, concept_id):
    """
    Ensure a concept exists under a subject.
    Returns the concept_id on success, or None if the concept_id is invalid.
    Never creates a concept named 'none', 'null', etc.
    """
    subject_id = ensure_subject(state, subject_id)

    concept_id = normalize_id(concept_id)

    if not concept_id:
        return None

    concepts = state["subjects"][subject_id]["concepts"]

    # Resolve via concept aliases within subject
    for existing_id, cpt in concepts.items():
        if not isinstance(cpt, dict):
            continue
        aliases = [normalize_id(a) for a in (cpt.get("aliases") or [])]
        if concept_id in aliases:
            concept_id = existing_id
            break

    if concept_id not in concepts:
        concepts[concept_id] = {
            "title": concept_id.replace("_", " ").title(),
            "knowledge": 0.0,
            "retrieval": 0.0,
            "application": 0.0,
            "explanation": 0.0,
            "transfer": 0.0,
            "misconceptions": [],
            "evidence": [],
            "last_practiced": None,
            "next_review": None,
            "review_count": 0,
            # FSRS fields
            "stability": 0.5,
            "difficulty": 5.0,
            "reps": 0,
            "lapses": 0,
            "fsrs_state": "new",
            "last_review": None,
            "last_grade": None,
            # Taxonomy
            "parent_concept_id": None,
            "aliases": [],
            "prerequisites": [],
        }
    else:
        concepts[concept_id].setdefault("parent_concept_id", None)
        concepts[concept_id].setdefault("aliases", [])
        concepts[concept_id].setdefault("prerequisites", [])

    return concept_id


def find_matching_subjects(state, query, limit=5):
    """
    Fuzzy match a free-form query against existing subject ids and titles.
    Returns a list of (subject_id, score) sorted by score descending.
    """
    if not query or not state.get("subjects"):
        return []

    q = normalize_id(query)
    q_raw = query.strip().lower()
    results = []

    for sid, subject in state["subjects"].items():
        title = str(subject.get("title", "")).lower()
        title_norm = normalize_id(title)
        score = 0

        # Exact id match
        if q and q == sid:
            score = 100
        # Id contains query or vice versa
        elif q and (q in sid or sid in q):
            score = 80
        # Title exact / contains
        elif q_raw and q_raw == title:
            score = 90
        elif q_raw and (q_raw in title or title in q_raw):
            score = 70
        elif title_norm and q and (q in title_norm or title_norm in q):
            score = 60
        # Token overlap (simple)
        else:
            q_tokens = set(re.findall(r"[a-z0-9à-ÿ]+", q_raw))
            t_tokens = set(re.findall(r"[a-z0-9à-ÿ]+", title))
            if q_tokens and t_tokens:
                overlap = len(q_tokens & t_tokens)
                if overlap:
                    score = 40 + overlap * 10

        if score > 0:
            results.append((sid, score))

    results.sort(key=lambda x: (-x[1], x[0]))
    return results[:limit]


def clean_topic_phrase(topic):
    """Strip filler words so 'agent verder leren' → 'agent'."""
    topic = topic.strip(" .?!")
    # Remove trailing intent words
    topic = re.sub(
        r"\s+(verder(\s+leren)?|leren|doen|gaan|alsjeblieft|please|"
        r"nu|weer|vandaag|graag|even|nog|eens)$",
        "",
        topic,
        flags=re.IGNORECASE
    )
    # Remove leading filler
    topic = re.sub(
        r"^(?:gaan\s+)?(?:over\s+)?(?:het\s+)?(?:de\s+)?",
        "",
        topic,
        flags=re.IGNORECASE
    )
    # Remove leading "even" / "graag"
    topic = re.sub(r"^(?:even\s+|graag\s+)+", "", topic, flags=re.IGNORECASE)
    return topic.strip(" .?!")


def detect_subject_intent(user_input):
    """
    Detect natural-language intent to continue / switch subject.
    Returns the extracted topic string or None.
    """
    text = user_input.strip().lower()

    patterns = [
        # "ik wil (graag) (even) over X verder (leren)"
        r"(?:ik wil\s+)?(?:graag\s+)?(?:even\s+)?over\s+(.+?)\s+verder(?:\s+leren)?",
        # "ik wil (graag) verder (leren) (over/met) X"
        r"(?:ik wil\s+)?(?:graag\s+)?(?:even\s+)?(?:verder|door)(?:gaan)?(?:\s+leren)?(?:\s+(?:met|over|over het onderwerp|van))\s+(.+)",
        # "waar was ik met/bij/over X"
        r"(?:waar was ik (?:met|bij|over))\s+(.+)",
        # "switch/wissel naar X"
        r"(?:switch|wissel)(?:\s+(?:naar|over naar))?\s+(.+)",
        # "open/hervat/resume X"
        r"(?:open|hervat|resume)\s+(.+)",
        # "ik wil (graag) (verder) leren over/van/met X"
        r"(?:ik wil\s+)?(?:graag\s+)?(?:even\s+)?(?:verder\s+)?leren\s+(?:over|van|met)\s+(.+)",
        # "laten we (verder) (gaan) met/over X"
        r"(?:laten we\s+)?(?:graag\s+)?(?:verder\s+)?(?:gaan\s+)?(?:met|over)\s+(.+)",
        # "ga (verder) met/over X"
        r"(?:ga\s+)?(?:verder\s+)?(?:met|over)\s+(.+)",
        # bare "verder met/over X"
        r"^(?:verder\s+(?:met|over))\s+(.+)",
        # "over X verder"
        r"^over\s+(.+?)\s+verder",
    ]

    for pat in patterns:
        m = re.search(pat, text)
        if m:
            topic = clean_topic_phrase(m.group(1))
            if topic and len(topic) > 1:
                return topic

    return None


def is_list_subjects_query(user_input):
    """Detect natural language questions about available subjects."""
    text = user_input.strip().lower()
    patterns = [
        r"welke onderwerpen",
        r"welke subjects",
        r"wat voor onderwerpen",
        r"welke topics",
        r"toon (?:mijn )?onderwerpen",
        r"lijst (?:van )?onderwerpen",
        r"overzicht (?:van )?onderwerpen",
        r"wat heb ik (?:al )?geleerd",
        r"waar kan ik (?:aan|over) verder",
    ]
    return any(re.search(p, text) for p in patterns)


# ============================================================
# REVIEW SCHEDULING
# ============================================================

def average_mastery(concept):

    dimensions = [
        "knowledge",
        "retrieval",
        "application",
        "explanation",
        "transfer"
    ]

    values = []

    for dimension in dimensions:

        value = concept.get(
            dimension,
            0.0
        )

        if isinstance(value, (int, float)):
            values.append(
                max(0.0, min(1.0, float(value)))
            )

    if not values:
        return 0.0

    return sum(values) / len(values)


def has_open_misconception(concept):

    for item in concept.get(
        "misconceptions",
        []
    ):

        if isinstance(item, dict):

            if item.get(
                "status",
                "open"
            ) != "resolved":
                return True

        elif isinstance(item, str):
            return True

    return False


def open_misconceptions(concept, severity=None):
    """Return open misconception dicts; optional severity filter (high/medium/low)."""
    out = []
    for item in concept.get("misconceptions") or []:
        if not isinstance(item, dict):
            continue
        if item.get("status", "open") == "resolved":
            continue
        if severity and str(item.get("severity", "")).lower() != severity.lower():
            continue
        out.append(item)
    return out


def get_focus_concept(state):
    """Return (subject_id, concept_id, concept_dict) for the active focus, or (None, None, None)."""
    active = state.get("active_session") or {}
    sid = active.get("subject_id")
    if not sid or sid not in state.get("subjects", {}):
        return None, None, None
    subject = state["subjects"][sid]
    concepts = subject.get("concepts") or {}
    cid = active.get("current_concept_id")
    if cid and cid in concepts:
        return sid, cid, concepts[cid]
    # Fall back to most recently practiced concept
    best_cid, best_t = None, ""
    for c_id, c in concepts.items():
        lp = c.get("last_practiced") or ""
        if lp >= best_t:
            best_t = lp
            best_cid = c_id
    if best_cid:
        return sid, best_cid, concepts[best_cid]
    return sid, None, None


def suggest_next_activity(state):
    """
    Deterministic policy: choose the next learning activity from state.
    Returns dict: activity, concept_id, subject_id, reason, force (bool).

    Priority (see ROADMAP.md):
      1. Due review for current subject
      2. Open high-severity misconception on focus concept
      3. Weak hard prerequisite (deeper gap first)
      4. Just resumed → RETRIEVAL
      5. Weakest dimension on focus concept below threshold
      6. Any open misconception → MISCONCEPTION_CHECK
      7. Free (LLM may choose)
    """
    active = state.get("active_session") or {}
    sid, cid, concept = get_focus_concept(state)

    if not active.get("active") or not sid:
        return {
            "activity": None,
            "concept_id": None,
            "subject_id": None,
            "reason": "no active teaching session",
            "force": False,
        }

    # 1) Due reviews for this subject
    now = datetime.now().isoformat()
    for item in state.get("review_queue") or []:
        if item.get("subject_id") != sid:
            continue
        due = item.get("next_review") or item.get("due") or ""
        if due and due <= now:
            return {
                "activity": "REVIEW",
                "concept_id": item.get("concept_id") or cid,
                "subject_id": sid,
                "reason": f"due review for concept {item.get('concept_id') or cid}",
                "force": True,
            }

    # 2) High-severity open misconception on focus concept
    if concept:
        high = open_misconceptions(concept, severity="high")
        if high:
            desc = str(high[0].get("description", ""))[:60]
            return {
                "activity": "MISCONCEPTION_CHECK",
                "concept_id": cid,
                "subject_id": sid,
                "reason": f"open high-severity MC: {desc}",
                "force": True,
            }

    # 3) Weak hard prerequisite — teach deeper gap first
    if concept and cid:
        weak_prereq = find_weak_prerequisite(state, sid, cid)
        if weak_prereq:
            pre_id, pre_avg, strength = weak_prereq
            return {
                "activity": "RETRIEVAL",
                "concept_id": pre_id,
                "subject_id": sid,
                "reason": (
                    f"weak {strength} prerequisite {pre_id} "
                    f"(avg={pre_avg:.2f}) before {cid}"
                ),
                "force": strength == "hard",
            }

    # 4) Just resumed
    if active.get("just_resumed"):
        return {
            "activity": "RETRIEVAL",
            "concept_id": cid,
            "subject_id": sid,
            "reason": "session just resumed — retrieval first",
            "force": True,
        }

    # 5) Weakest dimension on focus concept
    if concept:
        scores = {}
        for d in DIMENSIONS:
            v = clamp(concept.get(d))
            scores[d] = 0.0 if v is None else v
        weakest = min(scores, key=scores.get)
        if scores[weakest] < WEAK_DIM_THRESHOLD:
            act = DIM_TO_ACTIVITY.get(weakest, "RETRIEVAL")
            return {
                "activity": act,
                "concept_id": cid,
                "subject_id": sid,
                "reason": f"weak dimension {weakest}={scores[weakest]:.2f} → {act}",
                "force": False,
            }

    # 5) Any open misconception
    if concept and has_open_misconception(concept):
        return {
            "activity": "MISCONCEPTION_CHECK",
            "concept_id": cid,
            "subject_id": sid,
            "reason": "open misconception on focus concept",
            "force": False,
        }

    # 6) Free choice
    return {
        "activity": None,
        "concept_id": cid,
        "subject_id": sid,
        "reason": "no strong policy signal — LLM may choose activity",
        "force": False,
    }


# ------------------------------------------------------------
# FSRS-inspired spaced repetition (Phase D)
# Pure Python; no external package. Desired retention ~90%.
# Grade: 1=Again, 2=Hard, 3=Good, 4=Easy
# ------------------------------------------------------------

FSRS_REQUEST_RETENTION = 0.90
FSRS_MAX_INTERVAL_DAYS = 365.0
FSRS_MIN_STABILITY = 0.1


def ensure_fsrs_fields(concept):
    """Initialize FSRS fields; migrate coarse mastery → rough stability if needed."""
    if concept.get("stability") is None:
        mastery = average_mastery(concept)
        # Seed stability from existing mastery (days)
        if mastery >= 0.85:
            concept["stability"] = 14.0
        elif mastery >= 0.70:
            concept["stability"] = 7.0
        elif mastery >= 0.50:
            concept["stability"] = 3.0
        elif mastery >= 0.30:
            concept["stability"] = 1.5
        else:
            concept["stability"] = 0.5
    if concept.get("difficulty") is None:
        # Higher difficulty = harder card (1..10)
        mastery = average_mastery(concept)
        concept["difficulty"] = round(max(1.0, min(10.0, 10.0 - mastery * 8.0)), 2)
    concept.setdefault("reps", int(concept.get("review_count") or 0))
    concept.setdefault("lapses", 0)
    concept.setdefault("fsrs_state", "new" if concept.get("reps", 0) == 0 else "review")


def grade_from_assessment(concept, assessment):
    """
    Map tutor assessment → FSRS grade 1..4.
    1 Again, 2 Hard, 3 Good, 4 Easy.
    """
    if has_open_misconception(concept):
        high = open_misconceptions(concept, severity="high")
        if high:
            return 1  # Again

    dims = (assessment or {}).get("dimensions") or {}
    scores = []
    for d in DIMENSIONS:
        v = clamp(dims.get(d))
        if v is not None:
            scores.append(v)
    if not scores:
        # fall back to stored mastery
        m = average_mastery(concept)
        if m < 0.30:
            return 1
        if m < 0.50:
            return 2
        if m < 0.80:
            return 3
        return 4

    avg = sum(scores) / len(scores)
    # penalize if transfer/explanation still zero while others high
    t = clamp(dims.get("transfer"))
    e = clamp(dims.get("explanation"))
    if avg < 0.25:
        return 1
    if avg < 0.45:
        return 2
    if avg >= 0.75 and (t is None or t >= 0.4) and (e is None or e >= 0.4):
        return 4
    return 3


def _next_interval_days(stability, retention=FSRS_REQUEST_RETENTION):
    """Days until retrievability falls to target retention: I = S * ln(R) / ln(0.9)."""
    if stability <= 0:
        return 0.5
    # When R=0.9, interval ≈ S
    import math
    try:
        interval = stability * (math.log(retention) / math.log(0.9))
    except (ValueError, ZeroDivisionError):
        interval = stability
    return max(0.5, min(FSRS_MAX_INTERVAL_DAYS, interval))


def schedule_fsrs(concept, grade):
    """
    Update stability/difficulty/next_review from grade (1..4).
    Inspired by FSRS-4.5 scheduling dynamics (simplified, dependency-free).
    """
    ensure_fsrs_fields(concept)
    import math

    grade = int(grade)
    if grade < 1:
        grade = 1
    if grade > 4:
        grade = 4

    S = float(concept["stability"])
    D = float(concept["difficulty"])
    reps = int(concept.get("reps") or 0)
    lapses = int(concept.get("lapses") or 0)

    # Elapsed days since last review
    elapsed = 0.0
    last = concept.get("last_review") or concept.get("last_practiced")
    if last:
        try:
            elapsed = max(0.0, (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 86400.0)
        except Exception:
            elapsed = 0.0

    if grade == 1:  # Again
        lapses += 1
        reps = 0
        # Fail: increase difficulty, cut stability
        D = min(10.0, D + 0.8)
        S = max(FSRS_MIN_STABILITY, S * 0.25)
        concept["fsrs_state"] = "relearning"
        interval = min(1.0, _next_interval_days(S))
    elif grade == 2:  # Hard
        reps += 1
        D = min(10.0, D + 0.15)
        # modest growth, shorter than Good
        factor = 1.2 * (1.0 + (10.0 - D) / 20.0)
        if elapsed > 0 and S > 0:
            factor *= max(1.0, math.sqrt(elapsed / max(S, 0.5)))
        S = max(FSRS_MIN_STABILITY, S * factor)
        concept["fsrs_state"] = "review"
        interval = _next_interval_days(S) * 0.85
    elif grade == 3:  # Good
        reps += 1
        D = max(1.0, D - 0.05)
        factor = 1.6 * (1.0 + (10.0 - D) / 15.0)
        if elapsed > 0 and S > 0:
            factor *= max(1.0, math.sqrt(elapsed / max(S, 0.5)))
        S = max(FSRS_MIN_STABILITY, S * factor)
        concept["fsrs_state"] = "review"
        interval = _next_interval_days(S)
    else:  # Easy
        reps += 1
        D = max(1.0, D - 0.2)
        factor = 2.2 * (1.0 + (10.0 - D) / 12.0)
        if elapsed > 0 and S > 0:
            factor *= max(1.0, math.sqrt(elapsed / max(S, 0.5)))
        S = max(FSRS_MIN_STABILITY, S * factor)
        concept["fsrs_state"] = "review"
        interval = _next_interval_days(S) * 1.3

    # Open high-severity MC: never schedule further than 1 day
    if has_open_misconception(concept) and open_misconceptions(concept, severity="high"):
        interval = min(interval, 1.0)

    interval = max(0.5, min(FSRS_MAX_INTERVAL_DAYS, interval))

    concept["stability"] = round(S, 3)
    concept["difficulty"] = round(D, 2)
    concept["reps"] = reps
    concept["lapses"] = lapses
    concept["last_review"] = datetime.now().isoformat()
    concept["next_review"] = (
        datetime.now() + timedelta(days=interval)
    ).isoformat()
    concept["last_grade"] = grade
    return concept["next_review"]


def calculate_review_date(concept, assessment=None):
    """
    Schedule next review. Uses FSRS-inspired scheduler when assessment
    is available; otherwise seeds from mastery only.
    """
    if assessment is not None:
        grade = grade_from_assessment(concept, assessment)
        return schedule_fsrs(concept, grade)

    ensure_fsrs_fields(concept)
    interval = _next_interval_days(float(concept["stability"]))
    if has_open_misconception(concept):
        interval = min(interval, 1.0)
    return (datetime.now() + timedelta(days=interval)).isoformat()


def rebuild_review_queue(state):

    queue = []

    now = datetime.now()

    for subject_id, subject in state[
        "subjects"
    ].items():

        concepts = subject.get(
            "concepts",
            {}
        )

        for concept_id, concept in concepts.items():

            next_review = concept.get(
                "next_review"
            )

            if not next_review:
                continue

            try:
                review_time = datetime.fromisoformat(
                    next_review
                )
            except Exception:
                continue

            if review_time <= now:

                queue.append({
                    "subject_id": subject_id,
                    "concept_id": concept_id,
                    "priority": (
                        0
                        if has_open_misconception(
                            concept
                        )
                        else (
                            1
                            if average_mastery(
                                concept
                            ) < 0.50
                            else 2
                        )
                    )
                })

    queue.sort(
        key=lambda x: x["priority"]
    )

    state["review_queue"] = queue


# ============================================================
# STATE ASSESSMENT
# ============================================================

DIMENSIONS = [
    "knowledge",
    "retrieval",
    "application",
    "explanation",
    "transfer"
]

ACTIVITIES = [
    "RETRIEVAL",
    "APPLICATION",
    "EXPLANATION",
    "TRANSFER",
    "MISCONCEPTION_CHECK",
    "REVIEW"
]

# Dimension → preferred activity when that dimension is the weakest
DIM_TO_ACTIVITY = {
    "knowledge": "EXPLANATION",
    "retrieval": "RETRIEVAL",
    "application": "APPLICATION",
    "explanation": "EXPLANATION",
    "transfer": "TRANSFER",
}

# Below this average mastery, treat dimension as weak
WEAK_DIM_THRESHOLD = 0.45
# Prerequisite mastery below this → redirect focus to prereq
PREREQ_WEAK_THRESHOLD = 0.40


def find_weak_prerequisite(state, subject_id, concept_id, max_depth=4):
    """
    Walk hard (then soft) prerequisites; return deepest weak one.
    Returns (prereq_concept_id, avg_mastery, strength) or None.
    """
    subject = (state.get("subjects") or {}).get(subject_id) or {}
    concepts = subject.get("concepts") or {}
    if concept_id not in concepts:
        return None

    visited = set()
    best = None  # deepest weak

    def walk(cid, depth, inherited_strength):
        nonlocal best
        if depth > max_depth or cid in visited:
            return
        visited.add(cid)
        concept = concepts.get(cid)
        if not concept:
            return
        for edge in concept.get("prerequisites") or []:
            if isinstance(edge, str):
                pre_id, strength = normalize_id(edge), "soft"
            elif isinstance(edge, dict):
                pre_id = normalize_id(edge.get("id") or edge.get("concept_id"))
                strength = str(edge.get("strength") or "soft").lower()
                if strength not in ("hard", "soft"):
                    strength = "soft"
            else:
                continue
            if not pre_id or pre_id not in concepts or pre_id == cid:
                continue
            pre = concepts[pre_id]
            avg = average_mastery(pre)
            # Open high MC on prereq counts as weak
            weak = avg < PREREQ_WEAK_THRESHOLD or bool(
                open_misconceptions(pre, severity="high")
            )
            if weak:
                # Prefer deeper nodes; hard over soft at same depth
                rank = (depth, 1 if strength == "hard" else 0, -avg)
                if best is None or rank > best[0]:
                    best = (rank, pre_id, avg, strength)
            walk(pre_id, depth + 1, strength)

    walk(concept_id, 1, "soft")
    if not best:
        return None
    _, pre_id, avg, strength = best
    return pre_id, avg, strength


def apply_prerequisite_proposals(state, subject_id, concept_id, prereq_list):
    """
    Attach prerequisite edges to a concept. Conservative: only existing ids.
    prereq_list: ["other_id", ...] or [{"id": "...", "strength": "hard"|"soft"}]
    """
    if not prereq_list or not subject_id or not concept_id:
        return []
    subject = (state.get("subjects") or {}).get(subject_id)
    if not subject:
        return []
    concepts = subject.get("concepts") or {}
    if concept_id not in concepts:
        return []
    concept = concepts[concept_id]
    edges = concept.setdefault("prerequisites", [])
    existing = set()
    for e in edges:
        if isinstance(e, str):
            existing.add(normalize_id(e))
        elif isinstance(e, dict):
            existing.add(normalize_id(e.get("id") or e.get("concept_id")))

    notes = []
    for item in prereq_list:
        if isinstance(item, str):
            pid, strength = normalize_id(item), "soft"
        elif isinstance(item, dict):
            pid = normalize_id(item.get("id") or item.get("concept_id"))
            strength = str(item.get("strength") or "soft").lower()
            if strength not in ("hard", "soft"):
                strength = "soft"
        else:
            continue
        if not pid or pid == concept_id or pid not in concepts:
            continue
        if pid in existing:
            continue
        # No immediate cycle: prereq must not list concept as its prereq
        pre_edges = concepts[pid].get("prerequisites") or []
        cycle = False
        for pe in pre_edges:
            pe_id = normalize_id(pe if isinstance(pe, str) else (pe.get("id") or ""))
            if pe_id == concept_id:
                cycle = True
                break
        if cycle:
            continue
        edges.append({"id": pid, "strength": strength})
        existing.add(pid)
        notes.append(f"prereq: {concept_id} ← {pid} ({strength})")
    return notes


def clamp(value):

    try:
        value = float(value)
    except Exception:
        return None

    return max(
        0.0,
        min(1.0, value)
    )


# ============================================================
# NAMED MISCONCEPTION CATALOGUE (Phase C)
# ============================================================

_catalog_cache = None


def load_misconception_catalog(force_reload=False):
    """Load named MC catalogue from JSON. Cached after first read."""
    global _catalog_cache
    if _catalog_cache is not None and not force_reload:
        return _catalog_cache
    if not CATALOG_FILE.exists():
        _catalog_cache = {"version": 1, "entries": []}
        return _catalog_cache
    try:
        with open(CATALOG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {"entries": []}
        data.setdefault("entries", [])
        _catalog_cache = data
    except Exception:
        _catalog_cache = {"version": 1, "entries": []}
    return _catalog_cache


def catalog_entries_for(subject_id=None, concept_id=None):
    """Return catalogue entries relevant to subject and/or concept."""
    cat = load_misconception_catalog()
    out = []
    for e in cat.get("entries") or []:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        subjects = e.get("subjects") or []
        concepts = e.get("concepts") or []
        if subject_id and subjects and subject_id not in subjects:
            continue
        if concept_id and concepts and concept_id not in concepts:
            # still allow if no concept filter on entry and subject matched
            if concepts:
                continue
        out.append(e)
    return out


def find_catalog_entry(entry_id=None, description=None, subject_id=None, concept_id=None):
    """
    Resolve a catalogue entry by id or fuzzy description match.
    Returns entry dict or None. Prefers id. Abstains if no good match.
    """
    entries = catalog_entries_for(subject_id, concept_id)
    if not entries:
        entries = load_misconception_catalog().get("entries") or []

    if entry_id:
        eid = str(entry_id).strip()
        for e in entries:
            if e.get("id") == eid:
                return e
        # global fallback by id
        for e in load_misconception_catalog().get("entries") or []:
            if e.get("id") == eid:
                return e

    if not description:
        return None

    desc_norm = re.sub(r"\s+", " ", str(description).lower()).strip()
    desc_norm = (
        desc_norm.replace("vier", "4").replace("twee", "2")
        .replace("één", "1").replace(" een ", " 1 ")
    )
    desc_tokens = set(re.findall(r"[a-z0-9à-ÿ]+", desc_norm))
    best, best_score = None, 0.0

    for e in entries:
        kws = e.get("keywords") or []
        if kws:
            kw_ok = True
            for k in kws:
                k_n = re.sub(r"\s+", " ", str(k).lower()).replace("vier", "4")
                if k_n not in desc_norm:
                    kw_ok = False
                    break
            if kw_ok:
                return e

        candidates = [
            str(e.get("label") or ""),
            str(e.get("pattern") or ""),
            str(e.get("id") or "").replace("_", " ").lower(),
        ]
        for c in candidates:
            c_norm = re.sub(r"\s+", " ", c.lower()).strip()
            c_norm = c_norm.replace("vier", "4").replace("twee", "2")
            if not c_norm:
                continue
            if c_norm in desc_norm or desc_norm in c_norm:
                return e
            c_tokens = set(re.findall(r"[a-z0-9à-ÿ]+", c_norm))
            if desc_tokens and c_tokens:
                score = len(desc_tokens & c_tokens) / max(len(desc_tokens), len(c_tokens))
                if score > best_score:
                    best_score = score
                    best = e

    if best and best_score >= 0.40:
        return best
    return None


# Auto-promotion thresholds (learner never configures these)
MC_PROMOTE_MIN_SEEN = 3          # same fingerprint seen this many times
MC_PROMOTE_MIN_SUBJECTS = 2      # OR across this many subjects
MC_PROMOTE_AUTO_COMMIT = True    # write into misconception_catalog.json when ready


def _mc_fingerprint(description, catalog_id=None):
    if catalog_id:
        return f"id:{catalog_id}"
    desc = re.sub(r"\s+", " ", str(description or "").lower()).strip()
    desc = desc.replace("vier", "4").replace("twee", "2")
    tokens = re.findall(r"[a-z0-9à-ÿ]+", desc)
    # keep content words, drop very short noise
    tokens = [t for t in tokens if len(t) > 2][:12]
    return "fp:" + "_".join(tokens[:8]) if tokens else ""


def scan_misconception_clusters(state):
    """
    Group open/known MCs across all subjects by fingerprint or catalog id.
    Returns list of cluster dicts.
    """
    clusters = {}
    for sid, sub in (state.get("subjects") or {}).items():
        if not isinstance(sub, dict):
            continue
        for cid, concept in (sub.get("concepts") or {}).items():
            if not isinstance(concept, dict):
                continue
            for m in concept.get("misconceptions") or []:
                if not isinstance(m, dict):
                    continue
                if m.get("status") == "resolved":
                    continue
                mid = m.get("id")
                desc = m.get("description") or ""
                fp = _mc_fingerprint(desc, mid)
                if not fp:
                    continue
                cl = clusters.setdefault(fp, {
                    "fingerprint": fp,
                    "catalog_id": mid,
                    "description": desc or mid,
                    "severity": m.get("severity") or "medium",
                    "count": 0,
                    "subjects": set(),
                    "concepts": set(),
                    "examples": [],
                })
                cl["count"] += 1
                cl["subjects"].add(sid)
                cl["concepts"].add(f"{sid}/{cid}")
                if mid:
                    cl["catalog_id"] = mid
                if len(cl["examples"]) < 3 and desc:
                    cl["examples"].append(desc)
    # freeze sets for JSON
    out = []
    for cl in clusters.values():
        out.append({
            **cl,
            "subjects": sorted(cl["subjects"]),
            "concepts": sorted(cl["concepts"]),
            "n_subjects": len(cl["subjects"]),
        })
    return out


def update_pending_catalog(state):
    """
    Refresh meta.pending_catalog from clusters that look like repeated MCs.
    Already-catalogued ids are skipped (they already have a stable id).
    Returns list of new/updated pending entries.
    """
    state.setdefault("meta", {"pending_catalog": [], "taxonomy_events": []})
    pending = state["meta"].setdefault("pending_catalog", [])
    by_fp = {p.get("fingerprint"): p for p in pending if p.get("fingerprint")}

    clusters = scan_misconception_clusters(state)
    changed = []
    for cl in clusters:
        # Skip if already has catalog id and is in file
        if cl.get("catalog_id"):
            cat = find_catalog_entry(entry_id=cl["catalog_id"])
            if cat:
                continue
        if cl["count"] < MC_PROMOTE_MIN_SEEN and cl["n_subjects"] < MC_PROMOTE_MIN_SUBJECTS:
            continue
        fp = cl["fingerprint"]
        entry = by_fp.get(fp) or {
            "fingerprint": fp,
            "proposed_id": None,
            "description": cl["description"],
            "severity": cl["severity"],
            "subjects": [],
            "concepts": [],
            "count": 0,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
        }
        entry["description"] = cl["description"] or entry.get("description")
        entry["severity"] = cl.get("severity") or entry.get("severity")
        entry["subjects"] = sorted(set(entry.get("subjects") or []) | set(cl["subjects"]))
        entry["concepts"] = sorted(set(entry.get("concepts") or []) | set(cl["concepts"]))
        entry["count"] = max(int(entry.get("count") or 0), cl["count"])
        entry["updated_at"] = datetime.now().isoformat()
        if not entry.get("proposed_id"):
            # Derive a stable-ish id from fingerprint
            raw = re.sub(r"^fp:", "", fp).upper()
            raw = re.sub(r"[^A-Z0-9]+", "_", raw)[:40].strip("_")
            entry["proposed_id"] = f"M_AUTO_{raw}" if raw else None
        by_fp[fp] = entry
        changed.append(entry)

    state["meta"]["pending_catalog"] = list(by_fp.values())[-50:]
    return changed


def commit_pending_to_catalog(state, min_count=None):
    """
    Promote pending entries into misconception_catalog.json (multi-subject).
    Returns number of entries committed.
    """
    if not MC_PROMOTE_AUTO_COMMIT:
        return 0
    min_count = min_count if min_count is not None else MC_PROMOTE_MIN_SEEN
    cat = load_misconception_catalog(force_reload=True)
    existing_ids = {e.get("id") for e in (cat.get("entries") or []) if e.get("id")}
    committed = 0

    for entry in list(state.get("meta", {}).get("pending_catalog") or []):
        if entry.get("status") == "committed":
            continue
        if int(entry.get("count") or 0) < min_count and len(entry.get("subjects") or []) < MC_PROMOTE_MIN_SUBJECTS:
            continue
        pid = entry.get("proposed_id")
        if not pid or pid in existing_ids:
            if pid in existing_ids:
                entry["status"] = "committed"
                entry["catalog_id"] = pid
            continue

        # concept ids without subject prefix
        concepts = []
        for c in entry.get("concepts") or []:
            if "/" in c:
                concepts.append(c.split("/", 1)[1])
            else:
                concepts.append(c)
        concepts = sorted(set(concepts))

        new_e = {
            "id": pid,
            "concepts": concepts,
            "subjects": list(entry.get("subjects") or []),
            "label": (entry.get("description") or pid)[:80],
            "pattern": f"Auto-promoted from learner evidence (seen {entry.get('count')}×).",
            "keywords": [],
            "severity": entry.get("severity") or "medium",
            "remediation_hint": "Stel een gerichte Socratische vraag die dit misverstand blootlegt; geef niet meteen het antwoord.",
            "auto": True,
        }
        cat.setdefault("entries", []).append(new_e)
        existing_ids.add(pid)
        entry["status"] = "committed"
        entry["catalog_id"] = pid
        entry["committed_at"] = datetime.now().isoformat()
        committed += 1

        # Stamp open MCs in state with this id where fingerprint matches
        fp = entry.get("fingerprint")
        for sid, sub in (state.get("subjects") or {}).items():
            for cid, concept in (sub.get("concepts") or {}).items():
                for m in concept.get("misconceptions") or []:
                    if not isinstance(m, dict) or m.get("id"):
                        continue
                    if _mc_fingerprint(m.get("description"), None) == fp:
                        m["id"] = pid

        state.setdefault("meta", {}).setdefault("taxonomy_events", []).append({
            "date": datetime.now().isoformat(),
            "type": "mc_catalog_commit",
            "id": pid,
            "subjects": entry.get("subjects"),
        })

    if committed:
        # Trim events
        events = state["meta"].get("taxonomy_events") or []
        state["meta"]["taxonomy_events"] = events[-40:]
        try:
            atomic_save_json(CATALOG_FILE, cat)
            load_misconception_catalog(force_reload=True)
        except Exception:
            pass
    return committed


def maybe_curate_misconceptions(state):
    """Run pending update + optional auto-commit. Called after assessments."""
    update_pending_catalog(state)
    return commit_pending_to_catalog(state)


def link_subjects(state, child_id, parent_id, related=False):
    """
    Set parent_id or add related link. Conservative; no cycles.
    Returns True if changed.
    """
    child_id = normalize_id(child_id)
    parent_id = normalize_id(parent_id)
    subjects = state.get("subjects") or {}
    if child_id not in subjects or parent_id not in subjects:
        return False
    if child_id == parent_id:
        return False
    child = subjects[child_id]
    parent = subjects[parent_id]
    if related:
        rel = child.setdefault("related", [])
        if parent_id not in rel:
            rel.append(parent_id)
        rel_p = parent.setdefault("related", [])
        if child_id not in rel_p:
            rel_p.append(child_id)
        return True
    # hierarchy
    if parent.get("parent_id") == child_id:
        return False
    child["parent_id"] = parent_id
    state.setdefault("meta", {}).setdefault("taxonomy_events", []).append({
        "date": datetime.now().isoformat(),
        "type": "subject_parent",
        "child": child_id,
        "parent": parent_id,
    })
    return True


def catalog_prompt_slice(subject_id=None, concept_id=None, max_entries=8):
    """Compact catalogue slice for the system prompt (closed-set diagnosis)."""
    entries = catalog_entries_for(subject_id, concept_id)[:max_entries]
    return [
        {
            "id": e.get("id"),
            "label": e.get("label"),
            "severity": e.get("severity", "medium"),
            "hint": (e.get("remediation_hint") or "")[:120],
        }
        for e in entries
    ]


def update_dimension(
    old_value,
    new_value
):
    """
    Conservative incremental update.

    One LLM assessment cannot suddenly change a concept
    from 0.1 to 0.9.
    """

    new_value = clamp(new_value)

    if new_value is None:
        return old_value

    old_value = clamp(old_value)

    if old_value is None:
        old_value = 0.0

    delta = new_value - old_value

    # Maximum movement per answer
    max_delta = 0.10

    if delta > max_delta:
        delta = max_delta

    if delta < -max_delta:
        delta = -max_delta

    return round(
        old_value + delta,
        3
    )


def apply_assessment(
    state,
    assessment
):

    if not isinstance(
        assessment,
        dict
    ):
        return None

    raw_subject = assessment.get("subject", "general")
    raw_concept = assessment.get("concept")

    # Explicit null / invalid concept → do not touch concept database
    if raw_concept is None or not is_valid_id(raw_concept):
        return None

    subject_id = ensure_subject(state, raw_subject)
    concept_id = ensure_concept(state, subject_id, raw_concept)

    if not concept_id:
        return None

    concept = state["subjects"][subject_id]["concepts"][concept_id]

    # --------------------------------------------------------
    # Dimensions
    # --------------------------------------------------------

    dimensions = assessment.get(
        "dimensions",
        {}
    )

    if isinstance(
        dimensions,
        dict
    ):

        for dimension in DIMENSIONS:

            if dimension not in dimensions:
                continue

            value = dimensions[
                dimension
            ]

            concept[dimension] = (
                update_dimension(
                    concept.get(
                        dimension,
                        0.0
                    ),
                    value
                )
            )

    # --------------------------------------------------------
    # Misconceptions (named catalogue + free-form)
    # --------------------------------------------------------

    misconceptions = assessment.get(
        "misconceptions",
        []
    )

    if isinstance(
        misconceptions,
        list
    ):

        for item in misconceptions:

            if isinstance(
                item,
                str
            ):

                item = {
                    "description": item,
                    "severity": "unknown",
                    "status": "open",
                    "evidence": ""
                }

            if not isinstance(
                item,
                dict
            ):
                continue

            description = str(
                item.get(
                    "description",
                    ""
                )
            ).strip()

            # Optional catalogue id from model or fuzzy match
            raw_id = item.get("id") or item.get("misconception_id")
            cat = find_catalog_entry(
                entry_id=raw_id,
                description=description or None,
                subject_id=subject_id,
                concept_id=concept_id,
            )
            if cat:
                description = description or str(cat.get("label") or cat.get("id"))
                if not item.get("severity"):
                    item["severity"] = cat.get("severity", "medium")
                item["_catalog_id"] = cat.get("id")
                item["_remediation_hint"] = cat.get("remediation_hint")

            if not description:
                continue

            existing = None
            desc_norm = re.sub(r"\s+", " ", description.lower())
            cat_id = item.get("_catalog_id")

            for old in concept["misconceptions"]:
                if not isinstance(old, dict):
                    continue
                # Match by catalogue id first (strongest dedupe)
                if cat_id and old.get("id") == cat_id:
                    existing = old
                    break
                old_desc = str(old.get("description", "")).strip()
                old_norm = re.sub(r"\s+", " ", old_desc.lower())
                if old_norm == desc_norm:
                    existing = old
                    break
                t_new = set(re.findall(r"[a-z0-9à-ÿ]+", desc_norm))
                t_old = set(re.findall(r"[a-z0-9à-ÿ]+", old_norm))
                if t_new and t_old:
                    overlap = len(t_new & t_old) / max(len(t_new), len(t_old))
                    if overlap >= 0.6:
                        existing = old
                        break

            if existing:

                if "status" in item:
                    existing["status"] = item[
                        "status"
                    ]

                if "severity" in item:
                    existing["severity"] = item[
                        "severity"
                    ]

                if "evidence" in item:
                    existing["evidence"] = item[
                        "evidence"
                    ]
                if cat_id:
                    existing["id"] = cat_id
                if item.get("_remediation_hint") and not existing.get("remediation_hint"):
                    existing["remediation_hint"] = item["_remediation_hint"]

            else:

                concept[
                    "misconceptions"
                ].append({
                    "id": cat_id,
                    "description": description,
                    "severity": item.get(
                        "severity",
                        "medium"
                    ),
                    "status": item.get(
                        "status",
                        "open"
                    ),
                    "evidence": item.get(
                        "evidence",
                        ""
                    ),
                    "remediation_hint": item.get("_remediation_hint"),
                    "created_at": datetime.now().isoformat()
                })

    # --------------------------------------------------------
    # Evidence
    # --------------------------------------------------------

    evidence = assessment.get(
        "evidence"
    )

    if evidence:

        concept["evidence"].append({
            "date": datetime.now().isoformat(),
            "evidence": str(evidence)
        })

        concept["evidence"] = (
            concept["evidence"][-10:]
        )

    # --------------------------------------------------------
    # Practice / review
    # --------------------------------------------------------

    concept["last_practiced"] = (
        datetime.now().isoformat()
    )

    concept["review_count"] = (
        concept.get(
            "review_count",
            0
        ) + 1
    )

    concept["next_review"] = calculate_review_date(concept, assessment)

    # --------------------------------------------------------
    # Prerequisites (optional LLM proposals)
    # --------------------------------------------------------
    prereqs = assessment.get("prerequisites")
    if prereqs:
        notes = apply_prerequisite_proposals(
            state, subject_id, concept_id, prereqs
        )
        if notes:
            state.setdefault("meta", {}).setdefault("taxonomy_events", []).append({
                "date": datetime.now().isoformat(),
                "type": "prerequisites",
                "notes": notes,
            })

    # --------------------------------------------------------
    # Auto-curate misconceptions (pending → catalog)
    # --------------------------------------------------------
    try:
        maybe_curate_misconceptions(state)
    except Exception:
        pass

    # --------------------------------------------------------
    # Session
    # --------------------------------------------------------

    activity = assessment.get(
        "activity"
    )

    if activity in ACTIVITIES:

        state[
            "active_session"
        ]["current_activity"] = activity

    state[
        "active_session"
    ]["current_concept_id"] = concept_id

    state[
        "active_session"
    ]["subject_id"] = subject_id

    rebuild_review_queue(
        state
    )

    return {
        "subject_id": subject_id,
        "concept_id": concept_id
    }


# ============================================================
# MODEL OUTPUT PARSING
# ============================================================

def extract_json_object(text):
    """
    Robustly extract a JSON object from an LLM response.

    Handles:
      plain JSON
      ```json ... ```
      text surrounding JSON
    """

    text = text.strip()

    # Remove markdown fences
    text = re.sub(
        r"^```(?:json)?\s*",
        "",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"\s*```$",
        "",
        text
    )

    # First attempt: entire response
    try:
        value = json.loads(text)

        if isinstance(value, dict):
            return value

    except Exception:
        pass

    # Find first JSON object
    start = text.find("{")

    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for i in range(
        start,
        len(text)
    ):

        char = text[i]

        if escaped:
            escaped = False
            continue

        if char == "\\":
            escaped = True
            continue

        if char == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if char == "{":
            depth += 1

        elif char == "}":
            depth -= 1

            if depth == 0:

                candidate = text[
                    start:i + 1
                ]

                try:
                    value = json.loads(
                        candidate
                    )

                    if isinstance(
                        value,
                        dict
                    ):
                        return value

                except Exception:
                    pass

                break

    return None


# ============================================================
# MODEL COMMUNICATION
# ============================================================

def _normalize_think(think_val):
    if isinstance(think_val, str):
        t = think_val.strip().lower()
        if t in ("none", "off", "false", "0"):
            return False
        if t in ("true", "1", "on"):
            return True
    return think_val


def _decode_json_string_prefix(buffer, start_idx):
    """Decode a JSON string starting at start_idx (first char after opening quote)."""
    i = start_idx
    out = []
    while i < len(buffer):
        ch = buffer[i]
        if ch == "\\":
            if i + 1 >= len(buffer):
                break
            nxt = buffer[i + 1]
            escapes = {
                "n": "\n", "t": "\t", "r": "\r",
                '"': '"', "\\": "\\", "/": "/",
            }
            out.append(escapes.get(nxt, nxt))
            i += 2
            continue
        if ch == '"':
            break
        out.append(ch)
        i += 1
    return "".join(out)


def extract_partial_response_field(buffer):
    """
    Best-effort extract of the JSON "response" string value while the
    object is still streaming (may be incomplete / unclosed).
    Returns decoded text or None if the field has not started yet.
    """
    if not buffer:
        return None
    m = re.search(r'"response"\s*:\s*"', buffer)
    if not m:
        return None
    return _decode_json_string_prefix(buffer, m.end())


def extract_partial_routing_action(buffer):
    """
    Best-effort extract of routing.action from a partial JSON buffer.
    Returns action string or None if not yet present.
    """
    if not buffer:
        return None
    m = re.search(
        r'"routing"\s*:\s*\{[^}]*?"action"\s*:\s*"([^"]+)"',
        buffer,
        flags=re.DOTALL,
    )
    if m:
        return m.group(1).strip().lower()
    # Flat fallback if model emits action near the start
    m2 = re.search(r'"action"\s*:\s*"(continue|switch|new_subject|list_subjects|clarify)"', buffer)
    if m2:
        return m2.group(1).strip().lower()
    return None


def ask_model(messages, stream=None):
    """
    Call Ollama native /api/chat.
    Field name is "think" (not "thinking"). THINK=False disables CoT.
    When STREAM is True, live-prints the learner-facing response field
    as tokens arrive (JSON is still collected in full for parsing).
    """
    think_val = _normalize_think(THINK)
    use_stream = STREAM if stream is None else bool(stream)

    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": use_stream,
        "think": think_val,
        "options": {
            "temperature": TEMPERATURE,
        },
    }

    ask_model.did_stream_response = False
    t0 = datetime.now()

    if not use_stream:
        response = requests.post(
            MODEL_URL,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        message = data.get("message") or {}
        content = message.get("content") or ""
        thinking_text = message.get("thinking") or ""
        if not content:
            content = thinking_text
        total_ns = data.get("total_duration") or 0
        ask_model.last_stats = {
            "think_requested": think_val,
            "thinking_returned": bool(thinking_text),
            "thinking_chars": len(thinking_text),
            "eval_count": data.get("eval_count") or 0,
            "prompt_eval_count": data.get("prompt_eval_count") or 0,
            "total_s": round(total_ns / 1e9, 1) if total_ns else None,
            "streamed": False,
        }
        return content

    # ---- streaming path ----
    response = requests.post(
        MODEL_URL,
        json=payload,
        timeout=REQUEST_TIMEOUT,
        stream=True,
    )
    response.raise_for_status()

    full_content = []
    thinking_parts = []
    eval_count = 0
    prompt_eval_count = 0
    total_ns = 0
    header_shown = False
    printed_len = 0

    for line in response.iter_lines(decode_unicode=True):
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        message = data.get("message") or {}
        piece = message.get("content") or ""
        think_piece = message.get("thinking") or ""
        if piece:
            full_content.append(piece)
        if think_piece:
            thinking_parts.append(think_piece)

        if data.get("eval_count"):
            eval_count = data["eval_count"]
        if data.get("prompt_eval_count"):
            prompt_eval_count = data["prompt_eval_count"]
        if data.get("total_duration"):
            total_ns = data["total_duration"]

        # Live-print ONLY for teaching turns (continue / clarify).
        # list_subjects / switch / new_subject are handled by Python UI —
        # streaming those responses causes double questions / double lists.
        buf = "".join(full_content)
        action = extract_partial_routing_action(buf)
        streamable = action in (None, "continue", "clarify")
        # Wait until routing.action is known before printing, so we never
        # start a TUTOR block that must later be superseded by a resume banner.
        if action in ("continue", "clarify"):
            partial = extract_partial_response_field(buf)
            if partial is not None:
                if not header_shown:
                    print()
                    divider()
                    print(c("TUTOR", BOLD + GREEN))
                    divider()
                    print()
                    header_shown = True
                    ask_model.did_stream_response = True
                if len(partial) > printed_len:
                    print(partial[printed_len:], end="", flush=True)
                    printed_len = len(partial)
        elif action in ("list_subjects", "switch", "new_subject"):
            # Explicitly do not stream the response body
            pass

        if data.get("done"):
            break

    if header_shown:
        print()  # end streamed body

    content = "".join(full_content)
    thinking_text = "".join(thinking_parts)
    if not content and thinking_text:
        content = thinking_text

    elapsed = (datetime.now() - t0).total_seconds()
    ask_model.last_stats = {
        "think_requested": think_val,
        "thinking_returned": bool(thinking_text),
        "thinking_chars": len(thinking_text),
        "eval_count": eval_count,
        "prompt_eval_count": prompt_eval_count,
        "total_s": round(total_ns / 1e9, 1) if total_ns else round(elapsed, 1),
        "streamed": True,
    }
    return content


ask_model.last_stats = {}
ask_model.did_stream_response = False


def extract_question_from_text(text):
    """Best-effort: last interrogative sentence, else truncated response."""
    if not text:
        return None
    text = str(text).strip()
    # Prefer last sentence with ?
    parts = re.split(r"(?<=[?؟])\s+", text)
    for p in reversed(parts):
        p = p.strip()
        if "?" in p or "？" in p:
            return p[:300]
    # Fallback: last sentence
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if sentences:
        return sentences[-1].strip()[:300]
    return text[:200] or None


def update_session_memory(state, session_info, response_text):
    """
    Update working memory: session_thread, last_tutor_question, recent_questions.
    """
    active = state["active_session"]
    if not isinstance(session_info, dict):
        session_info = {}

    # Explicit last_question from model, else extract from response
    last_q = session_info.get("last_question") or session_info.get("last_tutor_question")
    if last_q:
        last_q = str(last_q).strip()[:300]
    else:
        last_q = extract_question_from_text(response_text)

    if last_q:
        active["last_tutor_question"] = last_q
        recent = list(active.get("recent_questions") or [])
        # Avoid exact dup at end
        if not recent or recent[-1] != last_q:
            recent.append(last_q)
        active["recent_questions"] = recent[-5:]

    thread = session_info.get("session_thread")
    if thread and str(thread).strip():
        active["session_thread"] = str(thread).strip()[:500]


def print_model_stats():
    """Show whether CoT ran and how long the call took."""
    s = getattr(ask_model, "last_stats", None) or {}
    if not s:
        return
    think_req = s.get("think_requested")
    think_got = s.get("thinking_returned")
    chars = s.get("thinking_chars") or 0
    eval_n = s.get("eval_count") or 0
    prompt_n = s.get("prompt_eval_count") or 0
    secs = s.get("total_s")
    streamed = s.get("streamed")
    parts = [
        f"think={think_req}",
        f"cot={'yes' if think_got else 'no'}"
        + (f"({chars}c)" if chars else ""),
        f"out={eval_n}tok",
        f"in={prompt_n}tok",
    ]
    if streamed:
        parts.append("stream")
    if secs is not None:
        parts.append(f"{secs}s")
    print(c("  [" + " | ".join(parts) + "]", GRAY))


JSON_REMINDER = (
    "Your previous reply was not valid JSON. "
    "Reply again with ONLY a single valid JSON object. "
    "No markdown, no fences, no extra text. "
    "Use exactly the required structure with keys: "
    "response, routing, assessment, session."
)


def ask_model_with_json_retry(messages):
    """
    Call the model once. If the reply is not valid JSON,
    do exactly one retry with a short reminder.
    Returns (raw_text, parsed_dict_or_None).
    """
    raw = ask_model(messages)
    parsed = extract_json_object(raw)

    if parsed is not None:
        return raw, parsed

    # One retry with a strict reminder
    retry_messages = messages + [
        {"role": "assistant", "content": raw},
        {"role": "user", "content": JSON_REMINDER}
    ]

    try:
        raw2 = ask_model(retry_messages)
        parsed2 = extract_json_object(raw2)
        if parsed2 is not None:
            return raw2, parsed2
        # Both failed — return the second attempt for display
        return raw2, None
    except Exception:
        # Retry itself failed — fall back to first raw
        return raw, None


# ============================================================
# CONTEXT BUILDING
# ============================================================

def _concept_avg(concept):
    dims = [
        float(concept.get(k) or 0)
        for k in ("knowledge", "retrieval", "application", "explanation", "transfer")
    ]
    return round(sum(dims) / 5.0, 2)


def compact_subject_state(state, subject_id, focus=True):
    """
    Lean subject snapshot for the LLM.
    focus=True  → detail only current concept; others = title + avg score
    focus=False → light summary of all concepts (for status / overview)
    """
    subject = state["subjects"].get(subject_id)
    if not subject:
        return None

    active = state.get("active_session") or {}
    current_cid = active.get("current_concept_id")

    concepts_out = {}
    for cid, concept in (subject.get("concepts") or {}).items():
        is_current = (cid == current_cid) or (not current_cid and focus and len(concepts_out) == 0)

        if focus and current_cid and cid != current_cid:
            # Sibling concepts: minimal
            concepts_out[cid] = {
                "title": concept.get("title", cid),
                "avg": _concept_avg(concept),
                "current": False,
            }
            continue

        open_mc = []
        for m in concept.get("misconceptions") or []:
            if isinstance(m, dict) and m.get("status", "open") == "open":
                open_mc.append({
                    "description": str(m.get("description", ""))[:120],
                    "severity": m.get("severity", "unknown"),
                })
            if len(open_mc) >= 2:
                break

        evidence = concept.get("evidence") or []
        last_ev = []
        for e in evidence[-1:]:
            if isinstance(e, dict):
                last_ev.append(str(e.get("evidence", ""))[:80])
            else:
                last_ev.append(str(e)[:80])

        prereq_out = []
        for edge in (concept.get("prerequisites") or [])[:5]:
            if isinstance(edge, str):
                prereq_out.append({"id": edge, "strength": "soft"})
            elif isinstance(edge, dict) and edge.get("id"):
                prereq_out.append({
                    "id": edge.get("id"),
                    "strength": edge.get("strength") or "soft",
                })

        concepts_out[cid] = {
            "title": concept.get("title", cid),
            "K": round(float(concept.get("knowledge") or 0), 2),
            "R": round(float(concept.get("retrieval") or 0), 2),
            "A": round(float(concept.get("application") or 0), 2),
            "E": round(float(concept.get("explanation") or 0), 2),
            "T": round(float(concept.get("transfer") or 0), 2),
            "open_misconceptions": open_mc,
            "recent_evidence": last_ev,
            "prerequisites": prereq_out,
            "current": bool(is_current),
        }

    return {
        "id": subject_id,
        "title": subject.get("title", subject_id),
        "goal": subject.get("goal"),
        "concepts": concepts_out,
    }


def subject_status_line(state, subject_id):
    """One-line status for overview questions."""
    subject = state["subjects"].get(subject_id) or {}
    concepts = subject.get("concepts") or {}
    if not concepts:
        return "nog geen concepten"
    # Prefer most recently practiced
    best = None
    best_time = ""
    for cid, c in concepts.items():
        lp = c.get("last_practiced") or ""
        if lp >= best_time:
            best_time = lp
            best = c
    title = (best or {}).get("title") or next(iter(concepts))
    open_n = 0
    for c in concepts.values():
        for m in c.get("misconceptions") or []:
            if isinstance(m, dict) and m.get("status", "open") == "open":
                open_n += 1
    extra = f", {open_n} open MC" if open_n else ""
    return f"laatst: {title}{extra}"


def build_system_message(base_prompt, state, mode="auto", policy=None):
    """
    mode:
      auto  — teach if session active, else meta
      teach — focus packet for current subject/concept
      meta  — subject index + one-line status (no deep concept dump)
    policy: optional dict from suggest_next_activity()
    """
    active = state["active_session"]
    if mode == "auto":
        mode = "teach" if active.get("active") and active.get("subject_id") else "meta"

    subject_id = active.get("subject_id")
    if policy is None:
        policy = suggest_next_activity(state) if mode == "teach" else {
            "activity": None, "concept_id": None, "subject_id": None,
            "reason": "meta mode", "force": False,
        }

    # --- subject index (always lean) ---
    subject_index = []
    for sid, sub in state.get("subjects", {}).items():
        subject_index.append({
            "id": sid,
            "title": sub.get("title", sid),
            "goal": (sub.get("goal") or "")[:80] or None,
            "status": subject_status_line(state, sid),
            "n_concepts": len(sub.get("concepts") or {}),
        })

    # --- working memory slice ---
    working = {
        "subject_id": subject_id,
        "concept_id": active.get("current_concept_id") or policy.get("concept_id"),
        "activity": active.get("current_activity"),
        "question_number": active.get("question_number"),
        "session_thread": active.get("session_thread"),
        "last_tutor_question": active.get("last_tutor_question"),
        "recent_questions": (active.get("recent_questions") or [])[-5:],
        "just_resumed": bool(active.get("just_resumed")),
        "pending_goal": bool(active.get("pending_goal")),
    }

    orchestrator = {
        "preferred_activity": policy.get("activity"),
        "preferred_concept_id": policy.get("concept_id"),
        "reason": policy.get("reason"),
        "force": bool(policy.get("force")),
    }

    focus_cid = working.get("concept_id")
    mc_catalog = catalog_prompt_slice(subject_id, focus_cid) if mode == "teach" else []

    if mode == "teach" and subject_id:
        state_context = {
            "mode": "teach",
            "working_memory": working,
            "orchestrator": orchestrator,
            "focus_subject": compact_subject_state(state, subject_id, focus=True),
            "misconception_catalog": mc_catalog,
            "available_subjects": [
                {"id": s["id"], "title": s["title"]} for s in subject_index
            ],
        }
    else:
        state_context = {
            "mode": "meta",
            "working_memory": working,
            "orchestrator": orchestrator,
            "available_subjects": subject_index,
            "due_reviews": (state.get("review_queue") or [])[:5],
        }

    state_json = json.dumps(
        state_context,
        ensure_ascii=False,
        separators=(",", ":")
    )

    resume_hint = ""
    if active.get("just_resumed"):
        resume_hint = """
============================================================
SESSION JUST RESUMED — RETRIEVAL FIRST
============================================================
FIRST response: one short retrieval question (use resume_instruction
if learner-facing). activity=RETRIEVAL. No new material yet.
"""

    anti_repeat = """
============================================================
ANTI-REPEAT & SESSION THREAD (CRITICAL)
============================================================
- NEVER ask a question that is substantially the same as
  working_memory.last_tutor_question or any in recent_questions.
- If the learner already answered that point, ADVANCE:
  application, contrast, transfer, or a short factual scaffold —
  do not re-quiz the same fact.
- session.session_thread MUST be updated every turn:
  2–4 short sentences: what is established + what is still open.
- session.last_question = the exact question you just asked the learner.
"""

    policy_hint = """
============================================================
ORCHESTRATOR (Python policy — follow unless clearly wrong)
============================================================
A deterministic policy chose the preferred next activity from learner state.
- If orchestrator.force is true: you MUST use preferred_activity this turn.
- If preferred_activity is set and force is false: prefer it unless the
  learner's latest message clearly requires a different move.
- Set assessment.activity to the activity you actually used.
- Stay on preferred_concept_id when set.
"""

    catalog_hint = """
============================================================
NAMED MISCONCEPTIONS (prefer catalogue ids)
============================================================
If misconception_catalog is non-empty and the learner's answer matches an entry,
set misconceptions[].id to that catalogue id and reuse its label/severity.
Do NOT force a match — omit id if none fits (abstain).
Free-form description is still allowed when no catalogue entry fits.
When probing a known open MC, prefer the remediation_hint as guidance for
your question — do not lecture the full correct answer.
"""

    return f"""
{base_prompt}

============================================================
CURRENT LEARNER STATE ({mode})
============================================================

{state_json}
{resume_hint}
{anti_repeat}
{policy_hint}
{catalog_hint}
============================================================
IMPORTANT TUTOR RULES
============================================================

You are operating inside a Python tutor application.

Python owns persistent state.

You are responsible for:
- pedagogical reasoning
- choosing the next useful learning activity
- asking good Socratic questions
- assessing the learner based on evidence

Python is responsible for:
- validation
- persistence
- score updates
- scheduling
- session management

Do NOT assume that a long answer means mastery.

Evaluate reasoning, not verbosity.

Do not invent learner history.

If evidence is insufficient, use conservative assessment values.

The learner may freely switch subjects.

Do not impose chapters or a fixed curriculum.

Prioritize:
1. due reviews
2. misconceptions
3. weak dimensions
4. retrieval
5. application
6. transfer
7. new concepts

Do not automatically explain everything.

Prefer making the learner retrieve, reason and apply.

============================================================
REQUIRED OUTPUT
============================================================

Return ONLY valid JSON.

No markdown.
No ```json fences.
No text before or after the JSON.

Exact structure (routing FIRST):

{{
  "routing": {{
    "action": "continue",
    "subject_id": null,
    "proposed_title": null,
    "taxonomy": {{
      "parent_id": null,
      "related_ids": [],
      "add_aliases": [],
      "concept_id": null,
      "concept_aliases": []
    }}
  }},
  "response": "Message shown to learner (empty for list_subjects/switch/new_subject).",
  "assessment": {{
    "subject": "subject_id",
    "concept": "concept_id or null",
    "activity": "RETRIEVAL",
    "dimensions": {{
      "knowledge": null,
      "retrieval": null,
      "application": null,
      "explanation": null,
      "transfer": null
    }},
    "misconceptions": [],
    "evidence": "Concrete evidence from latest learner answer."
  }},
  "session": {{
    "continue": true,
    "resume_instruction": "Learner-facing reconstruction question.",
    "session_thread": "Established: ... Open: ...",
    "last_question": "The exact question you just asked the learner."
  }}
}}

routing.action: continue | switch | new_subject | list_subjects | clarify
- switch: subject_id must be an existing id; response MUST be ""
- new_subject: proposed_title short; response MUST be ""
- list_subjects: response MUST be "" (Python prints the list)
- clarify: ask which subject in response
- continue: normal teaching; response is the only learner-facing text
- taxonomy (optional): parent_id, related_ids, add_aliases — only when clearly useful

session.session_thread: 2–4 sentences on what is established and still open.
session.last_question: the question in this turn's response (anti-repeat).

Assessment dimensions must be either null or numbers between 0.0 and 1.0.
Only assess dimensions with evidence this turn.
A misconception must be supported by evidence.
One main question at a time.
Do not mention this JSON protocol to the learner.
"""


def build_messages(base_prompt, state, conversation, policy=None):
    system_message = build_system_message(
        base_prompt,
        state,
        policy=policy,
    )
    return [
        {"role": "system", "content": system_message},
        *conversation[-MAX_CONVERSATION_MESSAGES:],
    ]


def print_policy(policy):
    """One-line log of deterministic activity choice."""
    if not policy:
        return
    act = policy.get("activity") or "FREE"
    reason = policy.get("reason") or ""
    force = "force" if policy.get("force") else "soft"
    cid = policy.get("concept_id") or "-"
    print(c(f"  [policy] activity={act} ({force}) concept={cid} | {reason}", GRAY))


# ============================================================
# SESSION MANAGEMENT
# ============================================================

def get_last_resume_for_subject(state, subject_id):
    """Return the most recent resume_instruction for a subject from history or active session."""
    # Prefer active session if same subject
    active = state.get("active_session", {})
    if active.get("subject_id") == subject_id and active.get("resume_instruction"):
        return active["resume_instruction"]

    for h in reversed(state.get("history", [])):
        if h.get("subject_id") == subject_id and h.get("resume_instruction"):
            return h["resume_instruction"]
    return None


def get_recent_concepts(state, subject_id, limit=3):
    """Return list of (concept_id, title) sorted by last_practiced desc."""
    subject = state.get("subjects", {}).get(subject_id, {})
    concepts = subject.get("concepts", {})
    items = []
    for cid, cpt in concepts.items():
        items.append((
            cid,
            cpt.get("title", cid),
            cpt.get("last_practiced") or ""
        ))
    items.sort(key=lambda x: x[2], reverse=True)
    return [(cid, title) for cid, title, _ in items[:limit]]


def is_learner_facing_resume(text):
    """Heuristic: reject internal notes and garbled model output."""
    if not text or not isinstance(text, str):
        return False
    t = text.strip().lower()
    if len(t) < 15:
        return False
    bad_starts = (
        "we moeten", "we bespreken", "vraag naar", "next:",
        "bespreek", "verder uitdiepen", "the next", "continue with"
    )
    for b in bad_starts:
        if t.startswith(b):
            return False
    # Garbled / non-physical phrases sometimes hallucinated into resumes
    garbage = (
        "strakke energie", "technische hapering", "technisch probleem",
        "system error", "n/a (user"
    )
    for g in garbage:
        if g in t:
            return False
    # Must look like a question or reconstruction prompt for the learner
    if "?" in text or t.startswith(("kun je", "leg ", "wat ", "how ", "can you", "explain")):
        return True
    return len(t) >= 40  # long instructional prompt without ? still ok


def make_fallback_resume(state, subject_id):
    """Generate a simple learner-facing resume question from state."""
    concepts = get_recent_concepts(state, subject_id, limit=2)
    if concepts:
        names = " en ".join(title for _, title in concepts)
        return (
            f"We waren bezig met {names}. "
            f"Kun je in eigen woorden reconstrueren wat je daarover begreep?"
        )
    subject = state.get("subjects", {}).get(subject_id, {})
    title = subject.get("title", subject_id)
    return (
        f"We pakken {title} weer op. "
        f"Wat weet je nog van de vorige keer?"
    )


def show_resume_banner(state, subject_id):
    """
    Print a learner-facing resume block:
    - subject + goal
    - recent concepts
    - reconstruction question
    """
    subject = state.get("subjects", {}).get(subject_id, {})
    title = subject.get("title", subject_id)
    goal = subject.get("goal")
    concepts = get_recent_concepts(state, subject_id, limit=3)

    resume = get_last_resume_for_subject(state, subject_id)
    if not is_learner_facing_resume(resume):
        resume = make_fallback_resume(state, subject_id)

    print()
    print(c(f"Hervat: {title}", BOLD + GREEN))
    if goal:
        print(f"Leerdoel : {goal[:100]}{'...' if len(str(goal)) > 100 else ''}")
    if concepts:
        names = ", ".join(f"{t}" for _, t in concepts)
        print(f"Recent   : {names}")
    print()
    print(c("Eerst even ophalen:", YELLOW))
    print(resume)
    print()

    # Single question on resume — store for anti-repeat / continuity
    active = state.get("active_session") or {}
    if resume:
        active["last_tutor_question"] = str(resume).strip()[:300]
        active["resume_instruction"] = str(resume).strip()
        recent = list(active.get("recent_questions") or [])
        if not recent or recent[-1] != active["last_tutor_question"]:
            recent.append(active["last_tutor_question"])
        active["recent_questions"] = recent[-5:]


def start_session(state, subject_id=None, pending_goal=False, is_resume=False):

    now = datetime.now().isoformat()

    # Carry over last concept / activity / resume / thread when resuming
    prev_concept = None
    prev_activity = None
    prev_resume = None
    prev_thread = None
    if is_resume and subject_id:
        for h in reversed(state.get("history", [])):
            if h.get("subject_id") == subject_id:
                prev_concept = h.get("concept_id")
                prev_resume = h.get("resume_instruction")
                break
        old = state.get("active_session", {})
        if old.get("subject_id") == subject_id:
            prev_concept = old.get("current_concept_id") or prev_concept
            prev_activity = old.get("current_activity")
            prev_resume = old.get("resume_instruction") or prev_resume
            prev_thread = old.get("session_thread")

    state["active_session"] = {
        "active": True,
        "subject_id": subject_id,
        "started_at": now,
        "last_activity": now,
        "question_number": 0,
        "hint_count": 0,
        "current_concept_id": prev_concept if is_resume else None,
        "current_activity": "RETRIEVAL" if is_resume else None,
        "resume_instruction": prev_resume if is_resume else None,
        "pending_goal": pending_goal,
        "just_resumed": bool(is_resume),
        "session_thread": prev_thread if is_resume else None,
        "last_tutor_question": prev_resume if is_resume else None,
        "recent_questions": (
            [prev_resume] if (is_resume and prev_resume) else []
        ),
    }

    save_state(state)


def apply_taxonomy_proposals(state, routing, focus_subject_id=None):
    """
    Conservatively apply optional routing.taxonomy proposals from the LLM.
    Never deletes subjects. Only parent/related/aliases.
    Returns list of human-readable change notes (for debug log).
    """
    if not isinstance(routing, dict):
        return []

    tax = routing.get("taxonomy")
    if not isinstance(tax, dict):
        # allow flat optional fields for simpler models
        tax = {}
        if routing.get("parent_id"):
            tax["parent_id"] = routing.get("parent_id")
        if routing.get("related_ids"):
            tax["related_ids"] = routing.get("related_ids")
        if routing.get("add_aliases"):
            tax["add_aliases"] = routing.get("add_aliases")
        if not tax:
            return []

    notes = []
    subjects = state.get("subjects") or {}
    focus = focus_subject_id or (state.get("active_session") or {}).get("subject_id")
    focus = normalize_id(focus) if focus else None

    # parent_id for focus (or explicit child_id)
    parent_id = tax.get("parent_id")
    child_id = normalize_id(tax.get("child_id") or focus or "")
    if parent_id and child_id:
        pid = resolve_subject_id(state, parent_id, title=str(parent_id))
        if not pid:
            pid = normalize_id(parent_id)
        if pid in subjects and child_id in subjects and pid != child_id:
            if link_subjects(state, child_id, pid, related=False):
                notes.append(f"parent: {child_id} → {pid}")

    # related links
    for rid in tax.get("related_ids") or []:
        if not focus:
            break
        other = resolve_subject_id(state, rid, title=str(rid)) or normalize_id(rid)
        if other in subjects and focus in subjects and other != focus:
            if link_subjects(state, focus, other, related=True):
                notes.append(f"related: {focus} ↔ {other}")

    # aliases on focus subject (soft merge: name maps to existing id)
    if focus and focus in subjects:
        aliases = subjects[focus].setdefault("aliases", [])
        for a in tax.get("add_aliases") or []:
            a = str(a).strip()
            if not a or len(a) > 60:
                continue
            if a not in aliases and normalize_id(a) != focus:
                aliases.append(a)
                notes.append(f"alias: {focus} += {a!r}")

    # concept aliases under focus
    concept_id = normalize_id(tax.get("concept_id") or "")
    if focus and concept_id and focus in subjects:
        concepts = subjects[focus].get("concepts") or {}
        if concept_id in concepts:
            caliases = concepts[concept_id].setdefault("aliases", [])
            for a in tax.get("concept_aliases") or []:
                a = str(a).strip()
                if a and normalize_id(a) and normalize_id(a) not in caliases:
                    caliases.append(a)
                    notes.append(f"concept alias: {concept_id} += {a!r}")

    if notes:
        state.setdefault("meta", {}).setdefault("taxonomy_events", []).append({
            "date": datetime.now().isoformat(),
            "type": "llm_taxonomy",
            "notes": notes,
        })
        state["meta"]["taxonomy_events"] = state["meta"]["taxonomy_events"][-40:]

    return notes


def apply_routing(state, routing, conversation):
    """
    Handle LLM routing intent.
    Returns:
      None          → continue normal assessment path
      "handled"     → list/clarify done, skip assessment
      "switched"    → subject switched, skip assessment
      "new_subject" → new subject created, skip assessment
    """
    if not isinstance(routing, dict):
        return None

    action = str(routing.get("action") or "continue").strip().lower()

    # Taxonomy proposals can apply on any action (including continue)
    # Deferred until we know the focus subject after switch/new.

    if action in ("", "continue", "none"):
        notes = apply_taxonomy_proposals(state, routing)
        if notes:
            print(c(f"  [taxonomy] {'; '.join(notes)}", GRAY))
        return None

    # ----- list_subjects -----
    if action == "list_subjects":
        print_subjects(state)
        return "handled"

    # ----- clarify -----
    if action == "clarify":
        return "handled"

    # ----- switch -----
    if action == "switch":
        raw_id = routing.get("subject_id") or routing.get("subject")
        if not raw_id:
            return None

        # Resolve against existing subjects (id, title, aliases)
        resolved = resolve_subject_id(state, raw_id, title=str(raw_id))
        matches = find_matching_subjects(state, str(raw_id))
        if resolved and resolved in state.get("subjects", {}):
            best_id = resolved
        elif matches and matches[0][1] >= 50:
            best_id = matches[0][0]
        elif normalize_id(raw_id) in state.get("subjects", {}):
            best_id = normalize_id(raw_id)
        else:
            print()
            print(c(
                f"Onderwerp \"{raw_id}\" niet gevonden in je state.",
                YELLOW
            ))
            print("Beschikbare onderwerpen:")
            print_subjects(state)
            return "handled"

        active = state["active_session"]
        if active.get("active") and active.get("subject_id") != best_id:
            close_session(state, active.get("resume_instruction"))

        start_session(state, best_id, is_resume=True)
        conversation.clear()
        show_resume_banner(state, best_id)
        apply_taxonomy_proposals(state, routing, focus_subject_id=best_id)
        return "switched"

    # ----- new_subject -----
    if action == "new_subject":
        title = routing.get("proposed_title") or routing.get("subject")
        if not title or not str(title).strip():
            return None

        title = str(title).strip()
        if len(title) > 60 or title.count(" ") > 5:
            print()
            print(c(
                "Voorgestelde titel is te lang. Gebruik /new <kort onderwerp>.",
                YELLOW
            ))
            return "handled"

        active = state["active_session"]
        if active.get("active"):
            close_session(state, active.get("resume_instruction"))

        # Optional parent from taxonomy
        tax = routing.get("taxonomy") if isinstance(routing.get("taxonomy"), dict) else {}
        parent_id = tax.get("parent_id") or routing.get("parent_id")
        if parent_id:
            parent_id = resolve_subject_id(state, parent_id, title=str(parent_id)) or normalize_id(parent_id)
            if parent_id not in state.get("subjects", {}):
                parent_id = None

        subject_id = ensure_subject(state, title, title, parent_id=parent_id)
        start_session(state, subject_id, pending_goal=True)
        conversation.clear()

        notes = apply_taxonomy_proposals(state, routing, focus_subject_id=subject_id)

        print()
        print(c(f"Nieuw onderwerp: {title}", GREEN))
        if parent_id and parent_id in state["subjects"]:
            print(c(
                f"  (onder: {state['subjects'][parent_id].get('title', parent_id)})",
                GRAY
            ))
        if notes:
            print(c(f"  [taxonomy] {'; '.join(notes)}", GRAY))
        print()
        print("Wat wil je met dit onderwerp bereiken?")
        print("(Beschrijf je leerdoel in één of twee zinnen.)")
        print()
        return "new_subject"

    return None


def close_session(state, resume_instruction=None):

    active = state["active_session"]

    if resume_instruction:
        active["resume_instruction"] = resume_instruction

    active["active"] = False
    active["last_activity"] = datetime.now().isoformat()
    active["just_resumed"] = False

    state["history"].append({
        "date": datetime.now().isoformat(),
        "subject_id": active.get("subject_id"),
        "concept_id": active.get("current_concept_id"),
        "question_number": active.get("question_number", 0),
        "resume_instruction": active.get("resume_instruction")
    })

    state["history"] = state["history"][-100:]
    save_state(state)


# ============================================================
# STATE DISPLAY
# ============================================================

def print_state_summary(state):

    divider()

    print(
        c(
            "LEARNER STATE",
            BOLD + MAGENTA
        )
    )

    divider()

    print()

    print(
        f"Subjects: {len(state['subjects'])}"
    )

    total_concepts = 0

    for subject_id, subject in state[
        "subjects"
    ].items():

        concepts = subject.get(
            "concepts",
            {}
        )

        total_concepts += len(
            concepts
        )

        print()
        print(
            c(
                f"  {subject.get('title', subject_id)}",
                BOLD
            )
        )
        goal = subject.get("goal")
        if goal:
            print(f"    goal: {str(goal)[:70]}{'...' if len(str(goal)) > 70 else ''}")

        for concept_id, concept in concepts.items():

            mastery = average_mastery(
                concept
            )

            print(
                f"    {concept_id}: "
                f"{mastery:.2f} "
                f"(K {concept.get('knowledge', 0):.2f}, "
                f"R {concept.get('retrieval', 0):.2f}, "
                f"A {concept.get('application', 0):.2f}, "
                f"E {concept.get('explanation', 0):.2f}, "
                f"T {concept.get('transfer', 0):.2f})"
            )

            open_mc = sum(
                1
                for m in concept.get(
                    "misconceptions",
                    []
                )
                if (
                    isinstance(m, dict)
                    and m.get(
                        "status",
                        "open"
                    ) != "resolved"
                )
            )

            if open_mc:
                print(
                    c(
                        f"      misconceptions open: {open_mc}",
                        YELLOW
                    )
                )

    print()

    print(
        f"Concepts: {total_concepts}"
    )

    print(
        f"Due reviews: {len(state.get('review_queue', []))}"
    )

    print()

    active = state[
        "active_session"
    ]

    print(
        f"Active session: {active.get('active')}"
    )

    print(
        f"Current subject: {active.get('subject_id')}"
    )

    print(
        f"Current concept: {active.get('current_concept_id')}"
    )

    print(
        f"Questions: {active.get('question_number', 0)}"
    )
    thread = active.get("session_thread")
    if thread:
        print(f"Thread: {thread[:120]}{'...' if len(str(thread)) > 120 else ''}")
    last_q = active.get("last_tutor_question")
    if last_q:
        print(f"Last Q: {last_q[:100]}{'...' if len(str(last_q)) > 100 else ''}")

    print()

    divider()


# ============================================================
# COMMANDS
# ============================================================

def print_help():

    print()
    print(c("COMMANDS", BOLD + CYAN))
    print()
    print("  /state              Show learner summary")
    print("  /state full         Show complete JSON state")
    print("  /subjects           List all subjects")
    print("  /new <subject>      Start a new subject/session")
    print("  /switch <subject>   Save current session and switch")
    print("  /resume             Resume saved session")
    print("  /review             Show due reviews")
    print("  /reset              Reset conversation context")
    print("  /quit               Save and exit")
    print("  /help               Show commands")
    print()
    print(c("Natuurlijke taal (elke taal):", GRAY))
    print("  \"ik wil verder met agents\"")
    print("  \"let's go back to the sun\"")
    print("  \"welke onderwerpen heb ik?\"")
    print("  \"je veux apprendre le Python\"")
    print()


def print_subjects(state):

    divider()
    print(c("SUBJECTS", BOLD + MAGENTA))
    divider()
    print()

    subjects = state.get("subjects", {})

    if not subjects:
        print("Nog geen onderwerpen. Start met /new <onderwerp>")
        print()
        return

    for sid, subject in sorted(subjects.items(), key=lambda x: x[1].get("title", x[0]).lower()):
        title = subject.get("title", sid)
        goal = subject.get("goal")
        concepts = subject.get("concepts", {})
        n_concepts = len(concepts)
        n_open_mc = 0
        for cpt in concepts.values():
            for m in cpt.get("misconceptions", []):
                if isinstance(m, dict) and m.get("status", "open") != "resolved":
                    n_open_mc += 1

        print(c(f"  {title}", BOLD))
        print(f"    id        : {sid}")
        pid = subject.get("parent_id")
        if pid and pid in subjects:
            print(f"    parent    : {subjects[pid].get('title', pid)}")
        related = subject.get("related") or []
        if related:
            rel_titles = [
                subjects[r].get("title", r) if r in subjects else r
                for r in related[:5]
            ]
            print(f"    related   : {', '.join(rel_titles)}")
        if goal:
            print(f"    goal      : {goal[:80]}{'...' if len(str(goal)) > 80 else ''}")
        print(f"    concepts  : {n_concepts}")
        print(f"    status    : {subject_status_line(state, sid)}")
        if n_open_mc:
            print(c(f"    open MC   : {n_open_mc}", YELLOW))
        print()

    divider()


def print_reviews(state):

    rebuild_review_queue(
        state
    )

    queue = state[
        "review_queue"
    ]

    divider()

    print(
        c(
            "REVIEW QUEUE",
            BOLD + YELLOW
        )
    )

    divider()

    print()

    if not queue:
        print("Geen reviews verschuldigd.")
        print()
        return

    for item in queue:

        subject_id = item[
            "subject_id"
        ]

        concept_id = item[
            "concept_id"
        ]

        print(
            f"  {subject_id} / {concept_id}"
        )

    print()


# ============================================================
# MAIN TUTOR
# ============================================================

def main():

    print()

    print(
        c(
            "============================================================",
            CYAN
        )
    )

    print(
        c(
            "                 SOCRATIC OFFLINE TUTOR",
            BOLD + CYAN
        )
    )

    print(
        c(
            "                         v0.4.0",
            CYAN
        )
    )

    print(
        c(
            "============================================================",
            CYAN
        )
    )

    print()

    print(f"Model : {MODEL_NAME}")
    print(f"Think : {THINK}  (False = no chain-of-thought)")
    print(f"Stream: {STREAM}")
    print(f"State : {STATE_FILE}")

    print()

    state = load_state()
    base_prompt = load_system_prompt()

    rebuild_review_queue(
        state
    )

    save_state(state)

    conversation = []

    # --------------------------------------------------------
    # RESUME EXISTING SESSION
    # --------------------------------------------------------

    active = state[
        "active_session"
    ]

    if active.get("active") and active.get("subject_id"):
        print(c("Een actieve sessie is gevonden.", YELLOW))
        # Mark as just resumed so first LLM turn prefers retrieval
        active["just_resumed"] = True
        active["current_activity"] = active.get("current_activity") or "RETRIEVAL"
        save_state(state)
        show_resume_banner(state, active["subject_id"])

    else:

        n_subjects = len(state.get("subjects", {}))
        if n_subjects > 0:
            print(f"Je hebt {n_subjects} onderwerp(en). Hervat met:")
            print(c('  "ik wil verder met agents"', CYAN))
            print("  /subjects")
            print("  /new <nieuw onderwerp>")
            print()
        else:
            print("Typ /new <onderwerp> om te beginnen.")
            print()
            print("Voorbeelden:")
            print("  /new AI-agents")
            print("  /new Python")
            print("  /new waterzuivering")
            print()
            print("Daarna beschrijf je in gewone taal wat je wilt leren.")
            print()

    # --------------------------------------------------------
    # MAIN INPUT LOOP
    # --------------------------------------------------------

    while True:

        # IMPORTANT:
        # We print YOU only once per actual input.
        # No automatic question is inserted after every response.

        show_you_prompt()

        try:
            user_input = input("> ")

        except (
            KeyboardInterrupt,
            EOFError
        ):

            print()

            print(
                c(
                    "Sessie wordt opgeslagen...",
                    YELLOW
                )
            )

            close_session(
                state,
                state[
                    "active_session"
                ].get(
                    "resume_instruction"
                )
            )

            print(
                c(
                    "Saved.",
                    GREEN
                )
            )

            break

        user_input = user_input.strip()

        # Empty ENTER should do NOTHING.
        if not user_input:
            continue

        # ----------------------------------------------------
        # COMMANDS
        # ----------------------------------------------------

        if user_input.lower() == "/quit":

            active = state[
                "active_session"
            ]

            close_session(
                state,
                active.get(
                    "resume_instruction"
                )
            )

            print()

            print(
                c(
                    "Learner state saved.",
                    GREEN
                )
            )

            print(
                "Session ended."
            )

            print()

            break

        if user_input.lower() == "/help":

            print_help()
            continue

        if user_input.lower() == "/state":

            print_state_summary(
                state
            )

            continue

        if user_input.lower() == "/state full":

            divider()

            print(
                json.dumps(
                    state,
                    indent=2,
                    ensure_ascii=False
                )
            )

            divider()

            continue

        if user_input.lower() == "/subjects":
            print_subjects(state)
            continue

        if user_input.lower() == "/review":
            print_reviews(state)
            continue

        if user_input.lower() == "/reset":

            conversation = []

            print()

            print(
                c(
                    "Conversation context reset.",
                    YELLOW
                )
            )

            print(
                "Learner state blijft behouden."
            )

            print()

            continue

        # ----------------------------------------------------
        # NEW SUBJECT
        # ----------------------------------------------------

        if user_input.lower().startswith("/new "):

            subject_name = user_input[5:].strip()

            if not subject_name:
                print("Gebruik: /new <onderwerp>")
                continue

            if state["active_session"].get("active"):
                close_session(
                    state,
                    state["active_session"].get("resume_instruction")
                )

            subject_id = ensure_subject(
                state,
                subject_name,
                subject_name
            )

            start_session(state, subject_id, pending_goal=True)
            conversation = []

            print()
            print(c(f"Nieuw onderwerp: {subject_name}", GREEN))
            print()
            print("Wat wil je met dit onderwerp bereiken?")
            print("(Beschrijf je leerdoel in één of twee zinnen.)")
            print()
            continue

        # ----------------------------------------------------
        # SWITCH SUBJECT
        # ----------------------------------------------------

        if user_input.lower().startswith(
            "/switch "
        ):

            subject_name = user_input[
                8:
            ].strip()

            if not subject_name:

                print(
                    "Gebruik: /switch <onderwerp>"
                )

                continue

            if state[
                "active_session"
            ].get("active"):

                close_session(
                    state,
                    state[
                        "active_session"
                    ].get(
                        "resume_instruction"
                    )
                )

            subject_id = ensure_subject(
                state,
                subject_name,
                subject_name
            )

            start_session(
                state,
                subject_id
            )

            conversation = []

            print()

            print(
                c(
                    f"Overgeschakeld naar: {subject_name}",
                    GREEN
                )
            )

            print()

            continue

        # ----------------------------------------------------
        # RESUME
        # ----------------------------------------------------

        if user_input.lower() == "/resume":

            active = state["active_session"]

            if active.get("active"):
                print("De huidige sessie is al actief.")
                continue

            history = state.get("history", [])
            if not history:
                print("Geen vorige sessie om te hervatten.")
                continue

            last = history[-1]
            subject_id = last.get("subject_id")
            if not subject_id:
                print("Laatste sessie bevat geen onderwerp.")
                continue

            start_session(state, subject_id, is_resume=True)
            conversation = []
            show_resume_banner(state, subject_id)
            continue

        # ----------------------------------------------------
        # PENDING GOAL (first message after /new or new_subject)
        # This message is ONLY the goal — not a learning answer.
        # ----------------------------------------------------

        active = state["active_session"]

        if active.get("active") and active.get("pending_goal"):
            sid = active.get("subject_id")
            goal_text = user_input.strip()
            if sid and sid in state["subjects"]:
                state["subjects"][sid]["goal"] = goal_text
                state["subjects"][sid]["updated_at"] = datetime.now().isoformat()
            active["pending_goal"] = False
            save_state(state)
            print()
            print(c("Leerdoel opgeslagen.", GREEN))
            print()

            # Feed the LLM a clear "start teaching" signal, not the raw goal
            # as if it were an answer to a content question.
            title = state["subjects"].get(sid, {}).get("title", sid)
            start_msg = (
                f"Leerdoel voor '{title}' is vastgelegd: {goal_text}\n\n"
                f"Start nu de les. Stel één korte Socratische openingsvraag "
                f"om te peilen wat de leerling al weet over dit onderwerp. "
                f"Gebruik routing.action = continue."
            )
            conversation.clear()
            conversation.append({
                "role": "user",
                "content": start_msg
            })
            state["active_session"]["question_number"] = 1
            state["active_session"]["last_activity"] = datetime.now().isoformat()
            save_state(state)

            # Jump straight to model call (skip re-appending user_input)
            show_status("Generating...")
            try:
                policy = suggest_next_activity(state)
                print_policy(policy)
                messages = build_messages(base_prompt, state, conversation, policy=policy)
                raw, result = ask_model_with_json_retry(messages)
                print_model_stats()
            except requests.exceptions.ConnectionError:
                print()
                print(c("ERROR: Could not connect to Ollama.", RED))
                conversation.pop()
                continue
            except Exception as e:
                print()
                print(c("ERROR communicating with model:", RED))
                print(str(e))
                conversation.pop()
                continue

            if not result:
                answer = (raw or "").strip()
                print()
                print(c("Warning: model returned non-JSON output (after retry).", YELLOW))
                show_tutor(
                    answer if answer else "...",
                    already_streamed=getattr(ask_model, "did_stream_response", False),
                )
                conversation.append({"role": "assistant", "content": answer})
                save_state(state)
                continue

            answer = str(result.get("response", "")).strip() or "Wat weet je al over dit onderwerp?"
            session_info = result.get("session", {})
            if isinstance(session_info, dict):
                ri = session_info.get("resume_instruction")
                if ri and is_learner_facing_resume(str(ri)):
                    state["active_session"]["resume_instruction"] = str(ri).strip()
            update_session_memory(state, session_info if isinstance(session_info, dict) else {}, answer)
            state["active_session"]["just_resumed"] = False
            state["active_session"]["last_activity"] = datetime.now().isoformat()
            save_state(state)
            show_tutor(
                answer,
                already_streamed=getattr(ask_model, "did_stream_response", False),
            )
            conversation.append({"role": "assistant", "content": answer})
            save_state(state)
            continue

        # ----------------------------------------------------
        # SLASH COMMAND ALIASES + UNKNOWN
        # ----------------------------------------------------

        cmd_lower = user_input.lower().strip()

        if cmd_lower in ("/subject", "/subjects"):
            print_subjects(state)
            continue

        if user_input.startswith("/") and not cmd_lower.startswith(
            ("/new ", "/switch ", "/state", "/subjects", "/subject",
             "/review", "/reset", "/quit", "/help", "/resume")
        ):
            print()
            print(c(f"Onbekend commando: {user_input.split()[0]}", YELLOW))
            print("Typ /help voor de lijst met commando's.")
            print()
            continue

        # ----------------------------------------------------
        # USER MESSAGE → LLM (intent / routing is LLM-owned)
        # Explicit /commands above still short-circuit.
        # ----------------------------------------------------

        conversation.append({
            "role": "user",
            "content": user_input
        })

        if active.get("active"):
            state["active_session"]["question_number"] += 1
            state["active_session"]["last_activity"] = datetime.now().isoformat()
            save_state(state)

        # ----------------------------------------------------
        # MODEL CALL (with one JSON retry)
        # ----------------------------------------------------

        show_status("Generating...")

        try:
            policy = suggest_next_activity(state)
            print_policy(policy)
            messages = build_messages(
                base_prompt,
                state,
                conversation,
                policy=policy,
            )
            raw, result = ask_model_with_json_retry(messages)
            print_model_stats()

        except requests.exceptions.ConnectionError:
            print()
            print(c("ERROR: Could not connect to Ollama.", RED))
            print()
            print("Controleer bijvoorbeeld:")
            print("  systemctl status ollama")
            print("  ollama list")
            print()
            conversation.pop()
            continue

        except Exception as e:
            print()
            print(c("ERROR communicating with model:", RED))
            print(str(e))
            print()
            conversation.pop()
            continue

        # ----------------------------------------------------
        # NON-JSON FALLBACK
        # ----------------------------------------------------

        if not result:
            answer = (raw or "").strip()
            print()
            print(c("Warning: model returned non-JSON output (after retry).", YELLOW))
            preview = answer[:400] + ("..." if len(answer) > 400 else "")
            if preview and not getattr(ask_model, "did_stream_response", False):
                print(c(f"Raw preview: {preview}", GRAY))
            print()
            show_tutor(
                answer if answer else "Ik kon geen geldig antwoord genereren.",
                already_streamed=getattr(ask_model, "did_stream_response", False),
            )
            conversation.append({
                "role": "assistant",
                "content": answer
            })
            save_state(state)
            continue

        answer = str(result.get("response", "")).strip()
        if not answer:
            answer = (
                "Ik kon geen geldig tutorantwoord "
                "uit de modelrespons halen."
            )

        assessment = result.get("assessment", {})
        session_info = result.get("session", {})
        routing = result.get("routing", {})

        # ----------------------------------------------------
        # APPLY ROUTING (LLM-owned intent)
        # ----------------------------------------------------

        routed = apply_routing(state, routing, conversation)
        if routed == "handled":
            # list_subjects: subjects already printed — no extra LLM monologue
            # clarify: show the disambiguation question
            action = str((routing or {}).get("action") or "").lower()
            if action == "clarify" and answer:
                show_tutor(
                    answer,
                    already_streamed=getattr(ask_model, "did_stream_response", False),
                )
                conversation.append({"role": "assistant", "content": answer})
            elif action == "list_subjects":
                # Close the history turn so the next user message is not
                # answered as a double-reply to "welke onderwerpen..."
                subjects = state.get("subjects", {})
                names = [
                    s.get("title", sid)
                    for sid, s in subjects.items()
                ]
                summary = (
                    "Overzicht getoond. Onderwerpen: "
                    + (", ".join(names) if names else "geen")
                    + ". Wacht op keuze van de leerling."
                )
                conversation.append({
                    "role": "assistant",
                    "content": summary
                })
            save_state(state)
            continue
        if routed == "switched":
            # Resume banner already posed the retrieval question.
            # Skip a second stacked question from the LLM.
            save_state(state)
            continue
        if routed == "new_subject":
            # Goal prompt already shown by apply_routing.
            # Do NOT show the LLM teaching question yet — that would
            # interleave with the goal request.
            save_state(state)
            continue

        # ----------------------------------------------------
        # APPLY ASSESSMENT (only on continue)
        # ----------------------------------------------------

        # If no active session but LLM continued teaching, open session
        if not state["active_session"].get("active"):
            sid = None
            if isinstance(assessment, dict):
                sid = assessment.get("subject")
            if sid and is_valid_id(sid):
                start_session(state, ensure_subject(state, sid, sid))
            elif state.get("subjects"):
                # fallback: stay without session — assessment may still create subject
                pass

        try:
            apply_assessment(state, assessment)
        except Exception as e:
            print(c(f"State update warning: {e}", YELLOW))

        # ----------------------------------------------------
        # APPLY SESSION INFORMATION
        # ----------------------------------------------------

        if isinstance(session_info, dict):
            resume_instruction = session_info.get("resume_instruction")
            if resume_instruction:
                text = str(resume_instruction).strip()
                if is_learner_facing_resume(text):
                    state["active_session"]["resume_instruction"] = text
                elif not state["active_session"].get("resume_instruction"):
                    sid = state["active_session"].get("subject_id")
                    state["active_session"]["resume_instruction"] = (
                        make_fallback_resume(state, sid) if sid else text
                    )

        # Working memory: thread + anti-repeat question log
        update_session_memory(state, session_info if isinstance(session_info, dict) else {}, answer)

        # After first successful turn, resume mode is done
        state["active_session"]["just_resumed"] = False
        state["active_session"]["last_activity"] = datetime.now().isoformat()

        rebuild_review_queue(state)
        save_state(state)

        # ----------------------------------------------------
        # DISPLAY ANSWER (skip body if already live-streamed)
        # ----------------------------------------------------

        show_tutor(
            answer,
            already_streamed=getattr(ask_model, "did_stream_response", False),
        )

        # ----------------------------------------------------
        # STORE CLEAN CONVERSATION
        # ----------------------------------------------------

        conversation.append({
            "role": "assistant",
            "content": answer
        })

        # Do not allow conversation context to grow forever.
        if len(conversation) > (
            MAX_CONVERSATION_MESSAGES
        ):

            conversation = conversation[
                -MAX_CONVERSATION_MESSAGES:
            ]

        save_state(state)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()