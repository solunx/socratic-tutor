"""
Deterministic teaching policy: next activity from learner state.

Pure Python rules (no LLM). tutor.suggest_next_activity re-exports this.
"""
from __future__ import annotations

from datetime import datetime

DIMENSIONS = [
    "knowledge",
    "retrieval",
    "application",
    "explanation",
    "transfer"
]

WEAK_DIM_THRESHOLD = 0.45

DIM_TO_ACTIVITY = {
    "knowledge": "EXPLANATION",
    "retrieval": "RETRIEVAL",
    "application": "APPLICATION",
    "explanation": "EXPLANATION",
    "transfer": "TRANSFER",
}


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
    # Late import avoids circular dependency (tutor imports policy after helpers exist)
    import tutor as T

    get_focus_concept = T.get_focus_concept
    get_open_thread = T.get_open_thread
    open_misconceptions = T.open_misconceptions
    average_mastery = T.average_mastery
    clamp = T.clamp
    has_open_misconception = T.has_open_misconception
    find_weak_prerequisite = T.find_weak_prerequisite
    find_catalog_entry = getattr(T, "find_catalog_entry", lambda *a, **k: None)

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


    # 0-diagnostic) New subject prior-knowledge intake (before teaching)
    sid_act = active.get("subject_id")
    if sid_act and T.subject_needs_diagnostic(state, sid_act):
        # Goal must be collected first
        if not active.get("pending_goal"):
            d = T.get_subject_diagnostic(state, sid_act)
            if d.get("status") != "done":
                if d.get("status") == "needed":
                    T.begin_subject_diagnostic(state, sid_act)
                return {
                    "activity": "DIAGNOSTIC",
                    "concept_id": cid,
                    "subject_id": sid_act,
                    "reason": (
                        "PRIOR-KNOWLEDGE INTAKE: ask 1 short diagnostic question "
                        f"(turn {d.get('turns', 0)+1}/{d.get('max_turns', 3)}). "
                        "No teaching yet. Probe what they already know/do; "
                        "vary angle (definition / example / mechanism). "
                        "Set assessment.activity=DIAGNOSTIC; after enough signal set "
                        "level_estimate=novice|emerging|solid and diagnostic_complete=true."
                    ),
                    "force": True,
                    "diagnostic": True,
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
        if tries >= 4:
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
                "learner stuck — do NOT give the full answer. "
                "Give ONE small nudge (L1–L2): a concrete example, contrast, or "
                "partial step from THEIR words. Then ONE simpler question. "
                "Productive struggle > comfort."
                + (f" | {focus_q[:70]}" if focus_q else "")
            ),
            "force": True,
            "scaffold": True,
            "open_thread_q": focus_q,
        }

    # 0b) Open thread: after 3 tries → FULL TEACH and CLOSE (stop the loop)
    ot = get_open_thread(active)
    if ot and int(ot.get("tries") or 0) >= 4 and ot.get("status") != "closed":
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


