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



# ----------------------------------------------------------------------------------
# Focus rotation + reframe (patch 2). Generic: no subject words, no language.
# ----------------------------------------------------------------------------------
ROTATE_AFTER = 4          # consecutive clean-correct answers on one concept before we move on
_DIMS = ("knowledge", "retrieval", "application", "explanation", "transfer")


def _avg_mastery(c):
    vals = [float((c or {}).get(d) or 0.0) for d in _DIMS]
    return sum(vals) / len(vals)


def _rotation_candidates(state, sid, cid):
    """Other concepts of the subject, siblings first, weakest/least-recent first."""
    concepts = ((state.get("subjects") or {}).get(sid) or {}).get("concepts") or {}
    parent = (concepts.get(cid) or {}).get("parent_concept_id")
    cands = [(k, c) for k, c in concepts.items() if k != cid]
    pool = [kc for kc in cands if kc[1].get("parent_concept_id") == parent] or cands
    return sorted(pool, key=lambda kc: (_avg_mastery(kc[1]), kc[1].get("last_practiced") or ""))


def _reframe_policy(active, sid, cid, why):
    return {
        "activity": active.get("current_activity") or "EXPLANATION",
        "concept_id": cid, "subject_id": sid, "force": True,
        "move": "reframe", "reframe": True,
        "reason": ("REFRAME: " + why + " Do NOT ask another quiz question; summarise what the learner "
                   "showed and let them choose the next direction."),
    }


