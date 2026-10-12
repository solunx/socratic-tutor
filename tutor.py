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
METRICS_DIR = BASE_DIR / "metrics"

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

# --- State hygiene (disk growth without losing mastery signal) ---
MAX_HISTORY_ENTRIES = 40          # session breadcrumbs only
MAX_EVIDENCE_PER_CONCEPT = 8      # recent evidence windows
MAX_MISCONCEPTIONS_PER_CONCEPT = 8

CONCEPT_PARENT_DEFAULTS = {
    "zon": {
        "quantum_tunneling": "nuclear_fusion",
        "kernsamenstelling": "nuclear_fusion",
        "sterke_kernkracht": "nuclear_fusion",
        "hydrostatisch_evenwicht": "nuclear_fusion",
        "energieniveaus": "kernsamenstelling",
        "spectrale_lijnen": "energieniveaus",
        "zwakke_kernkracht": "nuclear_fusion",
        "pp_chain": "nuclear_fusion",
        "coulomb_barriere": "quantum_tunneling",
    },
    "ai_agents": {
        "tool_use": "agent",
        "harness": "agent",
    },
}


MAX_OUTPUT_TOKENS = 700           # phase-3: room for full JSON (response+routing+assessment+session)


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


def prune_learner_state(state):
    """Keep mastery signal; drop bulk that does not help teaching.

    - history: last MAX_HISTORY_ENTRIES only
    - evidence: last MAX_EVIDENCE_PER_CONCEPT per concept
    - misconceptions: prefer open/improving; drop oldest resolved beyond cap
    - active_session.open_threads: drop closed threads older than the last 3 open/scaffolded
    - stale open MCs (>30 days, medium, no remediation activity) → improving (hygiene)
    Does NOT delete subjects, concepts, dimension scores, or FSRS fields.
    """
    if not isinstance(state, dict):
        return state
    hist = state.get("history")
    if isinstance(hist, list) and len(hist) > MAX_HISTORY_ENTRIES:
        state["history"] = hist[-MAX_HISTORY_ENTRIES:]

    # Compact open_threads: keep open/scaffolded + last few closed
    active = state.get("active_session")
    if isinstance(active, dict):
        threads = active.get("open_threads")
        if isinstance(threads, list) and len(threads) > 6:
            opens = [t for t in threads if isinstance(t, dict) and t.get("status") in ("open", "scaffolded", "hinted")]
            closed = [t for t in threads if isinstance(t, dict) and t.get("status") == "closed"]
            active["open_threads"] = (opens + closed)[-6:]

    now = datetime.now()
    for sub in (state.get("subjects") or {}).values():
        if not isinstance(sub, dict):
            continue
        for concept in (sub.get("concepts") or {}).values():
            if not isinstance(concept, dict):
                continue
            ev = concept.get("evidence")
            if isinstance(ev, list) and len(ev) > MAX_EVIDENCE_PER_CONCEPT:
                concept["evidence"] = ev[-MAX_EVIDENCE_PER_CONCEPT:]
            mcs = concept.get("misconceptions")
            if not isinstance(mcs, list):
                continue
            # Hygiene: very old open medium MCs without id → mark improving (stop force loops)
            for m in mcs:
                if not isinstance(m, dict):
                    continue
                if m.get("status", "open") != "open":
                    continue
                if str(m.get("severity", "")).lower() == "high":
                    continue  # high stays until explicitly resolved
                created = m.get("created_at")
                if not created:
                    continue
                try:
                    dt = datetime.fromisoformat(str(created).replace("Z", ""))
                    age_days = (now - dt).days
                except Exception:
                    continue
                if age_days >= 30:
                    m["status"] = "improving"
                    m["resolved_hint"] = m.get("resolved_hint") or "auto: stale open MC (>30d)"
            if len(mcs) <= MAX_MISCONCEPTIONS_PER_CONCEPT:
                continue
            openish = [m for m in mcs if isinstance(m, dict) and m.get("status", "open") != "resolved"]
            resolved = [m for m in mcs if isinstance(m, dict) and m.get("status") == "resolved"]
            strings = [m for m in mcs if not isinstance(m, dict)]
            kept = openish + strings
            room = MAX_MISCONCEPTIONS_PER_CONCEPT - len(kept)
            if room > 0:
                kept.extend(resolved[-room:])
            else:
                kept = kept[-MAX_MISCONCEPTIONS_PER_CONCEPT:]
            concept["misconceptions"] = kept
    return state



def clamp_fsrs_fields(state):
    """Prevent runaway FSRS stability from noisy grades / /state turns."""
    for sub in (state.get("subjects") or {}).values():
        if not isinstance(sub, dict):
            continue
        for c in (sub.get("concepts") or {}).values():
            if not isinstance(c, dict):
                continue
            st = c.get("stability")
            if isinstance(st, (int, float)) and st > 3650:  # ~10 years max
                c["stability"] = 3650.0
            diff = c.get("difficulty")
            if isinstance(diff, (int, float)):
                c["difficulty"] = max(1.0, min(10.0, float(diff)))
    return state


def save_state(state):
    try:
        hygiene_misconceptions(state)
    except Exception:
        pass
    try:
        clamp_fsrs_fields(state)
    except Exception:
        pass
    try:
        ensure_concept_tree(state)
    except Exception:
        pass
    prune_learner_state(state)
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
        parent_guess = None
        defaults = CONCEPT_PARENT_DEFAULTS.get(subject_id) or {}
        parent_guess = defaults.get(concept_id)
        active = state.get("active_session") or {}
        if not parent_guess and active.get("subject_id") == subject_id:
            cur = active.get("current_concept_id")
            if cur and cur in concepts and cur != concept_id:
                # new concept under current focus as soft parent only if default says so
                parent_guess = defaults.get(concept_id) or None
        if parent_guess and parent_guess not in concepts:
            parent_guess = None
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
            "parent_concept_id": parent_guess,
            "aliases": [],
            "prerequisites": ([{"id": parent_guess, "strength": "soft"}] if parent_guess else []),
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


def has_open_misconception(concept, include_improving=False):
    """True if concept has unresolved misconceptions worth probing.

    By default only status=="open" counts. status=="improving" is progress —
    do not keep forcing MISCONCEPTION_CHECK every turn for those.
    """
    for item in concept.get("misconceptions") or []:
        if isinstance(item, dict):
            st = item.get("status", "open")
            if st == "resolved":
                continue
            if st == "improving" and not include_improving:
                continue
            if st in ("open", "improving"):
                return True
        elif isinstance(item, str):
            return True
    return False


