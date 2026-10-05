#!/usr/bin/env python3
"""
Channel-agnostic tutor turn handler.

CLI, web gateway, and future Telegram adapters all call handle_turn().
No terminal printing. State is mutated and returned; caller saves if needed.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

import tutor as T


def subjects_payload(state: dict) -> list[dict]:
    out = []
    subjects = state.get("subjects") or {}
    for sid, sub in sorted(subjects.items(), key=lambda x: str(x[1].get("title", x[0])).lower()):
        if not isinstance(sub, dict):
            continue
        concepts = sub.get("concepts") or {}
        n_open = 0
        for cpt in concepts.values():
            if not isinstance(cpt, dict):
                continue
            for m in cpt.get("misconceptions") or []:
                if isinstance(m, dict) and m.get("status", "open") != "resolved":
                    n_open += 1
        item = {
            "id": sid,
            "title": sub.get("title", sid),
            "goal": sub.get("goal"),
            "n_concepts": len(concepts),
            "open_mc": n_open,
            "status": T.subject_status_line(state, sid),
            "parent_id": sub.get("parent_id"),
            "related": list(sub.get("related") or []),
        }
        out.append(item)
    return out


def resume_payload(state: dict, subject_id: str) -> dict:
    subject = (state.get("subjects") or {}).get(subject_id) or {}
    resume = T.get_last_resume_for_subject(state, subject_id)
    if not T.is_learner_facing_resume(resume):
        resume = T.make_fallback_resume(state, subject_id)
    concepts = T.get_recent_concepts(state, subject_id, limit=3)
    return {
        "subject_id": subject_id,
        "title": subject.get("title", subject_id),
        "goal": subject.get("goal"),
        "recent_concepts": [{"id": cid, "title": title} for cid, title in concepts],
        "resume_question": resume,
    }


def state_summary(state: dict) -> dict:
    active = state.get("active_session") or {}
    T.rebuild_review_queue(state)
    return {
        "version": state.get("version"),
        "learner_id": (state.get("learner") or {}).get("id"),
        "subjects": subjects_payload(state),
        "active_session": {
            "active": bool(active.get("active")),
            "subject_id": active.get("subject_id"),
            "concept_id": active.get("current_concept_id"),
            "activity": active.get("current_activity"),
            "question_number": active.get("question_number"),
            "session_thread": active.get("session_thread"),
            "last_tutor_question": active.get("last_tutor_question"),
            "just_resumed": bool(active.get("just_resumed")),
        },
        "due_reviews": len(state.get("review_queue") or []),
        "pending_catalog": len((state.get("meta") or {}).get("pending_catalog") or []),
    }


def _handle_command(state: dict, conversation: list, cmd: str) -> dict | None:
    """Return a response dict for slash commands, or None if not a command."""
    low = cmd.lower().strip()

    if low in ("/help", "help"):
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "help",
            "response": (
                "Commands: /subjects, /state, /review, /new <topic>, /reset, /help\n"
                "Or natural language: “ik wil verder met agents”, “welke onderwerpen?”"
            ),
            "subjects": subjects_payload(state),
        }

    if low in ("/subjects", "/subject"):
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "subjects",
            "response": "",
            "subjects": subjects_payload(state),
        }

    if low == "/state":
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "state",
            "response": "Learner state summary.",
            "summary": state_summary(state),
        }

    if low == "/review":
        T.rebuild_review_queue(state)
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "reviews",
            "response": "",
            "reviews": list(state.get("review_queue") or []),
        }

    if low == "/reset":
        conversation.clear()
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "reset",
            "response": "Conversation context reset. Learner state kept.",
        }

    if low.startswith("/new "):
        title = cmd[5:].strip()
        if not title or len(title) > 60 or title.count(" ") > 5:
            return {
                "ok": False,
                "agent": "tutor",
                "kind": "error",
                "response": "Use /new <short topic> (max ~4 words).",
            }
        active = state["active_session"]
        if active.get("active"):
            T.close_session(state, active.get("resume_instruction"))
        subject_id = T.ensure_subject(state, title, title)
        T.start_session(state, subject_id, pending_goal=True)
        conversation.clear()
        T.save_state(state)
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "new_subject",
            "response": f"New subject: {title}. What do you want to achieve with this topic?",
            "subject_id": subject_id,
            "subjects": subjects_payload(state),
        }

    return None


def _quiet_switch(state: dict, conversation: list, subject_id: str) -> dict:
    active = state["active_session"]
    if active.get("active") and active.get("subject_id") != subject_id:
        T.close_session(state, active.get("resume_instruction"))
    T.start_session(state, subject_id, is_resume=True)
    conversation.clear()
    banner = resume_payload(state, subject_id)
    # Store resume question for anti-repeat
    resume_q = banner.get("resume_question")
    if resume_q:
        active = state["active_session"]
        active["last_tutor_question"] = str(resume_q).strip()[:300]
        active["resume_instruction"] = str(resume_q).strip()
        recent = list(active.get("recent_questions") or [])
        if not recent or recent[-1] != active["last_tutor_question"]:
            recent.append(active["last_tutor_question"])
        active["recent_questions"] = recent[-5:]
    T.save_state(state)
    return {
        "ok": True,
        "agent": "tutor",
        "kind": "resume",
        "response": resume_q or "",
        "resume": banner,
        "subjects": subjects_payload(state),
        "summary": state_summary(state),
    }


def handle_turn(
    message: str,
    state: dict | None = None,
    conversation: list | None = None,
    *,
    agent: str = "tutor",
    save: bool = True,
    stream: bool = False,
) -> dict[str, Any]:
    """
    Process one user message. Returns a JSON-serializable result.

    kind: help | subjects | state | reviews | reset | new_subject |
          resume | clarify | message | error
    """
    if agent != "tutor":
        return {
            "ok": False,
            "agent": agent,
            "kind": "error",
            "response": f"Agent '{agent}' is not registered yet. Only 'tutor' is available.",
        }

    if state is None:
        state = T.load_state()
    if conversation is None:
        conversation = []

    text = (message or "").strip()
    if not text:
        return {
            "ok": False,
            "agent": "tutor",
            "kind": "error",
            "response": "Empty message.",
        }

    # Slash commands (no LLM)
    cmd_result = _handle_command(state, conversation, text)
    if cmd_result is not None:
        if save:
            T.save_state(state)
        return cmd_result

    # Pending goal collection (after /new)
    active = state.get("active_session") or {}
    if active.get("active") and active.get("pending_goal"):
        sid = active.get("subject_id")
        if sid and sid in state.get("subjects", {}):
            state["subjects"][sid]["goal"] = text[:300]
            state["subjects"][sid]["updated_at"] = datetime.now().isoformat()
        active["pending_goal"] = False
        T.save_state(state) if save else None
        # Fall through to first teaching turn with goal stored
        # Re-inject as learning intent
        text = f"Ik wil leren over dit onderwerp. Leerdoel: {text}"

    base_prompt = T.load_system_prompt()
    conversation.append({"role": "user", "content": text})
    if active.get("active"):
        state["active_session"]["question_number"] = int(
            state["active_session"].get("question_number") or 0
        ) + 1
        state["active_session"]["last_activity"] = datetime.now().isoformat()

    policy = T.suggest_next_activity(state)
    # Force non-streaming for API reliability (gateway can stream later)
    old_stream = T.STREAM
    T.STREAM = bool(stream)
    try:
        messages = T.build_messages(base_prompt, state, conversation, policy=policy)
        raw, result = T.ask_model_with_json_retry(messages)
    except Exception as e:
        conversation.pop()
        T.STREAM = old_stream
        return {
            "ok": False,
            "agent": "tutor",
            "kind": "error",
            "response": f"Model error: {e}",
        }
    finally:
        T.STREAM = old_stream

    stats = dict(getattr(T.ask_model, "last_stats", None) or {})

    if not result:
        answer = (raw or "").strip() or "I could not produce a valid tutor reply."
        conversation.append({"role": "assistant", "content": answer})
        if save:
            T.save_state(state)
        return {
            "ok": False,
            "agent": "tutor",
            "kind": "message",
            "response": answer,
            "raw_preview": (raw or "")[:400],
            "stats": stats,
            "summary": state_summary(state),
        }

    answer = str(result.get("response") or "").strip()
    assessment = result.get("assessment") or {}
    session_info = result.get("session") or {}
    routing = result.get("routing") or {}
    action = str((routing or {}).get("action") or "continue").lower()

    # ---- routing (quiet) ----
    if action == "list_subjects":
        names = [s["title"] for s in subjects_payload(state)]
        summary = (
            "Overzicht getoond. Onderwerpen: "
            + (", ".join(names) if names else "geen")
            + ". Wacht op keuze van de leerling."
        )
        conversation.append({"role": "assistant", "content": summary})
        if save:
            T.save_state(state)
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "subjects",
            "response": "",
            "subjects": subjects_payload(state),
            "stats": stats,
            "summary": state_summary(state),
        }

    if action == "clarify":
        if not answer:
            answer = "Welk onderwerp bedoel je precies?"
        conversation.append({"role": "assistant", "content": answer})
        T.update_session_memory(state, session_info if isinstance(session_info, dict) else {}, answer)
        if save:
            T.save_state(state)
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "clarify",
            "response": answer,
            "stats": stats,
            "summary": state_summary(state),
        }

    if action == "switch":
        raw_id = routing.get("subject_id") or routing.get("subject")
        resolved = T.resolve_subject_id(state, raw_id, title=str(raw_id or ""))
        matches = T.find_matching_subjects(state, str(raw_id or ""))
        if resolved and resolved in state.get("subjects", {}):
            best_id = resolved
        elif matches and matches[0][1] >= 50:
            best_id = matches[0][0]
        elif T.normalize_id(raw_id) in state.get("subjects", {}):
            best_id = T.normalize_id(raw_id)
        else:
            conversation.append({
                "role": "assistant",
                "content": f"Onderwerp \"{raw_id}\" niet gevonden.",
            })
            if save:
                T.save_state(state)
            return {
                "ok": True,
                "agent": "tutor",
                "kind": "subjects",
                "response": f"Onderwerp \"{raw_id}\" niet gevonden.",
                "subjects": subjects_payload(state),
                "stats": stats,
            }
        # Drop the user message from teaching conversation after switch
        return _quiet_switch(state, conversation, best_id)

    if action == "new_subject":
        title = routing.get("proposed_title") or routing.get("subject")
        if not title or len(str(title).strip()) > 60:
            if save:
                T.save_state(state)
            return {
                "ok": False,
                "agent": "tutor",
                "kind": "error",
                "response": "Could not create subject (title missing or too long).",
                "stats": stats,
            }
        title = str(title).strip()
        if active.get("active"):
            T.close_session(state, active.get("resume_instruction"))
        tax = routing.get("taxonomy") if isinstance(routing.get("taxonomy"), dict) else {}
        parent_id = tax.get("parent_id") or routing.get("parent_id")
        if parent_id:
            parent_id = T.resolve_subject_id(state, parent_id, title=str(parent_id)) or T.normalize_id(parent_id)
            if parent_id not in state.get("subjects", {}):
                parent_id = None
        subject_id = T.ensure_subject(state, title, title, parent_id=parent_id)
        T.apply_taxonomy_proposals(state, routing, focus_subject_id=subject_id)
        T.start_session(state, subject_id, pending_goal=True)
        conversation.clear()
        if save:
            T.save_state(state)
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "new_subject",
            "response": f"Nieuw onderwerp: {title}. Wat wil je hiermee bereiken?",
            "subject_id": subject_id,
            "subjects": subjects_payload(state),
            "stats": stats,
            "summary": state_summary(state),
        }

    # ---- continue: assessment ----
    if not state["active_session"].get("active"):
        sid = None
        if isinstance(assessment, dict):
            sid = assessment.get("subject")
        if sid and T.is_valid_id(sid):
            T.start_session(state, T.ensure_subject(state, sid, sid))

    try:
        T.apply_assessment(state, assessment)
    except Exception:
        pass

    if isinstance(session_info, dict):
        ri = session_info.get("resume_instruction")
        if ri and T.is_learner_facing_resume(str(ri)):
            state["active_session"]["resume_instruction"] = str(ri).strip()

    if not answer:
        answer = "Kun je dat in eigen woorden toelichten?"

    T.update_session_memory(
        state,
        session_info if isinstance(session_info, dict) else {},
        answer,
    )
    state["active_session"]["just_resumed"] = False
    state["active_session"]["last_activity"] = datetime.now().isoformat()
    T.rebuild_review_queue(state)

    conversation.append({"role": "assistant", "content": answer})
    # Cap conversation
    if len(conversation) > T.MAX_CONVERSATION_MESSAGES:
        del conversation[: len(conversation) - T.MAX_CONVERSATION_MESSAGES]

    if save:
        T.save_state(state)

    return {
        "ok": True,
        "agent": "tutor",
        "kind": "message",
        "response": answer,
        "routing": {"action": action},
        "policy": {
            "activity": policy.get("activity"),
            "force": policy.get("force"),
            "reason": policy.get("reason"),
            "concept_id": policy.get("concept_id"),
        },
        "stats": stats,
        "summary": state_summary(state),
    }


def bootstrap() -> dict:
    """Load state and return initial payload for the UI."""
    state = T.load_state()
    return {
        "ok": True,
        "agent": "tutor",
        "model": T.MODEL_NAME,
        "summary": state_summary(state),
        "subjects": subjects_payload(state),
    }