def _suggest_next_activity_core(state):
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
    mark_thread_scaffolded = T.mark_thread_scaffolded
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

    # ---- same-turn intent overrides ------------------------------------------------
    # Contract: handle_turn writes active["turn_intent"] / ["turn_correctness"] for THIS
    # learner message (tiny classifier call) before calling us. They beat flags left over
    # from the previous turn (the one-turn lag seen in the vriend1 event log).
    _judge = active.get("judge") if isinstance(active.get("judge"), dict) else None
    _jv = str((_judge or {}).get("verdict") or "").lower()
    if _jv in ("correct", "partial", "incorrect", "uncertain", "off_target"):
        # impartial judge > the tutor's own (key-biased) verdict and > flags left over from the previous turn
        active["turn_correctness"] = {"correct": "correct", "partial": "partial",
                                      "incorrect": "incorrect", "uncertain": "unknown", "off_target": "partial"}[_jv]
        if _jv == "correct":
            active.update(last_verdict="correct", last_learner_signal="correct", partial_missing=False,
                          stuck_signal=False, force_scaffold=False, confused_by_tutor=False)
        elif _jv == "partial":
            active.update(last_verdict="partial", partial_missing=True)
        elif _jv == "incorrect":
            active.update(last_verdict="incorrect")
        elif _jv == "off_target":     # true but not an answer: NOT a missing piece, NOT a failure
            active.update(last_verdict="partial", partial_missing=False, stuck_signal=False)
    intent = str(active.get("turn_intent") or "").strip().lower()
    corr = str(active.get("turn_correctness") or "").strip().lower()
    if intent:
        _ot = get_open_thread(active)
        _mid_ladder = bool(_ot and _ot.get("status") in ("hinted", "scaffolded", "open"))
        if intent == "asks_hint":
            active.update(
                must_advance=False,
                repeat_streak=0,
                force_scaffold=True,
                stuck_signal=True,
                confused_by_tutor=False,
            )
        elif intent == "asks_reveal":
            active.update(
                force_full_teach=True,
                stuck_signal=True,
                must_advance=False,
                confused_by_tutor=False,
            )
        elif intent == "frustration":
            active.update(must_advance=False, confused_by_tutor=False, reframe=True)
        elif intent == "challenges_tutor":
            if _jv == "incorrect":      # the learner's objection does not hold: explain, do not concede
                active.update(learner_correction=False, force_scaffold=False, must_advance=False,
                              confused_by_tutor=False)
            else:
                active.update(learner_correction=True, force_scaffold=True, must_advance=False,
                              confused_by_tutor=False)
        elif intent == "answer":
            # Learner is answering, not asking: drop help/confusion/correction leftovers
            active.update(confused_by_tutor=False, learner_correction=False)
            if active.get("last_learner_signal") in ("confused_by_tutor", "challenges_tutor"):
                active["last_learner_signal"] = (
                    corr if corr in ("correct", "partial") else "partial"
                )
            if corr == "correct":
                active.update(
                    stuck_signal=False,
                    force_scaffold=False,
                    last_verdict="correct",
                )
            elif corr == "incorrect":
                active.update(
                    must_advance=False,
                    last_verdict="incorrect",
                    last_learner_signal="stuck",
                    force_scaffold=True,
                    stuck_signal=True,
                )
            # Never run the loop-breaker while the learner is working an open hint ladder
            if _mid_ladder:
                active["same_activity_streak"] = 0
                active["repeat_streak"] = 0

    # ---- reframe (frustration) and focus rotation ---------------------------------
    if active.pop("reframe", False):
        return _reframe_policy(active, sid, cid, "the learner is frustrated (repetition, level or pace).")
    if int(active.get("correct_streak") or 0) >= ROTATE_AFTER and intent in ("", "answer", "insight"):
        active["correct_streak"] = 0
        cands = _rotation_candidates(state, sid, cid)
        if not cands:
            return _reframe_policy(active, sid, cid, "this concept is demonstrated and there is no other concept to move to.")
        nid, nc = cands[0]
        for t in active.get("open_threads") or []:
            if isinstance(t, dict) and t.get("status") in ("open", "scaffolded", "hinted"):
                active["parked_thread_q"] = t.get("q")
                t["status"] = "parked"
        active.update(current_concept_id=nid, must_advance=False, stuck_signal=False, force_scaffold=False,
                      rotation_pending=True)
        cur_title = (concept or {}).get("title") or cid
        return {
            "activity": "EXPLANATION", "concept_id": nid, "subject_id": sid, "force": True,
            "move": "rotate", "next_concept_id": nid,
            "reason": (
                f"ROTATE FOCUS: the learner has shown '{cur_title}' {ROTATE_AFTER}+ times in a row; more of the "
                f"same question type would be repetition. Switch to '{nc.get('title') or nid}' "
                f"(mastery {_avg_mastery(nc):.2f}). FIRST give clear feedback on their LAST answer in one sentence (right / what is right / not right and why); then bridge in ONE sentence; then ask "
                "ONE open question about the new focus. Do not re-ask earlier question types."
            ),
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

    def _gap_epistemic_note(ot):
        """Behaviour depends on gap_card.status — plan ≠ verified truth."""
        if not ot or not isinstance(ot.get("gap_card"), dict):
            return (
                " No verified gap_card: do not declare the learner factually wrong; "
                "ask how they arrived at their answer; prefer questions over hard claims."
            )
        gc = ot["gap_card"]
        st = str(gc.get("status") or "model_only")
        note = f" gap_card.status={st}."
        if st in ("model_only", "disputed"):
            note += (
                " Do NOT hard-reject the learner on factual grounds from your key alone; "
                "ask for their reasoning. Prefer questions; if unsure say you are unsure."
            )
        if gc.get("target"):
            note += f" target={str(gc['target'])[:100]}."
        if gc.get("retracted_claims"):
            note += " Never restate retracted_claims: " + "; ".join(
                str(x)[:80] for x in (gc.get("retracted_claims") or [])[-3:]
            )
        ladder = gc.get("scaffold_ladder") or []
        if ladder and st != "source_backed":
            note += f" Prefer next ladder step: {str(ladder[0])[:100]}."
        return note

    # 0a000) Learner insight / sharp side-question → follow THEIR thread
    if active.get("learner_insight"):
        active["learner_insight"] = False
        active["force_scaffold"] = False
        active["stuck_signal"] = False
        parked = active.get("parked_thread_q")
        return {
            "activity": "EXPLANATION",
            "concept_id": cid,
            "subject_id": sid,
            "reason": (
                "RECOGNISE: the learner formed an insight or asked a sharp side "
                "question. Confirm what they got right in one short clause. "
                "Follow THEIR thread this turn — do NOT force the previous quiz. "
                + (
                    f"(Parked earlier gap for later: {str(parked)[:80]}.) "
                    if parked
                    else ""
                )
                + "End with ONE question that deepens their insight "
                "(apply/contrast/predict), not a restate of an old gap."
            ),
            "force": True,
            "probe_type": "apply",
            "learner_signal": "insight",
        }

    # 0a00) Learner: "you already asked that / I just said that"
    # Pedagogical recovery — not stuck scaffold, not factual dispute.
    if active.get("repetition_feedback"):
        active["repetition_feedback"] = False
        active["force_scaffold"] = False
        active["stuck_signal"] = False
        active["must_advance"] = False  # consume
        ot = get_open_thread(active)
        prefer = active.pop("prefer_probe_type", None) or "apply"
        if ot:
            ot["last_probe_type"] = prefer
            ot["progress"] = ot.get("progress") or "demonstrated"
            if isinstance(ot.get("gap_card"), dict):
                ot["gap_card"]["last_probe_type"] = prefer
                ot["gap_card"]["progress"] = ot["progress"]
        nxt = "APPLICATION"
        if prefer == "contrast":
            nxt = "TRANSFER"
        elif prefer == "predict":
            nxt = "TRANSFER"
        return {
            "activity": nxt,
            "concept_id": cid,
            "subject_id": sid,
            "reason": (
                "REPETITION FEEDBACK: the learner already answered this. "
                "Briefly acknowledge the loop (one short clause). "
                f"FORBIDDEN: restate/re-ask the same knowledge check. "
                f"Required probe_type={prefer}: use apply, contrast, or predict "
                "in a NEW situation. Learning progress may advance even if "
                "gap_card.status is still model_only (pedagogy ≠ verified truth)."
            ),
            "force": True,
            "probe_type": prefer,
            "repetition_feedback": True,
            "open_thread_q": (ot or {}).get("q") if ot else None,
        }

    # 0a0) Learner corrected the tutor on a fact — never double-down on error
    if active.get("learner_correction"):
        active["learner_correction"] = False
        active["force_scaffold"] = False
        ot = get_open_thread(active)
        focus_q = (ot or {}).get("q") if ot else None
        if ot and isinstance(ot.get("gap_card"), dict):
            ot["gap_card"]["status"] = "disputed"
        return {
            "activity": "EXPLANATION",
            "concept_id": cid,
            "subject_id": sid,
            "reason": (
                "LEARNER CORRECTION / challenges_tutor: they dispute a tutor claim. "
                "If they are right, concede clearly and do not re-assert the wrong claim. "
                "If unsure, say so. Then continue only on the unresolved gap — "
                "simplify it; do not switch topic. No invented comparative facts."
                + (f" | UNRESOLVED: {focus_q[:120]}" if focus_q else "")
                + _gap_epistemic_note(ot)
            ),
            "force": True,
            "scaffold": True,
            "open_thread_q": focus_q,
            "learner_correction": True,
            "learner_signal": "challenges_tutor",
        }

    # 0a0b) Learner found the tutor confusing — repair the question, same gap
    if active.get("confused_by_tutor"):
        active["confused_by_tutor"] = False
        active["force_scaffold"] = False
        ot = get_open_thread(active)
        focus_q = (ot or {}).get("q") if ot else None
        return {
            "activity": "EXPLANATION",
            "concept_id": cid,
            "subject_id": sid,
            "reason": (
                "confused_by_tutor: acknowledge the confusion briefly, restate the "
                "SAME unresolved gap in simpler words (one unknown only). "
                "Do not open a new topic or a conflicting particle/scenario."
                + (f" | UNRESOLVED: {focus_q[:120]}" if focus_q else "")
                + _gap_epistemic_note(ot)
            ),
            "force": True,
            "scaffold": True,
            "open_thread_q": focus_q,
            "learner_signal": "confused_by_tutor",
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
                "REVEAL (full teach / slow): ≤3 short sentences answering the open "
                "point (plain words; max ONE new term). Then ONE apply/contrast/"
                "predict question on a NEW concrete case. "
                "FORBIDDEN: 'explain in one sentence why [what you just said]'. "
                "Close the open gap after the reveal."
                + (f" | thread: {focus_q[:70]}" if focus_q else "")
            ),
            "force": True,
            "scaffold": True,
            "full_teach": True,
            "close_thread": True,
            "probe_type": "apply",
            "open_thread_q": focus_q,
        }

    # 0a2) Soft stuck → scaffold once (not endless re-ask)
    if active.get("force_scaffold") or active.get("stuck_signal"):
        active["force_scaffold"] = False
        active["stuck_signal"] = False
        active["_was_stuck_this_turn"] = True  # consumed in update_session_memory
        mark_thread_scaffolded(active)
        ot = get_open_thread(active)
        focus_q = (ot or {}).get("q") if ot else None
        tries = int((ot or {}).get("tries") or 0)
        # Escalate only after several scaffolds (productive struggle first)
        if tries >= 5:
            if ot:
                ot["status"] = "closed"
                ot["closed_reason"] = "full_teach_after_tries"
            return {
                "activity": "EXPLANATION",
                "concept_id": cid,
                "subject_id": sid,
                "reason": (
                    "REVEAL after failed scaffolds — ≤3 sentences using the "
                    "learner's partial ideas, close thread, then ONE apply/"
                    "contrast on a NEW case (FORBIDDEN: restate the reveal)"
                ),
                "force": True,
                "full_teach": True,
                "close_thread": True,
                "probe_type": "apply",
                "open_thread_q": focus_q,
            }
        ladder_hint = ""
        if ot and isinstance(ot.get("gap_card"), dict):
            ladder = list(ot["gap_card"].get("scaffold_ladder") or [])
            if ladder:
                step_i = min(max(tries - 1, 0), len(ladder) - 1)
                ladder_hint = f" Ladder step: {str(ladder[step_i])[:120]}."
        return {
            "activity": "EXPLANATION",
            "concept_id": cid,
            "subject_id": sid,
            "reason": (
                "ELICIT (stuck): the LEARNER must construct the next idea. "
                "Start from their last words. Ask ONE hinge question about the "
                "same unknown"
                + (f" «{(focus_q or '')[:120]}»" if focus_q else "")
                + ". At most ONE short clue that does NOT contain the answer. "
                "FORBIDDEN: lecture the mechanism then ask them to restate it. "
                "FORBIDDEN: 'in one sentence explain why…' about a fact you just gave. "
                "If tries are high and no progress, escalate to a short REVEAL "
                "(≤3 sentences) + apply on a NEW case — never paraphrase-quiz."
                + ladder_hint
                + _gap_epistemic_note(ot)
            ),
            "force": True,
            "scaffold": True,
            "open_thread_q": focus_q,
            "learner_signal": "stuck",
            "probe_type": "missing_piece",
        }

    # 0b) Open thread: after 3 tries → FULL TEACH and CLOSE (stop the loop)
    ot = get_open_thread(active)
    if ot and int(ot.get("tries") or 0) >= 5 and ot.get("status") != "closed":
        focus_q = ot.get("q")
        ot["status"] = "closed"
        ot["closed_reason"] = "max_tries_full_teach"
        return {
            "activity": "EXPLANATION",
            "concept_id": cid,
            "subject_id": sid,
            "reason": (
                f"REVEAL — open thread tries={ot.get('tries')}: ≤3 sentences that "
                f"answer the gap (use learner's words), close gap, then ONE "
                f"apply/contrast/predict on a NEW case — never restate-quiz. "
                f"{(focus_q or '')[:80]}"
            ),
            "force": True,
            "full_teach": True,
            "close_thread": True,
            "probe_type": "apply",
            "open_thread_q": focus_q,
        }
    # Legacy: must_advance + open tries used to force the *same* gap question.
    # That caused echo loops. Fall through to probe-type advance below instead.

    # 0b) After a correct (or solid partial) answer: advance activity + probe type.
    # Domain-agnostic: never re-test the same knowledge with the same probe shape.
    verdict = str(active.get("last_verdict") or "").lower()
    ot_prog = get_open_thread(active)
    progress = (ot_prog or {}).get("progress") or ""
    prefer = active.pop("prefer_probe_type", None)
    if not prefer and ot_prog:
        prefer = ot_prog.get("last_probe_type")
        prefer = T.next_probe_type(prefer) if progress in ("partial_ok", "demonstrated") else prefer
    solid_partial = (
        verdict == "partial"
        and (
            int(active.get("same_activity_streak") or 0) >= 1
            or progress in ("partial_ok", "demonstrated")
            or active.get("partial_missing")
        )
    )
    if verdict in ("correct", "ok") or solid_partial or active.get("must_advance"):
        last_act = (active.get("current_activity") or active.get("prev_activity") or "").upper()
        if last_act in ("EXPLANATION", "RETRIEVAL", "MISCONCEPTION_CHECK", "", "APPLICATION"):
            nxt = "APPLICATION"
            probe = prefer or "apply"
            if probe in ("contrast", "predict"):
                nxt = "TRANSFER"
            elif probe == "missing_piece":
                nxt = "EXPLANATION"
            if concept and probe != "missing_piece":
                try:
                    app = float(concept.get("application") or 0)
                    tr = float(concept.get("transfer") or 0)
                    if app >= 0.45 and tr < 0.45:
                        nxt = "TRANSFER"
                        probe = probe if probe in ("contrast", "predict") else "contrast"
                    elif app >= 0.45 and tr >= 0.45 and verdict in ("correct", "ok"):
                        # Next step — avoid contradictory "RETRIEVAL + apply"
                        nxt = "APPLICATION"
                        probe = "predict" if probe == "apply" else probe
                except Exception:
                    pass
            active["last_verdict"] = None
            active["must_advance"] = False
            active["partial_missing"] = False
            if ot_prog:
                ot_prog["last_probe_type"] = probe
                if isinstance(ot_prog.get("gap_card"), dict):
                    ot_prog["gap_card"]["last_probe_type"] = probe
            probe_rules = {
                "restate": "FORBIDDEN now — learner already showed this; do not ask them to restate the key.",
                "missing_piece": "Ask ONLY for the single missing hinge; do not re-ask the whole gap.",
                "apply": "NEW situation: learner must USE the idea (what changes / what happens if…).",
                "contrast": "Compare two cases; one difference must carry the insight.",
                "predict": "Ask for a prediction before revealing the outcome.",
            }
            return {
                "activity": nxt,
                "concept_id": cid,
                "subject_id": sid,
                "reason": (
                    f"verdict={verdict or 'advance'} progress={progress or 'n/a'} "
                    f"→ {nxt} with probe_type={probe}. "
                    f"{probe_rules.get(probe, '')} "
                    "FORBIDDEN: pure restate of the tutor's previous key sentence. "
                    "Pedagogy may advance while gap_card.status stays model_only."
                ),
                "force": True,
                "probe_type": probe,
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




_JUDGE_HEAD = "JUDGE (impartial, for THIS turn; do not contradict it): "
_JUDGE_TAIL = " Open with approval ('right', 'good') ONLY when the verdict is CORRECT; never use 'you are right' to mean 'I made a mistake' (write 'my mistake')."


def judge_directive(j):
    """Verdict-specific feedback instruction. Python decides the verdict class; the model only words it."""
    v = str((j or {}).get("verdict") or "").lower()
    why = str((j or {}).get("reason") or "").strip()
    miss = (j or {}).get("missing")
    if v == "correct":
        return (_JUDGE_HEAD + f"the learner's answer is CORRECT ({why}). FIRST sentence: say clearly that it is right and "
                "why, in one clause. Do not add a 'but' that undoes it, and do not ask for a 'missing piece'; move on."
                + _JUDGE_TAIL)
    if v == "partial":
        return (_JUDGE_HEAD + f"PARTIAL ({why}). Missing: {miss or 'see reason'}. FIRST sentence: say what is right. Then ask "
                "ONLY for the missing piece WITHOUT stating it." + _JUDGE_TAIL)
    if v == "incorrect":
        return (_JUDGE_HEAD + f"the answer does NOT hold under the premises ({why}). FIRST sentence: say plainly that it does "
                "not hold and give the reason in one clause; then ask ONE counter-case question; do not lecture." + _JUDGE_TAIL)
    if v == "uncertain":
        return (_JUDGE_HEAD + f"UNCERTAIN ({why}). Do not call the answer right or wrong. Ask how they arrived at it."
                + _JUDGE_TAIL)
    if v == "off_target":
        return (_JUDGE_HEAD + f"the learner's statement is TRUE ({why}) but it does not answer your question. FIRST sentence: "
                "confirm it specifically (say what exactly they got right). Then connect it to your question in ONE step: ask a "
                "question that builds on THEIR statement. Do NOT repeat or rephrase your previous question; it is not a missing "
                "piece and not a mistake." + _JUDGE_TAIL)
    return ""


def thread_directive(streak):
    if int(streak or 0) < 2:
        return ""
    return (f"LEARNER THREAD: the learner has answered something other than your question {int(streak)} times in a row, each "
            "time with a valid point of their own. DROP your current line of questions for good (no rephrase, no 'missing piece'). "
            "FIRST confirm what they just said (say specifically what is right). Then ask ONE question that follows THEIR thread: "
            "build on the point they just made. Do NOT offer to end, summarise or wrap up the session, and do NOT offer a menu of "
            "choices, unless the learner asked for that.")


def suggest_next_activity(state):
    p = _suggest_next_activity_core(state)
    if not isinstance(p, dict):
        return p
    p = dict(p)
    _act5 = (state or {}).get("active_session") or {}
    j = _act5.get("judge")
    d = judge_directive(j) if isinstance(j, dict) else ""
    t = thread_directive(_act5.get("off_target_streak"))
    if (d or t) and p.get("reason") is not None:
        if isinstance(j, dict):
            p["judge"] = dict(j)
        p["off_target_streak"] = int(_act5.get("off_target_streak") or 0)
        ds = " ".join(x for x in (d, t) if x)
        if t or (isinstance(j, dict) and j.get("verdict") == "off_target"):
            # these directives REPLACE the base probe instruction (which would ask the same question again)
            p["reason"] = f"{ds} | BASE POLICY (secondary; ignore any instruction in it to re-ask the same question): {p['reason']}"
        else:
            p["reason"] = f"{p['reason']} | {ds}"
    try:
        from knowledge.focus import focus_policy_line
        fl = focus_policy_line(state)
        if fl and p.get("reason") is not None:
            p["reason"] = f"{p['reason']} | {fl}"
            p["focus_mode"] = ((state or {}).get("active_session") or {}).get("focus_mode")
    except Exception:
        pass
    return p