def open_misconceptions(concept, severity=None, include_improving=False):
    """Return open misconception dicts; optional severity filter (high/medium/low)."""
    out = []
    for item in concept.get("misconceptions") or []:
        if not isinstance(item, dict):
            continue
        st = item.get("status", "open")
        if st == "resolved":
            continue
        if st == "improving" and not include_improving:
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

    # 0a) Explicit "leg uit" or repeated stuck → FULL TEACH then close thread
    if active.get("force_full_teach"):
        active["force_full_teach"] = False
        active["force_scaffold"] = False
        active["stuck_signal"] = False
        ot = get_open_thread(active)
        focus_q = (ot or {}).get("q") if ot else None
        if ot:
            ot["status"] = "closed"
            ot["closed_reason"] = "full_teach"
        return {
            "activity": "EXPLANATION",
            "concept_id": cid,
            "subject_id": sid,
            "reason": (
                "FULL TEACH / SLOW: learner asked explanation, formula, or slow-down. "
                "Max ONE new technical term this turn, defined in plain words first. "
                "No misleading analogy. No chain of new jargon in one reply. "
                "Close the open gap, then ONE easy check on a NEW angle."
                + (f" | thread: {focus_q[:70]}" if focus_q else "")
            ),
            "force": True,
            "scaffold": True,
            "full_teach": True,
            "close_thread": True,
            "open_thread_q": focus_q,
        }

    # 0a2) Soft stuck → scaffold once (not endless re-ask)
    if active.get("force_scaffold") or active.get("stuck_signal"):
        active["force_scaffold"] = False
        active["stuck_signal"] = False
        mark_thread_scaffolded(active)
        ot = get_open_thread(active)
        focus_q = (ot or {}).get("q") if ot else None
        tries = int((ot or {}).get("tries") or 0)
        # If already scaffolded many times, escalate to full teach
        if tries >= 3:
            if ot:
                ot["status"] = "closed"
                ot["closed_reason"] = "full_teach_after_tries"
            return {
                "activity": "EXPLANATION",
                "concept_id": cid,
                "subject_id": sid,
                "reason": (
                    "FULL TEACH after failed scaffolds — answer completely "
                    "using learner's partial ideas, close thread, easy check"
                ),
                "force": True,
                "full_teach": True,
                "close_thread": True,
                "open_thread_q": focus_q,
            }
        return {
            "activity": "EXPLANATION",
            "concept_id": cid,
            "subject_id": sid,
            "reason": (
                "learner stuck — build on what THEY already said, add one missing "
                "piece (2–4 sentences), then ONE simpler question"
                + (f" | {focus_q[:70]}" if focus_q else "")
            ),
            "force": True,
            "scaffold": True,
            "open_thread_q": focus_q,
        }

    # 0b) Open thread: after 3 tries → FULL TEACH and CLOSE (stop the loop)
    ot = get_open_thread(active)
    if ot and int(ot.get("tries") or 0) >= 3 and ot.get("status") != "closed":
        focus_q = ot.get("q")
        ot["status"] = "closed"
        ot["closed_reason"] = "max_tries_full_teach"
        return {
            "activity": "EXPLANATION",
            "concept_id": cid,
            "subject_id": sid,
            "reason": (
                f"FULL TEACH — open thread tries={ot.get('tries')}: give the answer "
                f"now (use learner's analogies), close gap, ONE easy check — "
                f"{(focus_q or '')[:80]}"
            ),
            "force": True,
            "full_teach": True,
            "close_thread": True,
            "open_thread_q": focus_q,
        }
    # tries 1–2: stay on thread but BUILD ON learner words, not pure re-ask
    if ot and int(ot.get("tries") or 0) >= 1 and active.get("must_advance"):
        active["must_advance"] = False
        return {
            "activity": "EXPLANATION",
            "concept_id": cid,
            "subject_id": sid,
            "reason": (
                "open thread — quote/accept what learner got right, add ONE missing "
                f"piece, do not re-ask the same question: {(ot.get('q') or '')[:80]}"
            ),
            "force": True,
            "scaffold": True,
            "open_thread_q": ot.get("q"),
        }

    # 0b) After a correct (or solid partial) answer: advance activity type.
    # Domain-agnostic: do not re-quiz the same explanation angle.
    verdict = str(active.get("last_verdict") or "").lower()
    if verdict in ("correct", "ok") or (
        verdict == "partial" and int(active.get("same_activity_streak") or 0) >= 1
    ):
        last_act = (active.get("current_activity") or active.get("prev_activity") or "").upper()
        if last_act in ("EXPLANATION", "RETRIEVAL", "MISCONCEPTION_CHECK", ""):
            # Prefer application, then transfer if application already strong
            nxt = "APPLICATION"
            if concept:
                try:
                    app = float(concept.get("application") or 0)
                    tr = float(concept.get("transfer") or 0)
                    if app >= 0.45 and tr < 0.45:
                        nxt = "TRANSFER"
                    elif app >= 0.45 and tr >= 0.45:
                        nxt = "RETRIEVAL"  # spaced light check, not same explain
                except Exception:
                    pass
            active["last_verdict"] = None  # consume so we don't loop this forever
            return {
                "activity": nxt,
                "concept_id": cid,
                "subject_id": sid,
                "reason": (
                    f"verdict was {verdict} — advance to {nxt} on a NEW angle "
                    "(do not re-ask the same 'explain why' check)"
                ),
                "force": True,
            }

    # 0) Hard anti-repeat / activity-loop break (Python-enforced)
    if active.get("must_advance") or int(active.get("same_activity_streak") or 0) >= 3:
        active["must_advance"] = False
        last_act = active.get("current_activity") or active.get("prev_activity")
        candidates = ["EXPLANATION", "APPLICATION", "RETRIEVAL"]
        nxt = next((a for a in candidates if a != last_act), "EXPLANATION")
        streak = int(active.get("repeat_streak") or 0)
        active["same_activity_streak"] = 0
        return {
            "activity": nxt,
            "concept_id": cid,
            "subject_id": sid,
            "reason": f"must advance (repeat_streak={streak}, broke loop from {last_act})",
            "force": True,
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

    # 2) High-severity open misconception — force limited (not forever)
    if concept:
        high = open_misconceptions(concept, severity="high")
        if high:
            qn = int(active.get("question_number") or 0)
            mc_forces = int(active.get("mc_force_count") or 0)
            mastery = average_mastery(concept)
            # Skip if mastery already OK, or already forced 2x this session
            if mastery < 0.45 and mc_forces < 2 and (qn == 0 or qn % 5 == 1):
                desc = str(high[0].get("description", ""))[:60]
                active["mc_force_count"] = mc_forces + 1
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
    # IMPORTANT: do not push TRANSFER while K/R/E are still thin — that
    # produces endless "compare to another phenomenon" loops.
    if concept:
        scores = {}
        for d in DIMENSIONS:
            v = clamp(concept.get(d))
            scores[d] = 0.0 if v is None else v
        weakest = min(scores, key=scores.get)
        if scores[weakest] < WEAK_DIM_THRESHOLD:
            act = DIM_TO_ACTIVITY.get(weakest, "RETRIEVAL")
            foundation = min(
                scores.get("knowledge", 0),
                scores.get("retrieval", 0),
                scores.get("explanation", 0),
            )
            if act == "TRANSFER" and foundation < 0.5:
                # Rebuild understanding first; transfer later
                act = "EXPLANATION" if scores.get("explanation", 0) < 0.5 else "APPLICATION"
            # Cap consecutive TRANSFER even when foundation is OK
            prev = (active.get("current_activity") or active.get("prev_activity") or "")
            same = int(active.get("same_activity_streak") or 0)
            if act == "TRANSFER" and prev == "TRANSFER" and same >= 1:
                act = "APPLICATION"
            return {
                "activity": act,
                "concept_id": cid,
                "subject_id": sid,
                "reason": f"weak dimension {weakest}={scores[weakest]:.2f} → {act}",
                "force": False,
            }

    # 6) Open misconception — soft, rare (high severity already handled above)
    qn = int(active.get("question_number") or 0)
    if concept and has_open_misconception(concept):
        n_open = len(open_misconceptions(concept))
        # At most every 4th question — never dominate the session
        if qn > 0 and qn % 4 == 0:
            return {
                "activity": "MISCONCEPTION_CHECK",
                "concept_id": cid,
                "subject_id": sid,
                "reason": f"open MC soft check (n_open={n_open}, q={qn})",
                "force": False,
            }

    # 6b) Breadth-before-depth mastery gate
    # Stay on current concept until a solid "bubble" exists (avg mastery + few open MCs).
    # Blocks soft jumps to brand-new named concepts while foundations are thin.
    if concept and cid:
        m = average_mastery(concept)
        n_open = len(open_misconceptions(concept))
        if m < 0.45 or n_open >= 3:
            # Prefer explanation/retrieval on SAME concept — not transfer to elsewhere
            scores = {d: (clamp(concept.get(d)) or 0.0) for d in DIMENSIONS}
            weakest = min(scores, key=scores.get)
            act = DIM_TO_ACTIVITY.get(weakest, "EXPLANATION")
            if act == "TRANSFER":
                act = "EXPLANATION"
            return {
                "activity": act,
                "concept_id": cid,
                "subject_id": sid,
                "reason": (
                    f"mastery gate: stay on {cid} "
                    f"(avg={m:.2f}, open_mc={n_open}) — consolidate before new topic"
                ),
                "force": False,
            }

    # 7) Free choice
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
    dims_early = (assessment or {}).get("dimensions") or {}
    early_scores = [
        clamp(dims_early.get(d))
        for d in DIMENSIONS
        if clamp(dims_early.get(d)) is not None
    ]
    early_avg = sum(early_scores) / len(early_scores) if early_scores else None
    if has_open_misconception(concept) and open_misconceptions(concept, severity="high"):
        if early_avg is None or early_avg < 0.35:
            return 1  # Again — still struggling

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
            # Upgrade soft → hard if proposed stronger; never downgrade.
            if strength == "hard":
                for e in edges:
                    if isinstance(e, dict):
                        eid = normalize_id(e.get("id") or e.get("concept_id") or "")
                        if eid == pid and str(e.get("strength") or "soft").lower() != "hard":
                            e["strength"] = "hard"
                            notes.append(f"prereq upgrade: {concept_id} ← {pid} (hard)")
                    elif isinstance(e, str) and normalize_id(e) == pid:
                        idx = edges.index(e)
                        edges[idx] = {"id": pid, "strength": "hard"}
                        notes.append(f"prereq upgrade: {concept_id} ← {pid} (hard)")
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
    hygiene_misconceptions(state)
    update_pending_catalog(state)
    return commit_pending_to_catalog(state)


def hygiene_misconceptions(state):
    """Dedupe, merge near-duplicates, age out stale opens — prevent MC wildgrowth.

    - Same fingerprint within one concept → keep newest, merge severity (max)
    - Near-duplicate descriptions (token Jaccard ≥ 0.55) → merge into one
    - Medium/low open older than 14 days → improving
    - Medium/low open older than 45 days → resolved (noise)
    - High open older than 60 days with no evidence string → improving
    - Cap still applied in prune_learner_state
    """
    if not isinstance(state, dict):
        return 0
    now = datetime.now()
    changed = 0

    def _age_days(m):
        created = m.get("created_at")
        if not created:
            return 0
        try:
            dt = datetime.fromisoformat(str(created).replace("Z", ""))
            return max(0, (now - dt).days)
        except Exception:
            return 0

    def _sev_rank(s):
        return {"high": 3, "medium": 2, "low": 1}.get(str(s or "medium").lower(), 2)

    for sid, sub in (state.get("subjects") or {}).items():
        if not isinstance(sub, dict):
            continue
        for cid, concept in (sub.get("concepts") or {}).items():
            if not isinstance(concept, dict):
                continue
            mcs = concept.get("misconceptions")
            if not isinstance(mcs, list) or not mcs:
                continue

            # 1) Age statuses
            for m in mcs:
                if not isinstance(m, dict):
                    continue
                st = m.get("status", "open")
                if st == "resolved":
                    continue
                age = _age_days(m)
                sev = str(m.get("severity") or "medium").lower()
                if st == "open" and sev in ("medium", "low") and age >= 7:
                    m["status"] = "improving"
                    m["resolved_hint"] = m.get("resolved_hint") or "auto: aged open→improving"
                    changed += 1
                elif st in ("open", "improving") and sev in ("medium", "low") and age >= 21:
                    m["status"] = "resolved"
                    m["resolved_hint"] = m.get("resolved_hint") or "auto: stale MC closed"
                    changed += 1
                elif st == "open" and sev == "high" and age >= 30:
                    # High stays open longer, but not forever without recovery
                    m["status"] = "improving"
                    m["resolved_hint"] = m.get("resolved_hint") or "auto: aged high→improving"
                    changed += 1
                elif st == "improving" and sev == "high" and age >= 60:
                    m["status"] = "resolved"
                    m["resolved_hint"] = m.get("resolved_hint") or "auto: stale high closed"
                    changed += 1

            # 2) Dedupe by fingerprint / near-duplicate description
            kept = []
            for m in mcs:
                if not isinstance(m, dict):
                    kept.append(m)
                    continue
                fp = _mc_fingerprint(m.get("description"), m.get("id"))
                desc_tokens = set(re.findall(r"[a-z0-9à-ÿ]+", str(m.get("description") or "").lower()))
                merged = False
                for k in kept:
                    if not isinstance(k, dict):
                        continue
                    if k.get("status") == "resolved" and m.get("status") != "resolved":
                        # prefer keeping active over resolved duplicate
                        pass
                    kfp = _mc_fingerprint(k.get("description"), k.get("id"))
                    if fp and kfp and fp == kfp:
                        # merge into k
                        if _sev_rank(m.get("severity")) > _sev_rank(k.get("severity")):
                            k["severity"] = m.get("severity")
                        # keep more active status
                        order = {"open": 0, "improving": 1, "resolved": 2}
                        if order.get(m.get("status", "open"), 9) < order.get(k.get("status", "open"), 9):
                            k["status"] = m.get("status")
                        if m.get("id") and not k.get("id"):
                            k["id"] = m.get("id")
                        if m.get("evidence") and not k.get("evidence"):
                            k["evidence"] = m.get("evidence")
                        merged = True
                        changed += 1
                        break
                    k_tokens = set(re.findall(r"[a-z0-9à-ÿ]+", str(k.get("description") or "").lower()))
                    if desc_tokens and k_tokens:
                        j = len(desc_tokens & k_tokens) / max(len(desc_tokens | k_tokens), 1)
                        d1 = str(m.get("description") or "").lower()[:80]
                        d2 = str(k.get("description") or "").lower()[:80]
                        prefix_hit = len(d1) > 30 and (d1 in d2 or d2 in d1)
                        if j >= 0.45 or prefix_hit:
                            if _sev_rank(m.get("severity")) > _sev_rank(k.get("severity")):
                                k["severity"] = m.get("severity")
                            order = {"open": 0, "improving": 1, "resolved": 2}
                            if order.get(m.get("status", "open"), 9) < order.get(k.get("status", "open"), 9):
                                k["status"] = m.get("status")
                            # prefer longer description
                            if len(str(m.get("description") or "")) > len(str(k.get("description") or "")):
                                k["description"] = m.get("description")
                            if m.get("id") and not k.get("id"):
                                k["id"] = m.get("id")
                            merged = True
                            changed += 1
                            break
                if not merged:
                    kept.append(m)
            concept["misconceptions"] = kept
    return changed


# Default concept parent map (subject-local). Used when parent_concept_id is null.

def ensure_concept_tree(state, subject_id=None):
    """Fill parent_concept_id from defaults + session focus; no cycles.

    Also adds soft related links between sibling concepts under same parent.
    Safe to call often; only writes when parent is missing.
    """
    subjects = state.get("subjects") or {}
    sids = [subject_id] if subject_id else list(subjects.keys())
    notes = []
    for sid in sids:
        sid = normalize_id(sid) if sid else None
        if not sid or sid not in subjects:
            continue
        concepts = subjects[sid].get("concepts") or {}
        defaults = CONCEPT_PARENT_DEFAULTS.get(sid) or {}
        # Also: if current focus has no parent and we just came from another concept, soft-link
        active = state.get("active_session") or {}
        focus = active.get("current_concept_id")
        for cid, cpt in concepts.items():
            if not isinstance(cpt, dict):
                continue
            parent = cpt.get("parent_concept_id")
            if parent:
                # validate still exists
                if parent not in concepts:
                    cpt["parent_concept_id"] = None
                    parent = None
                else:
                    continue
            # defaults table
            want = defaults.get(cid)
            if want and want in concepts and want != cid:
                cpt["parent_concept_id"] = want
                notes.append(f"{sid}/{cid} → {want}")
                continue
            # heuristic: if name contains known parent token
            for p_candidate in concepts:
                if p_candidate == cid:
                    continue
                if p_candidate in cid or (len(p_candidate) > 4 and p_candidate in str(cpt.get("title") or "").lower().replace(" ", "_")):
                    # weak; only if default missing
                    pass
        # prerequisites soft: child should list parent as soft prereq
        for cid, cpt in concepts.items():
            if not isinstance(cpt, dict):
                continue
            parent = cpt.get("parent_concept_id")
            if not parent or parent not in concepts:
                continue
            prereqs = cpt.setdefault("prerequisites", [])
            # normalize to list of {id, strength}
            ids = set()
            for p in prereqs:
                if isinstance(p, str):
                    ids.add(normalize_id(p))
                elif isinstance(p, dict) and p.get("id"):
                    ids.add(normalize_id(p["id"]))
            if parent not in ids:
                prereqs.append({"id": parent, "strength": "soft"})
                notes.append(f"prereq soft: {cid} needs {parent}")
    if notes:
        state.setdefault("meta", {}).setdefault("taxonomy_events", []).append({
            "date": datetime.now().isoformat(),
            "type": "concept_tree",
            "notes": notes[:20],
        })
        events = state["meta"].get("taxonomy_events") or []
        state["meta"]["taxonomy_events"] = events[-40:]
    return notes


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



def _auto_resolve_misconceptions(concept, assessment, verdict=None):
    """Demote open MCs when the learner demonstrates recovery.

    Domain-agnostic: uses verdict, mastery, and LLM-reported MC status —
    never topic-specific keyword lists.
    """
    if not concept:
        return 0
    changed = 0
    dims = (assessment or {}).get("dimensions") or {}
    scores = [clamp(dims.get(d)) for d in DIMENSIONS if clamp(dims.get(d)) is not None]
    mastery = average_mastery(concept)
    turn_ok = (sum(scores) / len(scores)) >= 0.45 if scores else False
    verdict = str(verdict or (assessment or {}).get("verdict") or "").lower()
    solid = (
        verdict in ("correct", "true", "ok")
        or mastery >= 0.45
        or turn_ok
    )

    # Explicit statuses from model assessment first
    for item in (assessment or {}).get("misconceptions") or []:
        if not isinstance(item, dict):
            continue
        want = str(item.get("status") or "").lower()
        if want not in ("resolved", "improving"):
            continue
        for m in concept.get("misconceptions") or []:
            if not isinstance(m, dict):
                continue
            if m.get("status") == "resolved":
                continue
            match = False
            if item.get("id") and item.get("id") == m.get("id"):
                match = True
            else:
                idesc = str(item.get("description") or "").lower()
                desc = str(m.get("description") or "").lower()
                if idesc and desc and (idesc[:40] in desc or desc[:40] in idesc):
                    match = True
            if match and m.get("status") != want:
                m["status"] = want
                m["resolved_hint"] = m.get("resolved_hint") or f"auto: assessment→{want}"
                changed += 1

    if not solid:
        return changed

    # After a solid turn: open medium/low → improving; high → improving
    # if mastery already decent. Full resolve needs stronger mastery.
    for m in concept.get("misconceptions") or []:
        if not isinstance(m, dict):
            continue
        st = m.get("status", "open")
        if st == "resolved":
            continue
        sev = str(m.get("severity") or "medium").lower()
        if st == "open":
            if sev in ("medium", "low") and (mastery >= 0.40 or turn_ok or verdict in ("correct", "true", "ok")):
                m["status"] = "improving"
                m["resolved_hint"] = m.get("resolved_hint") or "auto: solid turn→improving"
                changed += 1
            elif sev == "high" and mastery >= 0.50:
                m["status"] = "improving"
                m["resolved_hint"] = m.get("resolved_hint") or "auto: mastery≥0.5 high→improving"
                changed += 1
        if m.get("status") == "improving" and mastery >= 0.60 and verdict in ("correct", "true", "ok"):
            m["status"] = "resolved"
            m["resolved_hint"] = m.get("resolved_hint") or "auto: mastery≥0.6 + correct→resolved"
            changed += 1
    return changed


def apply_assessment(
    state,
    assessment
):

    if not isinstance(
        assessment,
        dict
    ):
        return None

    # Immediate correctness signal (pedagogy: feedback close to the error)
    verdict = str(assessment.get("verdict") or "").strip().lower()
    if verdict in ("incorrect", "wrong", "false"):
        active = state.get("active_session") or {}
        active["last_verdict"] = "incorrect"
        # Prefer misconception remediation next turn
        active["must_advance"] = False
        active["pending_verdict_feedback"] = True
    elif verdict in ("correct", "true", "ok", "partial"):
        active = state.get("active_session") or {}
        active["last_verdict"] = verdict if verdict != "ok" else "correct"
        active["pending_verdict_feedback"] = False
        if verdict in ("correct", "true", "ok"):
            cur = get_open_thread(active)
            if cur:
                cur["status"] = "closed"
                cur["closed_reason"] = "verdict_correct"

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

    _auto_resolve_misconceptions(concept, assessment, verdict=verdict)
    # Periodic hygiene (dedupe + age) after every scored turn
    hygiene_misconceptions(state)

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
        pass  # mc_force_count set only on forced high-MC policy returns

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

    # Common model slip: trailing commas before } or ]
    try:
        fixed = re.sub(r",\s*([}\]])", r"\1", text)
        value = json.loads(fixed)
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


def ask_model(messages, stream=None, on_response_delta=None, live_print=None):
    """
    Call Ollama native /api/chat.
    Field name is "think" (not "thinking"). THINK=False disables CoT.
    When STREAM is True, live-prints the learner-facing response field
    as tokens arrive (JSON is still collected in full for parsing).

    on_response_delta(text_so_far, delta): optional callback for web SSE.
    live_print: override console printing (default = STREAM and no callback).
    """
    think_val = _normalize_think(THINK)
    use_stream = STREAM if stream is None else bool(stream)
    if on_response_delta is not None:
        use_stream = True
    if live_print is None:
        live_print = bool(STREAM) and on_response_delta is None

    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": use_stream,
        "think": think_val,
        # Force structured JSON — cuts most parse failures / second-call retries
        "format": "json",
        "options": {
            "temperature": TEMPERATURE,
            "num_predict": MAX_OUTPUT_TOKENS,
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
        prompt_ns = data.get("prompt_eval_duration") or 0
        eval_ns = data.get("eval_duration") or 0
        load_ns = data.get("load_duration") or 0
        eval_n = data.get("eval_count") or 0
        prompt_n = data.get("prompt_eval_count") or 0
        total_s = (total_ns / 1e9) if total_ns else None
        eval_s = (eval_ns / 1e9) if eval_ns else None
        ask_model.last_stats = {
            "model": MODEL_NAME,
            "think_requested": think_val,
            "thinking_returned": bool(thinking_text),
            "thinking_chars": len(thinking_text),
            "eval_count": eval_n,
            "prompt_eval_count": prompt_n,
            "total_s": round(total_s, 2) if total_s is not None else None,
            "prompt_eval_s": round(prompt_ns / 1e9, 3) if prompt_ns else None,
            "eval_s": round(eval_s, 3) if eval_s is not None else None,
            "load_s": round(load_ns / 1e9, 3) if load_ns else None,
            "ttft_s": round(prompt_ns / 1e9, 3) if prompt_ns else None,
            "out_tok_per_s": round(eval_n / eval_s, 1) if eval_s and eval_n else None,
            "streamed": False,
        }
        try:
            from metrics_lib import log_call_from_stats
            log_call_from_stats(ask_model.last_stats)
        except Exception:
            pass
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
    prompt_ns = 0
    eval_ns = 0
    load_ns = 0
    header_shown = False
    printed_len = 0
    ttft_s = None

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
            if ttft_s is None:
                ttft_s = (datetime.now() - t0).total_seconds()
        if think_piece:
            thinking_parts.append(think_piece)

        if data.get("eval_count"):
            eval_count = data["eval_count"]
        if data.get("prompt_eval_count"):
            prompt_eval_count = data["prompt_eval_count"]
        if data.get("total_duration"):
            total_ns = data["total_duration"]
        if data.get("prompt_eval_duration"):
            prompt_ns = data["prompt_eval_duration"]
        if data.get("eval_duration"):
            eval_ns = data["eval_duration"]
        if data.get("load_duration"):
            load_ns = data["load_duration"]

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
                    if live_print:
                        print()
                        divider()
                        print(c("TUTOR", BOLD + GREEN))
                        divider()
                        print()
                    header_shown = True
                    ask_model.did_stream_response = True
                if len(partial) > printed_len:
                    delta = partial[printed_len:]
                    printed_len = len(partial)
                    if live_print:
                        print(delta, end="", flush=True)
                    if on_response_delta is not None:
                        try:
                            on_response_delta(partial, delta)
                        except Exception:
                            pass
        elif action in ("list_subjects", "switch", "new_subject"):
            # Explicitly do not stream the response body
            pass

        if data.get("done"):
            break

    if header_shown and live_print:
        print()  # end streamed body

    content = "".join(full_content)
    thinking_text = "".join(thinking_parts)
    if not content and thinking_text:
        content = thinking_text

    elapsed = (datetime.now() - t0).total_seconds()
    total_s = (total_ns / 1e9) if total_ns else elapsed
    eval_s = (eval_ns / 1e9) if eval_ns else None
    # Prefer wall-clock first token; fall back to prompt_eval_duration
    if ttft_s is None and prompt_ns:
        ttft_s = prompt_ns / 1e9
    ask_model.last_stats = {
        "model": MODEL_NAME,
        "think_requested": think_val,
        "thinking_returned": bool(thinking_text),
        "thinking_chars": len(thinking_text),
        "eval_count": eval_count,
        "prompt_eval_count": prompt_eval_count,
        "total_s": round(total_s, 2),
        "prompt_eval_s": round(prompt_ns / 1e9, 3) if prompt_ns else None,
        "eval_s": round(eval_s, 3) if eval_s is not None else None,
        "load_s": round(load_ns / 1e9, 3) if load_ns else None,
        "ttft_s": round(ttft_s, 3) if ttft_s is not None else None,
        "out_tok_per_s": round(eval_count / eval_s, 1) if eval_s and eval_count else None,
        "streamed": True,
    }
    try:
        from metrics_lib import log_call_from_stats
        log_call_from_stats(ask_model.last_stats)
    except Exception:
        pass
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


def questions_similar(a, b, threshold=0.55):
    """Token-overlap similarity for anti-repeat (Dutch/English).

    Uses intersection / min(|a|,|b|) so a short rephrase of a longer
    question still counts as a repeat.
    """
    if not a or not b:
        return False
    a, b = str(a).strip().lower(), str(b).strip().lower()
    if a == b:
        return True
    for x in ("?", "؟", "！", "!"):
        a = a.replace(x, " ")
        b = b.replace(x, " ")
    def toks(s):
        return set(re.findall(r"[a-z0-9à-ÿ]{3,}", s))
    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return False
    return (len(ta & tb) / min(len(ta), len(tb))) >= threshold



def _ensure_open_threads(active):
    threads = active.get("open_threads")
    if not isinstance(threads, list):
        threads = []
        active["open_threads"] = threads
    return threads


def get_open_thread(active):
    """Most recent non-closed thread, or None."""
    for t in reversed(_ensure_open_threads(active)):
        if isinstance(t, dict) and t.get("status") in ("open", "scaffolded", "hinted"):
            return t
    return None


def close_open_threads_matching(active, question_text, reason="answered"):
    """Mark threads similar to question_text as closed."""
    if not question_text:
        return 0
    n = 0
    for t in _ensure_open_threads(active):
        if t.get("status") == "closed":
            continue
        if questions_similar(t.get("q") or "", question_text, threshold=0.45):
            t["status"] = "closed"
            t["closed_reason"] = reason
            n += 1
    return n


def push_or_bump_open_thread(active, question_text, activity=None):
    """Register tutor question as open thread; bump tries if same thread."""
    if not question_text:
        return
    threads = _ensure_open_threads(active)
    q = str(question_text).strip()[:280]
    # Same as current open → bump tries
    cur = get_open_thread(active)
    if cur and questions_similar(cur.get("q") or "", q, threshold=0.5):
        cur["tries"] = int(cur.get("tries") or 0) + 1
        if activity:
            cur["activity"] = activity
        return cur
    # Close older opens if we clearly moved to a new distinct question
    # (keep max 2 open — force focus)
    opens = [t for t in threads if t.get("status") in ("open", "scaffolded", "hinted")]
    if len(opens) >= 2:
        # close the oldest open
        opens[0]["status"] = "closed"
        opens[0]["closed_reason"] = "superseded"
    # Unique ids even after capping list (len alone collides → all "t9")
    max_n = 0
    for t in threads:
        tid = str(t.get("id") or "")
        if tid.startswith("t") and tid[1:].isdigit():
            max_n = max(max_n, int(tid[1:]))
    entry = {
        "id": f"t{max_n + 1}",
        "q": q,
        "status": "open",
        "tries": 1,
        "activity": activity or active.get("current_activity"),
    }
    threads.append(entry)
    active["open_threads"] = threads[-8:]  # cap history
    return entry


def mark_thread_scaffolded(active):
    cur = get_open_thread(active)
    if cur:
        cur["status"] = "scaffolded"
        cur["tries"] = int(cur.get("tries") or 0) + 1
    return cur


def update_session_memory(state, session_info, response_text):
    """
    Update working memory: session_thread, last_tutor_question, recent_questions,
    open_threads (pedagogical questions that must be closed, not abandoned).
    """
    active = state["active_session"]
    if not isinstance(session_info, dict):
        session_info = {}

    last_q = session_info.get("last_question") or session_info.get("last_tutor_question")
    if last_q:
        last_q = str(last_q).strip()[:300]
    else:
        last_q = extract_question_from_text(response_text)

    if last_q:
        recent = list(active.get("recent_questions") or [])
        is_repeat = any(questions_similar(last_q, prev) for prev in recent[-5:])
        if is_repeat:
            active["must_advance"] = True
            active["repeat_streak"] = int(active.get("repeat_streak") or 0) + 1
        else:
            active["repeat_streak"] = 0

        active["last_tutor_question"] = last_q
        if not recent or not questions_similar(recent[-1], last_q):
            recent.append(last_q)
        active["recent_questions"] = recent[-5:]

        # Register as open pedagogical thread (or bump tries on same)
        push_or_bump_open_thread(
            active, last_q, activity=active.get("current_activity")
        )

    # LLM may signal thread closure
    closed = session_info.get("closed_thread") or session_info.get("close_thread")
    if closed:
        close_open_threads_matching(active, str(closed), reason="llm_closed")
    if session_info.get("thread_resolved") is True:
        cur = get_open_thread(active)
        if cur:
            cur["status"] = "closed"
            cur["closed_reason"] = "llm_resolved"

    thread = session_info.get("session_thread")
    if thread and str(thread).strip():
        active["session_thread"] = str(thread).strip()[:500]

    act = active.get("current_activity")
    prev = active.get("prev_activity")
    if act and act == prev:
        active["same_activity_streak"] = int(active.get("same_activity_streak") or 0) + 1
    else:
        active["same_activity_streak"] = 1
    active["prev_activity"] = act


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
    if s.get("ttft_s") is not None:
        parts.append(f"ttft={s['ttft_s']}s")
    if s.get("out_tok_per_s") is not None:
        parts.append(f"{s['out_tok_per_s']}tok/s")
    if streamed:
        parts.append("stream")
    if getattr(ask_model, "last_json_retry", False):
        parts.append("json_retry")
    if secs is not None:
        parts.append(f"{secs}s")
    print(c("  [" + " | ".join(parts) + "]", GRAY))
    # Optional one-line component sizes from last build
    try:
        from metrics_lib import get_last_breakdown
        bd = get_last_breakdown()
        if bd and bd.get("components"):
            top = sorted(
                bd["components"],
                key=lambda x: -(x.get("tokens_est") or 0),
            )[:3]
            bits = [f"{x['name']}≈{x.get('tokens_est', 0)}" for x in top]
            est = bd.get("total_tokens_est")
            ph = bd.get("prefix_hash") or "-"
            print(c(
                f"  [ctx est≈{est} | prefix={ph} | top: {', '.join(bits)}]",
                GRAY,
            ))
    except Exception:
        pass


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
    do exactly one retry with a short reminder (non-streaming).
    Returns (raw_text, parsed_dict_or_None).
    Sets ask_model.last_json_retry when a second call was needed.
    """
    ask_model.last_json_retry = False
    raw = ask_model(messages)
    parsed = extract_json_object(raw)

    if parsed is not None and isinstance(parsed, dict) and (
        "response" in parsed or "routing" in parsed
    ):
        if getattr(ask_model, "last_stats", None) is not None:
            ask_model.last_stats["json_retry"] = False
        return raw, parsed

    # First call produced invalid/truncated JSON (often num_predict cut).
    # Do NOT stream the retry — avoids a second TUTOR block on screen.
    first_streamed = bool(getattr(ask_model, "did_stream_response", False))
    if first_streamed:
        print()
        print(c("(antwoord opnieuw opbouwen…)", GRAY))
        print()

    failed_preview = (raw or "")[:400]
    retry_messages = list(messages) + [
        {"role": "assistant", "content": failed_preview},
        {"role": "user", "content": JSON_REMINDER},
    ]

    try:
        ask_model.last_json_retry = True
        # Force non-stream so only one final TUTOR body is shown via show_tutor
        raw2 = ask_model(retry_messages, stream=False)
        # Retry was silent: allow show_tutor to print the final response once
        ask_model.did_stream_response = False
        if getattr(ask_model, "last_stats", None) is not None:
            ask_model.last_stats["json_retry"] = True
        parsed2 = extract_json_object(raw2)
        if parsed2 is not None:
            return raw2, parsed2
        return raw2, None
    except Exception:
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
            if not isinstance(m, dict):
                continue
            if m.get("status", "open") != "open":
                continue
            entry = {
                "severity": m.get("severity", "medium"),
                "description": str(m.get("description", ""))[:80],
            }
            if m.get("id"):
                entry["id"] = m.get("id")
            open_mc.append(entry)
            if len(open_mc) >= 1:  # one open MC in prompt is enough
                break

        evidence = concept.get("evidence") or []
        last_ev = []
        for e in evidence[-1:]:
            if isinstance(e, dict):
                last_ev.append(str(e.get("evidence", ""))[:60])
            else:
                last_ev.append(str(e)[:60])

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
    def _clip(s, n=180):
        s = str(s or "")
        return s if len(s) <= n else s[: n - 1] + "…"

    recent_q = (active.get("recent_questions") or [])[-3:]
    ot = get_open_thread(active)
    open_list = []
    for t in (_ensure_open_threads(active))[-3:]:
        if t.get("status") == "closed":
            continue
        open_list.append({
            "q": _clip(t.get("q"), 140),
            "status": t.get("status"),
            "tries": int(t.get("tries") or 0),
        })
    working = {
        "subject_id": subject_id,
        "concept_id": active.get("current_concept_id") or policy.get("concept_id"),
        "activity": active.get("current_activity"),
        "question_number": active.get("question_number"),
        "session_thread": _clip(active.get("session_thread"), 280),
        "last_tutor_question": _clip(active.get("last_tutor_question"), 200),
        "recent_questions": [_clip(q, 160) for q in recent_q],
        "open_threads": open_list,
        "just_resumed": bool(active.get("just_resumed")),
        "pending_goal": bool(active.get("pending_goal")),
        "must_advance": bool(active.get("must_advance")),
        "repeat_streak": int(active.get("repeat_streak") or 0),
    }

    orchestrator = {
        "preferred_activity": policy.get("activity"),
        "preferred_concept_id": policy.get("concept_id"),
        "reason": policy.get("reason"),
        "force": bool(policy.get("force")),
        "scaffold": bool(policy.get("scaffold")),
        "full_teach": bool(policy.get("full_teach")),
        "open_thread_q": _clip(policy.get("open_thread_q") or ((ot or {}).get("q")), 160),
        "close_thread": bool(policy.get("close_thread")),
    }

    focus_cid = working.get("concept_id")
    mc_catalog = catalog_prompt_slice(subject_id, focus_cid, max_entries=4) if mode == "teach" else []

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
- If orchestrator.full_teach is true: STOP quizzing. Write 4–8 clear sentences
  that ANSWER the open point, preferably using the learner's own words/analogies
  (veer, hoogteverschil, …). End with one easy yes/no or fill-in check. Set
  session.thread_resolved=true. Do not re-ask the hard question.
- If orchestrator.scaffold is true: first QUOTE what the learner got right,
  then add the single missing piece (2–4 sentences), then ONE simpler question.
- If orchestrator.open_thread_q is set: stay on that point; never ignore a
  partial answer that already points the right way.
- If orchestrator.close_thread is true: fully close the gap this turn.
- When the learner asks what YOUR previous QUESTION means ("ik snap je vraag niet",
  "wat bedoel je met die vraag"): start with "Ik bedoel met die vraag: …"
  Only then. NEVER open a normal teaching turn with "Ik bedoel:".
  NEVER say "Je bedoelt:" when clarifying your own question.
  If they ask to slow down or unpack symbols/formulas, do NOT say "Ik bedoel".
  Just teach: name each symbol, one short sentence each, then one easy check.
- Set assessment.activity to the activity you actually used.
- Stay on preferred_concept_id when set.
- Never ask a pure TRANSFER/analogy question two turns in a row.
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

    static_rules = """
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

IMMEDIATE FEEDBACK (critical):
- If the learner stated something factually wrong, the FIRST sentence of
  response MUST clearly say it is incorrect (one short clause), then ask a
  Socratic question that repairs that specific error.
- Do not leave the learner unsure whether their last claim was right or wrong.
- Correct facts close in time to the error; then rebuild the right reasoning.

WHEN THE LEARNER IS STUCK (says "geen idee", "onduidelijk", "wat bedoel je"):
- Do NOT repeat the same abstract question or demand another analogy.
- First give a SHORT concrete scaffold (2–4 sentences of fact), then ONE
  simpler yes/no or fill-in question on that fact.
- If they ask what you meant by a question, answer that meta-question plainly
  in one sentence, then continue teaching — never refuse to clarify.

BUILD ON THE LEARNER (critical — reduce frustration):
- Always start from what THEY just said. Quote or paraphrase their words.
- If they are 70% right, say what is right FIRST, then the one missing piece.
- Do NOT ignore a good analogy (veer, hoogte, …) and later reuse it as if new.
- Never re-ask the identical conceptual question after a partial answer; either
  complete the answer with them or fully teach it.
- Clarifying YOUR own question only: "Ik bedoel met die vraag: …".
  Never open a formula/symbol explanation with "Ik bedoel".
  Never "Je bedoelt:" when they are confused about what you asked.

OPEN THREADS:
- Finish open points; do not loop the same quiz more than ~2 tries.
- After repeated stuck or "leg uit": FULL answer (4–8 sentences), then easy check.
- Set session.thread_resolved=true when the gap is closed.

NEVER:
- Keep the learner "in the dark" while fishing for a textbook phrase.
- Repeat the same question after "geen idee" without adding substance.
- Treat a near-correct answer as failure.

BREADTH BEFORE DEPTH:
- Consolidate the current concept (retrieve + explain in own words + one
  application) before introducing a new named concept or jumping far ahead.
- If the learner still has open confusion on A, deepen A1/A2 — do not skip to B/C.
- Respect explicit learner goals (e.g. "no unit conversion") — drop that thread.

FACTUAL ACCURACY & ANALOGIES (critical — many learners trust the tutor):
- Prefer a short correct mechanism over a catchy analogy.
- NEVER invent an analogy that misrepresents the physics (e.g. "heavy stone
  waves die sooner" for pion range — that is FALSE and must not be used).
- If you use an analogy, in the SAME turn state its limit in one clause
  ("this only illustrates X, not Y").
- When unsure of a detail, stay qualitative and conservative; do not bluff.

ONE NEW TERM PER TURN:
- Introduce at most ONE new technical name per response (any domain: jargon,
  symbol, named law, named component). Define it in plain language first.
- Do NOT dump a chain of new terms in one turn (A → B → C → D).
- If the learner says "go slow", "nieuwe concepten", "wtf", "te snel",
  "too fast", "ralentis": stop introducing names; define only the single
  term they asked about; one short check after.

NEW CONCEPT GATE:
- When the learner says a topic is new ("geen idee", "nieuw concept"),
  teach the role/why first (1–2 sentences), then mechanism in the next turn —
  not both at once with jargon.
"""

    output_schema = """
============================================================
REQUIRED OUTPUT
============================================================

Return ONLY valid JSON.

No markdown.
No ```json fences.
No text before or after the JSON.

Exact structure (routing FIRST):

{
  "routing": {
    "action": "continue",
    "subject_id": null,
    "proposed_title": null,
    "taxonomy": {
      "parent_id": null,
      "related_ids": [],
      "add_aliases": [],
      "concept_id": null,
      "concept_aliases": []
    }
  },
  "response": "Message shown to learner (empty for list_subjects/switch/new_subject).",
  "assessment": {
    "subject": "subject_id",
    "concept": "concept_id or null",
    "activity": "RETRIEVAL",
    "verdict": "correct | partial | incorrect | null",
    "dimensions": {
      "knowledge": null,
      "retrieval": null,
      "application": null,
      "explanation": null,
      "transfer": null
    },
    "misconceptions": [],
    "evidence": "Concrete evidence from latest learner answer."
  },
  "session": {
    "continue": true,
    "resume_instruction": "Learner-facing reconstruction question.",
    "session_thread": "Established: ... Open: ...",
    "last_question": "The exact question you just asked the learner.",
    "thread_resolved": false,
    "closed_thread": null
  }
}

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

    # Concrete questions to avoid (dynamic — kept AFTER fixed prefix)
    avoid_lines = []
    lq = working.get("last_tutor_question")
    if lq:
        avoid_lines.append(f"- last: {lq}")
    for q in (working.get("recent_questions") or [])[-3:]:
        if q and q != lq:
            avoid_lines.append(f"- {q}")
    for q in (active.get("banned_questions") or [])[-6:]:
        if q and all(q not in line for line in avoid_lines):
            avoid_lines.append(f"- BANNED: {q}")
    avoid_block = ""
    if avoid_lines:
        extra = ""
        if (
            working.get("must_advance")
            or int(active.get("repeat_streak") or 0) >= 1
            or (active.get("banned_questions") or [])
        ):
            extra = (
                "CRITICAL: Learner already answered or rejected a recent question. "
                "This turn you MUST ask a DIFFERENT question on a NEW angle "
                "(new scenario, new concept link, or deeper mechanism). "
                "NEVER rephrase any BANNED or recent question.\n"
            )
        avoid_block = (
            "\n============================================================\n"
            "DO NOT RE-ASK (verbatim or paraphrase)\n"
            "============================================================\n"
            + extra
            + "\n".join(avoid_lines)
            + "\nIf the learner already answered these, ADVANCE to a new angle.\n"
        )

    state_block = f"""============================================================
CURRENT LEARNER STATE ({mode})
============================================================

{state_json}
"""

    # --- Prefix cache strategy ---
    # FIXED first (byte-stable across turns when system_prompt.md unchanged):
    #   base_prompt → static_rules → output_schema → anti_repeat → policy_hint → catalog_hint
    # DYNAMIC last (changes every turn):
    #   state_context → resume_hint → avoid_block
    # Ollama can then reuse KV for the long fixed prefix when only state changes.
    parts = {
        "base_prompt": base_prompt or "",
        "static_rules": static_rules,
        "output_schema": output_schema,
        "anti_repeat": anti_repeat or "",
        "policy_hint": policy_hint or "",
        "catalog_hint": catalog_hint or "",
        "state_context": state_block,
        "resume_hint": resume_hint or "",
        "avoid_recent_questions": avoid_block,
    }

    fixed_prefix = "".join([
        parts["base_prompt"],
        "\n\n",
        parts["static_rules"],
        parts["output_schema"],
        parts["anti_repeat"],
        parts["policy_hint"],
        parts["catalog_hint"],
    ])
    dynamic_suffix = "".join([
        parts["state_context"],
        parts["resume_hint"],
        parts["avoid_recent_questions"],
    ])
    system_message = fixed_prefix + dynamic_suffix

    # Stash for metrics (no full text in logs — sizes only)
    try:
        from metrics_lib import build_breakdown_from_parts, prompt_hash, set_last_breakdown
        bd = build_breakdown_from_parts(parts, conversation=None)
        bd["mode"] = mode
        bd["prefix_hash"] = prompt_hash(fixed_prefix)
        bd["prefix_chars"] = len(fixed_prefix)
        bd["dynamic_chars"] = len(dynamic_suffix)
        bd["_parts"] = parts
        set_last_breakdown(bd)
    except Exception:
        pass

    return system_message


def build_messages(base_prompt, state, conversation, policy=None):
    system_message = build_system_message(
        base_prompt,
        state,
        policy=policy,
    )
    # Prefer messages after the last session-boundary marker so topic switches
    # do not leak prior-subject dialogue into the model context (UI keeps full log).
    raw = list(conversation or [])
    boundary = "[session boundary"
    cut = 0
    for i, m in enumerate(raw):
        if isinstance(m, dict) and boundary in str(m.get("content") or ""):
            cut = i + 1
    hist = [m for m in raw[cut:] if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    hist = hist[-MAX_CONVERSATION_MESSAGES:]
    try:
        from metrics_lib import (
            build_breakdown_from_parts,
            estimate_tokens,
            get_last_breakdown,
            prompt_hash,
            set_last_breakdown,
        )
        prev = get_last_breakdown() or {}
        parts = prev.pop("_parts", None) or {"system_message": system_message}
        if parts and "system_message" not in parts and "base_prompt" in parts:
            bd = build_breakdown_from_parts(parts, conversation=hist)
        else:
            bd = build_breakdown_from_parts({"system_message": system_message}, conversation=hist)
        bd["mode"] = prev.get("mode")
        bd["system_hash"] = prompt_hash(system_message)
        bd["system_chars"] = len(system_message)
        bd["system_tokens_est"] = estimate_tokens(system_message)
        if prev.get("prefix_hash"):
            bd["prefix_hash"] = prev["prefix_hash"]
            bd["prefix_chars"] = prev.get("prefix_chars")
            bd["dynamic_chars"] = prev.get("dynamic_chars")
        set_last_breakdown(bd)
    except Exception:
        pass
    return [
        {"role": "system", "content": system_message},
        *hist,
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
    active["question_number"] = 0
    active["hint_count"] = 0
    active["must_advance"] = False
    active["force_scaffold"] = False
    active["force_full_teach"] = False
    active["stuck_signal"] = False
    active["repeat_streak"] = 0
    # Keep at most one open thread as resume seed; drop closed clutter
    threads = active.get("open_threads") or []
    opens = [t for t in threads if isinstance(t, dict) and t.get("status") in ("open", "scaffolded")]
    active["open_threads"] = opens[-1:] if opens else []
    if active.get("recent_questions"):
        active["recent_questions"] = list(active["recent_questions"])[-2:]
    if active.get("banned_questions"):
        active["banned_questions"] = list(active["banned_questions"])[-4:]

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




def format_concept_tree(state):
    """Return concept tree as string (for web/CLI without printing)."""
    import io
    import re
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_concept_tree(state)
    raw = buf.getvalue()
    # Strip ANSI color codes for web / plain clients
    return re.sub(r"\[[0-9;]*m", "", raw).strip()

def print_concept_tree(state):
    """Show subject → concept parent/child tree + open MC counts."""
    print()
    print(c("─" * 60, GRAY))
    print(c("CONCEPT TREE", BOLD + CYAN))
    print(c("─" * 60, GRAY))
    subjects = state.get("subjects") or {}
    if not subjects:
        print("  (geen onderwerpen)")
        print()
        return
    for sid, sub in subjects.items():
        title = (sub or {}).get("title") or sid
        print(f"  {c(str(title), BOLD)}  ({sid})")
        concepts = (sub or {}).get("concepts") or {}
        children = {}
        for cid, cobj in concepts.items():
            p = (cobj or {}).get("parent_concept_id")
            if p:
                children.setdefault(p, []).append(cid)
        roots = [
            cid for cid, cobj in concepts.items()
            if not (cobj or {}).get("parent_concept_id")
            or (cobj or {}).get("parent_concept_id") not in concepts
        ]

        def _line(cid, indent=2):
            cobj = concepts.get(cid) or {}
            try:
                dims = [
                    float(cobj.get(d) or 0)
                    for d in ("knowledge", "retrieval", "application", "explanation", "transfer")
                ]
                avg = sum(dims) / 5.0
            except Exception:
                avg = 0.0
            open_mc = sum(
                1
                for m in (cobj or {}).get("misconceptions") or []
                if isinstance(m, dict) and m.get("status") == "open"
            )
            mc = f", {open_mc} open MC" if open_mc else ""
            label = cobj.get("title") or cid
            print(" " * indent + f"├─ {label}  avg={avg:.2f}{mc}")
            for ch in children.get(cid) or []:
                _line(ch, indent + 3)

        seen = set()
        for r in roots:
            if r in seen:
                continue
            _line(r)
            # mark subtree
            stack = [r]
            while stack:
                n = stack.pop()
                if n in seen:
                    continue
                seen.add(n)
                stack.extend(children.get(n) or [])
        for cid in concepts:
            if cid not in seen:
                _line(cid)
    print(c("─" * 60, GRAY))
    print()


def print_help():

    print()
    print(c("COMMANDS", BOLD + CYAN))
    print()
    print("  /state              Show learner summary")
    print("  /state full         Write full JSON to learner_state_full.json + summary")
    print("  /state dump [path]  Same (optional filename)")
    print("  /subjects           List all subjects")
    print("  /tree               Show concept parent/child tree")
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

        if user_input.lower() in ("/debug tokens", "/debug", "/metrics"):
            try:
                from metrics_lib import format_breakdown, get_last_breakdown, print_summary
                policy = suggest_next_activity(state)
                build_messages(base_prompt, state, conversation, policy=policy)
                bd = get_last_breakdown()
                print()
                if bd:
                    print(format_breakdown(bd.get("components") or [], bd.get("total_tokens_est")))
                    print(f"system_hash={bd.get('system_hash')} mode={bd.get('mode')}")
                else:
                    print("No breakdown available.")
                print()
                print_summary(10)
            except Exception as e:
                print(c(f"Metrics error: {e}", YELLOW))
            continue

        if user_input.lower() == "/state":

            print_state_summary(
                state
            )

            continue

        if user_input.lower() == "/state full" or user_input.lower().startswith("/state dump"):
            # Write full JSON to file (CLI paste is too large); print short summary only
            dump_path = Path("learner_state_full.json")
            parts = user_input.split(None, 2)
            if len(parts) >= 3 and parts[1].lower() in ("full", "dump"):
                # /state dump myfile.json
                dump_path = Path(parts[2].strip() or "learner_state_full.json")
            elif len(parts) == 2 and parts[1].lower() == "dump":
                dump_path = Path("learner_state_full.json")
            try:
                dump_path.write_text(
                    json.dumps(state, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                print(c(f"Full state written to: {dump_path.resolve()}", GREEN))
                print("Use:  cat learner_state_full.json | less")
                print("Or copy the file for analysis. Summary below:")
            except Exception as e:
                print(c(f"Could not write dump: {e}", RED))
            print_state_summary(state)
            continue

        if user_input.lower() == "/subjects":
            print_subjects(state)
            continue

        if user_input.lower() in ("/tree", "/concepts"):
            print_concept_tree(state)
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
                if state.get("active_session", {}).get("active"):
                    state["active_session"]["just_resumed"] = False
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

        if cmd_lower in ("/tree", "/concepts"):
            print_concept_tree(state)
            continue

        if user_input.startswith("/") and not cmd_lower.startswith(
            ("/new ", "/switch ", "/state", "/subjects", "/subject",
             "/tree", "/concepts",
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
            # Learner signals "we already did this" → force advance next policy
            low = user_input.lower()
            already = any(
                p in low
                for p in (
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
                )
            )
            if already:
                state["active_session"]["must_advance"] = True
                state["active_session"]["repeat_streak"] = int(
                    state["active_session"].get("repeat_streak") or 0
                ) + 1
            # Learner rejects a thread / shifts goal → drop stale resume + resume force
            goal_shift = any(
                p in low
                for p in (
                    "niet belangrijk",
                    "wil geen",
                    "wil niet",
                    "ik wil begrijpen",
                    "ik wil meer",
                    "liever over",
                    "overstappen",
                    "iets anders",
                )
            )
            if goal_shift:
                state["active_session"]["just_resumed"] = False
                state["active_session"]["resume_instruction"] = None
                state["active_session"]["must_advance"] = True

            # Stuck signals: "geen idee", question unclear, meta "what did you mean"
            stuck = any(
                p in low
                for p in (
                    "geen idee",
                    "weet ik niet",
                    "dat weet ik niet",
                    "geen flauw",
                    "snap je vraag niet",
                    "snap de vraag niet",
                    "vraag is onduidelijk",
                    "onduidelijk",
                    "wat bedoel je",
                    "wat beodel",
                    "welk fenomeen",
                    "welke vergelijking",
                    "geef eens een hint",
                    "geef een hint",
                    "hint",
                    "leg uit",
                    "leg het me uit",
                    "leg me uit",
                    "vertel het",
                    "zeg het gewoon",
                    "wat wil je",
                    "wat is de formule",
                    "wat is de formula",
                    "wat is die formule",
                    "formule voor",
                    "foirumel",
                    "geef de formule",
                    "schrijf de formule",
                )
            )
            explicit_teach = any(
                p in low
                for p in (
                    "leg uit",
                    "leg het me uit",
                    "leg me uit",
                    "vertel het",
                    "zeg het gewoon",
                    "gewoon uitleggen",
                    "wat is de formule",
                    "wat is de formula",
                    "wat is die formule",
                    "formule voor",
                    "foirumel",
                    "geef de formule",
                    "schrijf de formule",
                    "wat betekent",
                    "definieer",
                    "niet zo snel",
                    "te snel",
                    "langzamer",
                    "meer diepgang",
                    "toelichten",
                    "vanwaar komen",
                    "wat is die k",
                    "wat is kappa",
                    "wat zijn die formules",
                    "wat zijn die symbolen",
                    "abstract",
                    "kan me er niks bij",
                    "go slow",
                    "slow down",
                    "nieuwe concepten",
                    "new concepts",
                    "wtf",
                    "was dat allemaal",
                    "te veel",
                    "too much",
                    "too fast",
                    "too many terms",
                    "te veel termen",
                )
            )
            if stuck:
                hc = int(state["active_session"].get("hint_count") or 0) + 1
                state["active_session"]["hint_count"] = hc
                state["active_session"]["stuck_signal"] = True
                state["active_session"]["force_scaffold"] = True
                if explicit_teach or hc >= 2:
                    # Hard teach: full answer this turn, then close thread
                    state["active_session"]["force_full_teach"] = True
                    state["active_session"]["must_advance"] = True
            else:
                state["active_session"]["stuck_signal"] = False
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
            # Still leave resume mode so policy does not force RETRIEVAL forever
            if state.get("active_session", {}).get("active"):
                state["active_session"]["just_resumed"] = False
                save_state(state)
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
        # Detect similar question → ban it and force advance next turn.
        # If not yet streamed, one silent retry; if already streamed, ban only
        # (avoids double TUTOR block on screen).
        try:
            new_q = None
            if isinstance(session_info, dict):
                new_q = (
                    session_info.get("last_question")
                    or session_info.get("last_tutor_question")
                    or session_info.get("resume_instruction")
                )
            if not new_q:
                new_q = extract_question_from_text(answer)
            active_chk = state.setdefault("active_session", {})
            ban_list = list(active_chk.get("banned_questions") or [])
            recent_list = list(active_chk.get("recent_questions") or [])
            conflict = False
            for prev in ban_list + recent_list[-3:]:
                if questions_similar(new_q, prev, threshold=0.50):
                    conflict = True
                    break
            if conflict:
                if new_q and new_q not in ban_list:
                    ban_list.append(new_q)
                active_chk["banned_questions"] = ban_list[-8:]
                active_chk["must_advance"] = True
                active_chk["repeat_streak"] = int(active_chk.get("repeat_streak") or 0) + 1
                already_streamed = bool(getattr(ask_model, "did_stream_response", False))
                if already_streamed:
                    print(c("  [anti-repeat] similar Q banned for next turns", YELLOW))
                elif not active_chk.get("_anti_repeat_retried"):
                    active_chk["_anti_repeat_retried"] = True
                    print(c("  [anti-repeat] similar question — silent retry", YELLOW))
                    policy2 = suggest_next_activity(state)
                    messages2 = build_messages(
                        base_prompt, state, conversation, policy=policy2
                    )
                    raw2, parsed2 = ask_model_with_json_retry(messages2)
                    if isinstance(parsed2, dict) and parsed2.get("response"):
                        answer = str(parsed2.get("response") or answer)
                        if parsed2.get("session"):
                            session_info = parsed2["session"]
                        if parsed2.get("assessment"):
                            assessment = parsed2["assessment"]
                active_chk["_anti_repeat_retried"] = False
        except Exception as _ar_err:
            print(c(f"  [anti-repeat] skip: {_ar_err}", YELLOW))

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