#!/usr/bin/env python3
"""
Channel-agnostic tutor turn handler.

CLI, web gateway, and future Telegram adapters all call handle_turn().
No terminal printing. State is mutated and returned; caller saves if needed.
"""

from __future__ import annotations

import logging
import os
import re
import types as _types

import copy
from datetime import datetime
from typing import Any

import tutor as T
import intent_phrases as IP
import guards as G

logger = logging.getLogger(__name__)

_ECHO_REPAIR_USER = (
    "INTERNAL REPAIR (not shown to learner): your previous response violated "
    "the explain-then-restate rule — you asserted a mechanism and then asked "
    "the learner to restate that same mechanism.\n"
    "Rewrite the learner-facing response field ONLY:\n"
    "- Keep at most ONE short acknowledgement of what they said.\n"
    "- Do NOT restate the full mechanism you just gave.\n"
    "- End with ONE hinge / apply / contrast / predict question that lets the "
    "learner construct the insight, continuing INSIDE the same setup "
    "(premises unchanged; no new variable, no different 'real reason').\n"
    "- FORBIDDEN phrases: 'in one sentence explain why', 'in eigen woorden', "
    "and any question whose content words mostly repeat the lecture body.\n"
    "Return the same JSON shape. routing.action=continue."
)

_INTENTS = (
    "answer", "asks_hint", "asks_reveal", "asks_clarification", "side_question",
    "insight", "challenges_tutor", "frustration", "off_topic",
)
_CORRECTNESS = ("correct", "partial", "incorrect", "unknown")

_CLASSIFY_SYSTEM = (
    "You classify ONE learner message in a tutoring chat. Any language. Reply with JSON only: "
    '{"intent": "<intent>", "correctness": "<correct|partial|incorrect|unknown>"}. '
    "intent: answer = tries to answer the tutor's last question; asks_hint = asks for a hint, help or a "
    "smaller step; asks_reveal = asks to simply be told the answer or explanation; asks_clarification = "
    "does not understand the question; side_question = asks a new content question; insight = voices "
    "their own new inference; challenges_tutor = says the tutor is wrong or misattributed something; "
    "frustration = complains about the tutor or the session; off_topic. "
    "correctness only matters for intent=answer; use unknown when unsure or when the claim is subtle. "
    "Never explain."
)

_REPEAT_REPAIR = (
    "INTERNAL INSTRUCTION (not from the learner). Your draft reply re-asks something the learner already "
    "answered, or reuses the same question template. Write a replacement reply: at most one short sentence "
    "reacting to the learner's last message, then ONE question that takes the next small step INSIDE the "
    "current setup{target}. Do not repeat or rephrase this question: \"{avoid}\". Do not mention this "
    'instruction.\n{context}\nReturn JSON only: {{"response": "..."}}'
)


def llm_call(role, messages, **opts):
    """
    THE single entry point for every model call.
    Roles: main | classify | judge | evaluate | repair | reframe.
    Tests monkeypatch core.llm_call — do not call T.ask_model* elsewhere in this module.
    Returns SimpleNamespace(raw, parsed, stats).
    """
    if role == "main":
        raw, parsed, stats = T.ask_model_with_json_retry(
            messages,
            stream=opts.get("stream"),
            on_response_delta=opts.get("on_response_delta"),
        )
        return _types.SimpleNamespace(raw=raw, parsed=parsed, stats=stats or {})
    extra = {}
    if opts.get("think") is not None:
        extra["think"] = opts["think"]
    if opts.get("max_tokens"):
        extra["num_predict"] = opts["max_tokens"]
    try:
        raw, stats = T.ask_model(messages, stream=False, **extra)
    except TypeError:                      # older ask_model without think/num_predict
        raw, stats = T.ask_model(messages, stream=False)
    try:
        parsed = T.extract_json_object(raw)
    except Exception:
        parsed = None
    return _types.SimpleNamespace(
        raw=raw,
        parsed=parsed if isinstance(parsed, dict) else None,
        stats=stats or {},
    )


def _last_tutor_text(conversation):
    for m in reversed(conversation or []):
        if m.get("role") == "assistant":
            return str(m.get("content") or "")
    return ""


def _classify_turn(state, conversation, message):
    """Same-turn classification -> active_session.turn_intent / turn_correctness.

    Phase 1: always run so policy (later) and tests see a fresh signal.
    On failure: leave unset (never reuse previous turn).
    """
    act = state.get("active_session") or {}
    if not act.get("active") or not (message or "").strip():
        return None
    msgs = [
        {"role": "system", "content": _CLASSIFY_SYSTEM},
        {
            "role": "user",
            "content": (
                f"TUTOR'S LAST MESSAGE:\n{_last_tutor_text(conversation)[:700]}\n\n"
                f"LEARNER MESSAGE:\n{message[:700]}"
            ),
        },
    ]
    try:
        d = llm_call("classify", msgs).parsed or {}
    except Exception:
        logger.exception("classify call failed; falling back to legacy phrase flags")
        return None
    intent = str(d.get("intent") or "").strip().lower()
    corr = str(d.get("correctness") or "unknown").strip().lower()
    if intent not in _INTENTS:
        return None
    act["turn_intent"] = intent
    act["turn_correctness"] = corr if corr in _CORRECTNESS else "unknown"
    return {"intent": act["turn_intent"], "correctness": act["turn_correctness"]}


_REFRAME_SYSTEM = (
    "You are a tutor in a learning chat. The learner is frustrated (repetition, level or pace). Reply in "
    "the SAME LANGUAGE as the learner's message. Do NOT ask a quiz question about the topic. In at most 4 "
    "short sentences: (1) acknowledge their point plainly, without long apologies; (2) say in one sentence "
    "what they have already shown, using ONLY the points listed; (3) let them choose what to do next from "
    "the options listed (phrase them naturally in their language) and ask which they prefer. "
    'Return JSON only: {"response": "..."}'
)
_LOOP_SYSTEM = (
    "You are a tutor in a learning chat. Your draft would have repeated a question the learner already answered or ignored. "
    "Reply in the SAME LANGUAGE as the learner's message with at most 3 short sentences: (1) react SPECIFICALLY to what the "
    "learner just said (confirm what is right in it; if you cannot verify it, say so plainly); (2) ask ONE NEW question that "
    "builds on THEIR statement and was not asked before. Do NOT repeat or rephrase your previous question. Do NOT offer to end, "
    "summarise or wrap up, and do NOT offer a menu of options. Do NOT say what the learner has or has not shown unless it is "
    'listed. Return JSON only: {"response": "..."}'
)
_DIMS = ("knowledge", "retrieval", "application", "explanation", "transfer")


# ======================================================================================
# Judge: an impartial correctness check that runs BEFORE the tutor writes its reply.
# ======================================================================================
_JUDGE_RULES = (
    "You are an impartial judge in a tutoring chat. Any language. Judge TWO separate things about the learner's message. "
    "(1) claim: is what the learner STATES true? Decide ONLY from the SETUP and the SOURCE EVIDENCE; if they do not let you "
    "decide, answer unverified. Do not call something false on general knowledge unless you are certain. For claim=true or "
    "claim=false you MUST quote the decisive words VERBATIM from the SETUP or the SOURCE EVIDENCE in `support`; if you cannot "
    "quote them, the claim is unverified. The evidence may describe several modes or scopes: use only the part that is about "
    "the mode the learner is talking about. "
    "(2) responsive: does the message answer the tutor's question: yes, partly or no. "
    "A true statement that does not answer the question is claim=true with responsive=no: it is NOT false. "
    "If the learner is disputing something the tutor said, judge whether the learner's point is right. "
    "The tutor's key may be wrong: never defer to it. Solve the tutor's question yourself from the SETUP and the SOURCE EVIDENCE. "
)
_JUDGE_SYSTEM = (
    _JUDGE_RULES + "Reply with JSON only: "
    '{"claim": "true|false|unverified", "support": "verbatim excerpt from the setup or evidence, or null", '
    '"responsive": "yes|partly|no", "reason": "one short sentence", '
    '"missing": "what is missing for a complete answer, or null"}'
)
# verdicts DERIVED by Python from (claim, responsive): false -> incorrect; unverified -> uncertain;
# true+yes -> correct; true+partly -> partial; true+no -> off_target (true, but it does not answer the question).
# A true/false claim only counts when its `support` quote is found in the setup/evidence (otherwise it is downgraded to unverified).
_JUDGE_VERDICTS = ("correct", "partial", "incorrect", "uncertain", "off_target")

# Judge style. Live evidence (gpu01, 2026-10-26): with think="low" and a 500-token cap the model spent the whole budget
# THINKING on the hard cases (11 s = 500 tokens at 59 tok/s), returned no JSON, and llm.py then handed the thinking prose
# to the JSON parser: 4 of 6 judge calls failed in a real session, exactly on the contested answers. "cot_json" keeps the
# reasoning but INSIDE the JSON (field `analysis` first), think off, fixed length (~3-4 s, no truncation of the reasoning).
JUDGE_STYLE = os.environ.get("TUTOR_JUDGE_STYLE", "cot_json").strip().lower()      # "cot_json" | "think"
# The echo-repair heuristic had recall 2/10 and replaced a BETTER draft with a worse one (2026-10-26 14:13).
# Keep it switchable; `stats.echo_flagged` is logged even when it is off, so its hit rate can be audited.
ECHO_REPAIR = os.environ.get("TUTOR_ECHO_REPAIR", "1").strip() != "0"


_JUDGE_SYSTEM_COT = (
    _JUDGE_RULES + "Reply with JSON only, keys in THIS order: "
    '{"analysis": "2-3 short sentences: decide the truth of the learner\'s statement from the setup and the evidence, '
    'then say whether it answers the question", "claim": "true|false|unverified", '
    '"support": "verbatim excerpt from the setup or evidence, or null", "responsive": "yes|partly|no", '
    '"reason": "one short sentence", "missing": "what is missing for a complete answer, or null"}'
)


def _judge_trigger(act):
    """Judge only where the tutor would otherwise grade its own homework."""
    return (bool(act.get("active"))
            and act.get("turn_intent") in ("answer", "insight", "challenges_tutor")
            and act.get("turn_correctness") in ("partial", "incorrect", "unknown"))


def _as_dict(x):
    """Model output is untrusted: a field that should be an object may be a string or a list."""
    return x if isinstance(x, dict) else {}


def _norm(x):
    return str(x or "").strip().lower()


def _evidence_lines(evidence, limit=3, chars=350):
    """Short excerpts for the judge. Query-centered when possible (not blind prefix)."""
    if not isinstance(evidence, dict):
        return []
    query = str(evidence.get("query") or "")
    try:
        from knowledge.chunking import excerpt_for_query
    except Exception:
        excerpt_for_query = None  # type: ignore
    out = []
    for s in (evidence.get("snippets") or [])[:limit]:
        t = (s.get("text") or s.get("snippet") or s.get("content") or "") if isinstance(s, dict) else s
        t = str(t or "").strip()
        if not t:
            continue
        if excerpt_for_query is not None:
            t = excerpt_for_query(t, query, max_chars=chars)
        else:
            t = t[:chars]
        if t:
            out.append(t)
    return out


def _derive_verdict(d):
    """(claim, responsive) -> verdict. A TRUE statement that does not answer the question is never 'incorrect'.
    Falls back to a legacy `verdict` field (older prompts / scripted tests)."""
    claim, resp = _norm(d.get("claim")), _norm(d.get("responsive"))
    if claim in ("true", "false", "unverified") and resp in ("yes", "partly", "no"):
        if claim == "false":
            return "incorrect"
        if claim == "unverified":
            return "uncertain"
        return {"yes": "correct", "partly": "partial", "no": "off_target"}[resp]
    v = _norm(d.get("verdict"))
    return v if v in _JUDGE_VERDICTS else None


def _norm_text(s):
    return re.sub(r"\s+", " ", re.sub(r"[\"“”«»‘’`*_]", "", str(s or "")).lower()).strip(" .,;:")


def _verify_support(support, haystack):
    """The judge must QUOTE what decides the claim. Evidence (stateful EMS run, 2026-11-10): the judge called a false claim true
    and a true one false in 5 of 11 decisive verdicts; without a verifiable quote a verdict is only a guess."""
    s = _norm_text(support)
    if len(s) < 12 or s in ("null", "none"):
        return False
    return any(s in _norm_text(h) for h in (haystack or []))


def _judge_call(system, body, opts):
    res = llm_call("judge", [{"role": "system", "content": system}, {"role": "user", "content": body}], **opts)
    return _as_dict(res.parsed), (getattr(res, "stats", None) or {}).get("done_reason") == "length"


def _judge_turn(state, conversation, text, evidence=None):
    act = state.get("active_session") or {}
    act["_judge_meta"] = {"called": False, "failed": False}
    if not _judge_trigger(act):
        return None
    ot = T.get_open_thread(act) or {}
    prem = [str(p) for p in (ot.get("premises") or []) if str(p).strip()]
    last = _last_tutor_text(conversation)
    question = G.final_question(last) or last[-300:]
    key = ((ot.get("gap_card") or {}).get("key") if isinstance(ot.get("gap_card"), dict) else None)
    setup = "SETUP (givens set by the tutor):\n" + "\n".join(f"- {p}" for p in prem) if prem else "SETUP: (none stated)"
    ev = _evidence_lines(evidence)
    ev_block = ("SOURCE EVIDENCE (excerpts; absence of evidence is not proof of falsity):\n" + "\n".join(f"- {e}" for e in ev)
                if ev else "SOURCE EVIDENCE: (none)")
    body = (f"{setup}\n\n{ev_block}\n\nTUTOR'S QUESTION:\n{question[:400]}\n\nLEARNER'S MESSAGE:\n{(text or '')[:700]}"
            f"\n\nTUTOR'S KEY (untrusted, may be wrong):\n{(str(key) if key else '(none)')[:300]}")
    meta = act["_judge_meta"]
    meta["called"] = True
    meta["evidence"] = len(ev)
    style = JUDGE_STYLE if JUDGE_STYLE in ("cot_json", "think") else "cot_json"
    meta["style"] = style
    system = _JUDGE_SYSTEM_COT if style == "cot_json" else _JUDGE_SYSTEM
    opts = {"think": False, "max_tokens": 600} if style == "cot_json" else {"think": "low", "max_tokens": 1200}
    try:
        d, trunc = _judge_call(system, body, opts)
    except Exception:
        logger.exception("judge call failed; the turn continues without a judge")
        meta["failed"] = True
        return None
    meta["truncated"] = trunc
    if _derive_verdict(d) is None:
        # Truncated (or unusable) on the turn that mattered most (2 of 3 misconception turns in the stateful run): ONE cheap
        # retry without analysis. Only failures pay for it.
        meta["retry"] = True
        try:
            d2, trunc2 = _judge_call(_JUDGE_SYSTEM, body, {"think": False, "max_tokens": 250})
            meta["truncated"] = trunc or trunc2
            if _derive_verdict(d2) is not None:
                d = d2
        except Exception:
            logger.exception("judge retry failed")
    claim = _norm(d.get("claim"))
    sv, downgraded = None, False
    if claim in ("true", "false"):
        sv = _verify_support(d.get("support"), prem + ev)
        if not sv:
            d = dict(d)
            d["claim"] = "unverified"          # a verdict without a quote from the setup/evidence is a guess
            downgraded = True
    v = _derive_verdict(d)
    if v is None:
        meta["failed"] = True
        return None
    miss = d.get("missing")
    act["judge"] = {"verdict": v, "claim": _norm(d.get("claim")) or None, "responsive": _norm(d.get("responsive")) or None,
                    "reason": str(d.get("reason") or "")[:240],
                    "analysis": str(d.get("analysis") or "")[:400],
                    "support": (str(d.get("support"))[:240] if d.get("support") else None),
                    "support_verified": sv, "downgraded": downgraded,
                    "missing": (str(miss)[:240] if miss and str(miss).lower() not in ("null", "none") else None)}
    # Off-target streak: consecutive plain answers that do not address the tutor's question. Two in a row means the
    # TUTOR's line of questioning is not landing (the learner keeps offering valid points of their own).
    if act.get("turn_intent") == "answer":
        r = act["judge"]["responsive"]
        if r == "no":
            act["off_target_streak"] = int(act.get("off_target_streak") or 0) + 1
        elif r in ("yes", "partly"):
            act["off_target_streak"] = 0
    return act["judge"]


def _evidence_discipline(evidence):
    """One instruction per turn, appended to the policy reason (the dynamic tail of the prompt, so the cached prefix is untouched).
    Evidence (EMS runs): the tutor adopted terms the source does not contain ("6A-rem"), invented thresholds and quoted words that
    were not in the source; on a turn WITHOUT evidence it invented a sign convention."""
    if not isinstance(evidence, dict) or not evidence.get("enabled"):
        return ""
    if _evidence_lines(evidence, limit=1):
        return ("EVIDENCE DISCIPLINE: state facts about the system ONLY if they are in the SOURCE EVIDENCE, and apply them only to "
                "the mode/scope they are about. Do NOT invent numbers, thresholds, rules or terms. If the learner uses a term that "
                "is not in the evidence, ask what they mean by it instead of defining it yourself. Use quotation marks only for "
                "words that are verbatim in the evidence or in the learner's message. Never say what the learner has or has not "
                "shown unless it is listed.")
    return ("NO SOURCE EVIDENCE for this turn: do NOT state facts, numbers, thresholds or rules about the system. Ask the learner a "
            "question, or say plainly that you cannot verify this from the sources. Never say what the learner has or has not "
            "shown unless it is listed.")


def _gate_hint(gate, state):
    """Reason-specific instruction for the repair (the generic one only says 'repeat')."""
    r = getattr(gate, "reason", "")
    if r == "learner_echo":
        return "\nYOUR DRAFT ONLY REPEATED THE LEARNER'S OWN WORDS: write a reply of your own (react to what they said, then ask ONE question)."
    if r == "dead_probe":
        probes = [str(q) for q in ((state.get("active_session") or {}).get("recent_probes") or [])[-3:]]
        return ("\nTHE LEARNER HAS TWICE NOT ANSWERED THESE QUESTIONS; NEVER ASK THEM AGAIN, ALSO NOT REPHRASED:\n"
                + "\n".join(f"- {q}" for q in probes))
    if r in ("identical_question", "identical_reply", "repeat"):
        return "\nYOUR DRAFT REPEATS A QUESTION OR REPLY YOU ALREADY USED: take a genuinely new step."
    return ""


def _extra_gate(answer, hist, state):
    """Two cheap rules the lexical repeat gate misses (evidence: pilot_ems_v3 turns 6-8; george 2026-10-20 turns 167/169):
    (1) the SAME final question as one the learner already answered, whatever the preamble; (2) a probe the learner has
    twice not answered."""
    norm = lambda s: " ".join(str(s or "").split()).strip().lower()
    answered = G._answered_tutor_turns(hist)
    for idx, text in reversed(answered[-3:]):
        if norm(answer) and norm(answer) == norm(text):             # byte-identical (also when there is no question at all)
            return G.GateResult(True, "identical_reply", 1.0, idx)
    # a reply that (nearly) repeats the LEARNER's own recent words (stateless EMS run: the tutor sent the learner's message back)
    ta = G.tokens(answer)
    if len(ta) >= 4:
        for h in [h for h in hist if h["role"] == "user"][-3:]:
            tb = G.tokens(h["text"])
            if len(tb) >= 4 and len(ta & tb) / len(ta | tb) >= 0.8:
                return G.GateResult(True, "learner_echo", round(len(ta & tb) / len(ta | tb), 2), None)
    cq = G.final_question(answer)
    if not cq or len(G.tokens(cq)) < 4:
        # a contentless follow-up ("And why?") is legitimately asked again after a different preamble: only the identical-reply
        # rule applies to it (measured: it was the only false alarm on 3 real sessions)
        return None
    w = G.build_weights([h["text"] for h in hist if h["role"] == "assistant"] + [answer])
    for idx, text in reversed(G._answered_tutor_turns(hist)[-6:]):
        q = G.final_question(text)
        if q:
            s = G.similarity(cq, q, w)
            if s >= 0.95:
                return G.GateResult(True, "identical_question", s, idx)
    act = state.get("active_session") or {}
    if int(act.get("off_target_streak") or 0) >= 2:
        for q in (act.get("recent_probes") or [])[-3:]:
            s = G.similarity(cq, str(q), w)
            if s >= 0.5:
                return G.GateResult(True, "dead_probe", s, None)
    return None


def _note_probe(state, answer, action):
    """Remember the questions we asked (last 4): the ban list for a probe that is not landing."""
    act = state.get("active_session") or {}
    fq = G.final_question(answer)
    if action not in ("continue", "clarify", "") or not fq or not act.get("active"):
        return
    lst = [q for q in (act.get("recent_probes") or []) if isinstance(q, str)]
    lst.append(fq[:240])
    act["recent_probes"] = lst[-4:]


_CONCEDE_SYSTEM = (
    "You are a tutor in a learning chat. The learner just corrected or challenged you. Reply in the SAME LANGUAGE as the "
    "learner's message. In at most 3 short sentences: (1) acknowledge SPECIFICALLY what they said or objected to and say plainly "
    "whether they are right (follow the JUDGE line if there is one; if you are not sure, say so); (2) say what you will do "
    "differently; (3) ask ONE question that builds on THEIR point. Do NOT repeat or rephrase your previous reply or your "
    'previous question. Return JSON only: {"response": "..."}'
)


_EMPTY_SYSTEM = (
    "You are a tutor in a learning chat. Your previous reply was EMPTY. Reply in the SAME LANGUAGE as the learner's message with "
    "at most 2 short sentences: react briefly to what they said, then ask ONE question that continues the current thread (do not "
    'repeat a question the learner already answered). Return JSON only: {"response": "..."}'
)


def _empty_reply(state, message, policy):
    ot = T.get_open_thread(state.get("active_session") or {}) or {}
    body = (f"LEARNER MESSAGE:\n{(message or '')[:500]}\n\nCURRENT THREAD QUESTION (re-ask in NEW words only if the learner "
            f"did not answer it): {str(ot.get('q') or '(none)')[:300]}\n\n" + _repair_context(state, policy))
    res = llm_call("repair", [{"role": "system", "content": _EMPTY_SYSTEM}, {"role": "user", "content": body}])
    return str(_as_dict(res.parsed).get("response") or "").strip() or None


def _concede_reply(state, hist, message, policy):
    """Small side call for a turn where the learner challenged us and the draft is a bare repeat."""
    act = state.get("active_session") or {}
    prev = next((h["text"] for h in reversed(hist) if h["role"] == "assistant"), "")
    j = act.get("judge") or {}
    jl = f"\nJUDGE ON THEIR POINT: {j.get('verdict')} - {j.get('reason')}" if j.get("verdict") else ""
    body = (f"LEARNER MESSAGE:\n{(message or '')[:500]}{jl}\n\nYOUR PREVIOUS REPLY (do not repeat it):\n{prev[:400]}\n\n"
            + _repair_context(state, policy))
    res = llm_call("repair", [{"role": "system", "content": _CONCEDE_SYSTEM}, {"role": "user", "content": body}])
    return str(_as_dict(res.parsed).get("response") or "").strip() or None


def _reframe_options(state):
    act = state.get("active_session") or {}
    concepts = ((state.get("subjects") or {}).get(act.get("subject_id")) or {}).get("concepts") or {}
    cur_id = act.get("current_concept_id")
    opts = []
    if cur_id in concepts:
        opts.append(f"go deeper: {concepts[cur_id].get('title') or cur_id}")
    others = sorted(((k, c) for k, c in concepts.items() if k != cur_id),
                    key=lambda kc: sum(float(kc[1].get(d) or 0) for d in _DIMS))
    opts += [f"switch to: {c.get('title') or k}" for k, c in others[:2]]
    opts.append("wrap up: short summary of what they showed, then stop")
    return opts


def _reframe(state, message, avoid=None, kind="policy"):
    """Small side call (NOT the big prompt): acknowledge, summarise what was shown, offer a choice.
    `avoid`: our previous reply, which must not be repeated (a frustrated learner who complains twice gets a NEW reply)."""
    act = state.get("active_session") or {}
    # only what the LEARNER demonstrated; "Established:" lines are tutor assertions (unverified)
    shown = [str(p).split(":", 1)[1].strip()[:140] for p in (act.get("established_points") or [])
             if str(p).lower().startswith("learner showed")][-4:]
    _j = act.get("judge") or {}
    jline = (f"\n\nJUDGE ON THEIR LAST CLAIM (say one honest sentence about it): {_j.get('verdict')} - {_j.get('reason')}"
             if _j.get("verdict") else "")
    # "(nothing recorded)" used to make the model announce that the learner had shown NOTHING (stateful EMS run, 3 replies)
    shown_block = (("\n\nALREADY SHOWN:\n" + "\n".join(f"- {s}" for s in shown)) if shown else
                   "\n\n(There is no record of what the learner has shown: do not say that they have or have not shown anything.)")
    prev_block = (f"\n\nYOUR PREVIOUS REPLY (do not repeat it or its wording):\n{str(avoid)[:400]}" if avoid else "")
    if kind == "loop":
        asked = [str(q) for q in (act.get("recent_probes") or [])[-3:]]
        body = (f"LEARNER MESSAGE:\n{(message or '')[:500]}{jline}{shown_block}{prev_block}"
                + (("\n\nQUESTIONS ALREADY ASKED (do not ask them again):\n" + "\n".join(f"- {q}" for q in asked)) if asked else ""))
        system = _LOOP_SYSTEM
    else:
        body = (f"LEARNER MESSAGE:\n{(message or '')[:500]}{jline}{shown_block}"
                + "\n\nOPTIONS (descriptors, not final wording):\n"
                + "\n".join(f"- {o}" for o in _reframe_options(state)) + prev_block)
        system = _REFRAME_SYSTEM
    res = llm_call("reframe", [{"role": "system", "content": system},
                               {"role": "user", "content": body}])
    return str((res.parsed or {}).get("response") or "").strip() or None


def _repair_context(state, policy):
    """Everything a repair must respect: premises, what the learner showed, the policy instruction."""
    act = state.get("active_session") or {}
    ot = T.get_open_thread(act) or {}
    prem = [str(p) for p in (ot.get("premises") or []) if str(p).strip()]
    shown = [str(p).split(":", 1)[1].strip()[:140] for p in (act.get("established_points") or [])
             if str(p).lower().startswith("learner showed")][-4:]
    lines = ["CONSTRAINTS (non-negotiable):",
             "- Keep the setup and premises exactly as listed; do not change them and do not introduce a new "
             "variable or a different 'real reason'.",
             "- Follow the policy instruction; do not contradict it.",
             "- Do not re-explain or re-ask what the learner has already shown."]
    if prem:
        lines += ["PREMISES:"] + [f"- {p}" for p in prem]
    if shown:
        lines += ["ALREADY SHOWN BY THE LEARNER:"] + [f"- {s}" for s in shown]
    pr = str((policy or {}).get("reason") or "")[:500]
    if pr:
        lines.append("POLICY: " + pr)
    return "\n".join(lines)


def _repair_repeat(messages, avoid_q, next_target, context=""):
    """One response-only repair when the pre-send gate blocks a draft."""
    tgt = f" (aim at: {next_target})" if next_target else ""
    inst = _REPEAT_REPAIR.format(target=tgt, avoid=(avoid_q or "")[:200], context=context or "")
    res = llm_call("repair", list(messages) + [{"role": "user", "content": inst}])
    return str((res.parsed or {}).get("response") or "").strip() or None


_EVAL_SYSTEM = (
    "You check whether a learner's claim is correct GIVEN a stated setup. Any language. The tutor's key "
    "may itself be wrong: never trust it over the setup. Reply with JSON only: "
    '{"verdict": "correct|incorrect|uncertain", "reason": "one short sentence"}. '
    "Answer uncertain when the claim depends on subtle effects or when you are not sure."
)


def _evaluate_correction(premises, claim, key):
    """Correction gate: premises + learner claim; tutor key is UNTRUSTED."""
    setup = (
        "SETUP (givens):\n" + "\n".join(f"- {p}" for p in premises)
        if premises
        else "SETUP: (none stated)"
    )
    body = (
        f"{setup}\n\nLEARNER CLAIM:\n{(claim or '')[:600]}\n\n"
        f"TUTOR KEY (untrusted):\n{(key or '(none)')[:300]}"
    )
    try:
        d = llm_call(
            "evaluate",
            [
                {"role": "system", "content": _EVAL_SYSTEM},
                {"role": "user", "content": body},
            ],
        ).parsed or {}
    except Exception:
        logger.exception("evaluate call failed")
        return "uncertain"
    v = str(d.get("verdict") or "uncertain").strip().lower()
    return v if v in ("correct", "incorrect", "uncertain") else "uncertain"


def _repair_echo_teach_turn(messages, result, answer, *, policy=None, context=""):
    """One silent LLM call to replace an explain-then-restate reply.

    Returns (new_answer: str|None, stats: dict|None).
    Kept as secondary fallback after the deterministic gate.
    """
    if not answer or not messages:
        return None, None
    preview = str(answer)[:500]
    repair_messages = list(messages) + [
        {
            "role": "assistant",
            "content": str(result.get("response") or answer)[:800],
        },
        {"role": "user", "content": _ECHO_REPAIR_USER + (("\n\n" + context) if context else "")
         + f"\n\nBad draft was:\n{preview}"},
    ]
    try:
        res2 = llm_call("repair", repair_messages)
        raw2, stats2 = res2.raw, res2.stats
        parsed2 = res2.parsed
    except Exception as e:
        logger.warning("echo repair call failed: %s", e)
        return None, None
    if not isinstance(parsed2, dict):
        return None, stats2
    new_ans = str(parsed2.get("response") or "").strip()
    if not new_ans or len(new_ans) < 20:
        return None, stats2
    if T.response_looks_like_echo_teach(new_ans):
        if "?" not in new_ans:
            return None, stats2
        if len(new_ans) >= len(answer):
            return None, stats2
    if isinstance(result, dict):
        result["response"] = new_ans
        # Keep FIRST-call assessment/session; only replace response text.
    return new_ans, stats2


def _subject_last_activity(state: dict, sid: str, sub: dict) -> str:
    """ISO timestamp of most recent practice on this subject (concepts or history)."""
    best = str(sub.get("updated_at") or sub.get("created_at") or "")
    for cpt in (sub.get("concepts") or {}).values():
        if not isinstance(cpt, dict):
            continue
        for key in ("last_practiced", "last_review"):
            t = str(cpt.get(key) or "")
            if t > best:
                best = t
    for h in state.get("history") or []:
        if isinstance(h, dict) and h.get("subject_id") == sid:
            t = str(h.get("date") or "")
            if t > best:
                best = t
    return best


def subjects_payload(state: dict) -> list[dict]:
    """Subjects for UI/API. Sorted: least-recently-active first (good for 'what to review')."""
    out = []
    subjects = state.get("subjects") or {}
    for sid, sub in subjects.items():
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
        last_act = _subject_last_activity(state, sid, sub)
        item = {
            "id": sid,
            "title": sub.get("title", sid),
            "goal": sub.get("goal"),
            "n_concepts": len(concepts),
            "open_mc": n_open,
            "status": T.subject_status_line(state, sid),
            "parent_id": sub.get("parent_id"),
            "related": list(sub.get("related") or []),
            "last_activity": last_act or None,
        }
        out.append(item)
    # Oldest activity first (None last); then title
    out.sort(key=lambda x: (x.get("last_activity") is None, x.get("last_activity") or "", (x.get("title") or "").lower()))
    return out


def lobby_overview(state: dict) -> str:
    """Plain-language meta overview for the lobby (no teaching)."""
    T.rebuild_review_queue(state)
    items = subjects_payload(state)
    due = state.get("review_queue") or []
    lines = [
        "Dit is de lobby — navigatie, geen les.",
        "Zeg bv. “ik wil verder met agents”, “welke onderwerpen?”, “/tree”, “/review”.",
        "Of tik een onderwerp in ☰.",
        "",
    ]
    if not items:
        lines.append("Nog geen onderwerpen. Start met /new <kort onderwerp>.")
        return "\n".join(lines)
    lines.append(f"Onderwerpen ({len(items)}), minst recent eerst:")
    for it in items:
        la = it.get("last_activity") or "—"
        if la and la not in ("—",) and "T" in str(la):
            la = str(la).replace("T", " ")[:16]
        bits = [it.get("title") or it["id"]]
        if it.get("n_concepts"):
            bits.append(f"{it['n_concepts']} concepten")
        if it.get("open_mc"):
            bits.append(f"{it['open_mc']} open MC")
        bits.append(f"laatst: {la}")
        lines.append(" · ".join(bits))
    if due:
        lines.append("")
        lines.append(f"Due reviews: {len(due)}")
    return "\n".join(lines)


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


def _subject_detail_lines(state: dict) -> list:
    """Lines for every subject (not only the active one) for /state display."""
    lines = []
    for item in subjects_payload(state):
        sid = item["id"]
        sub = (state.get("subjects") or {}).get(sid) or {}
        concepts = sub.get("concepts") or {}
        lines.append(f"  {item['title']} ({sid})")
        if item.get("goal"):
            g = str(item["goal"])
            lines.append(f"    goal: {g[:80]}{'…' if len(g) > 80 else ''}")
        if not concepts:
            lines.append("    concepts: (none yet)")
            continue
        for cid, cpt in concepts.items():
            if not isinstance(cpt, dict):
                continue
            avg = T.average_mastery(cpt)
            open_mc = sum(
                1
                for m in (cpt.get("misconceptions") or [])
                if isinstance(m, dict) and m.get("status", "open") != "resolved"
            )
            mc_bit = f", MC open={open_mc}" if open_mc else ""
            lines.append(f"    {cpt.get('title', cid)}: avg={avg:.2f}{mc_bit}")
        if item.get("status"):
            lines.append(f"    status: {item['status']}")
    return lines


def state_summary(state: dict) -> dict:
    active = state.get("active_session") or {}
    T.rebuild_review_queue(state)
    return {
        "version": state.get("version"),
        "learner_id": (state.get("learner") or {}).get("id"),
        "subjects": subjects_payload(state),
        "subject_lines": _subject_detail_lines(state),
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



def _looks_like_navigation(text: str) -> bool:
    """True if the message is mainly 'switch/continue topic', not a content answer.

    Long replies (diagnostic answers, explanations) often contain words that
    also match subject-intent regexes; those must stay on the teaching thread.
    """
    t = (text or "").strip()
    if not t:
        return False
    # Substantial answers are never pure navigation
    if len(t) > 100:
        return False
    if t.count(" ") >= 18:  # ~18+ words
        return False
    return True

def _handle_command(state: dict, conversation: list, cmd: str, user_id: str | None = None) -> dict | None:
    """Return a response dict for slash commands, or None if not a command."""
    low = cmd.lower().strip()

    if low in ("/help", "help"):
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "help",
            "response": (
                "Commands:\n"
                "  /subjects     lijst onderwerpen\n"
                "  /tree         concept parent/child boom\n"
                "  /state        korte samenvatting\n"
                "  /state full   iets meer detail (geen ruwe JSON in chat)\n"
                "  /review       due reviews\n"
                "  /new <topic>  nieuw onderwerp\n"
                "  /rename <titel>           hernoem actief onderwerp\n"
                "  /rename <oud> -> <nieuw>  hernoem bestaand onderwerp (ook vanuit lobby)\n"
                "  /reset        alleen chatcontext leeg\n"
                "  /quit         sessie opslaan / afsluiten\n"
                "  /debug tokens prompt component sizes (no LLM)\n"
                "  /help\n"
                "Natuurlijke taal: “ik wil verder met agents”, “welke onderwerpen?”"
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

    if low in ("/tree", "/concepttree", "/concepts"):
        # include learner id so multi-tab cookie mix is obvious
        try:
            tree_text = T.format_concept_tree(state)
        except Exception:
            # Fallback: call print-style builder if present
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                if hasattr(T, "print_concept_tree"):
                    T.print_concept_tree(state)
                else:
                    buf.write("(tree unavailable)")
            tree_text = buf.getvalue()
        lid = (state.get("learner") or {}).get("id") or user_id or "?"
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "tree",
            "response": f"User: {lid}\n" + (tree_text or "(geen concepten)"),
            "subjects": subjects_payload(state),
            "user_id": lid,
        }

    if low == "/state":
        summary = state_summary(state)
        active = state.get("active_session") or {}
        lid = (state.get("learner") or {}).get("id") or user_id or "?"
        lines = [
            f"User: {lid}",
            f"Active: {bool(active.get('active'))}"
            + (
                f" · {active.get('subject_id')} / {active.get('current_concept_id') or '—'}"
                if active.get("active")
                else ""
            ),
            f"Due reviews: {summary.get('due_reviews', 0)}",
            "Subjects:",
        ]
        lines.extend(summary.get("subject_lines") or ["  (none)"])
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "state",
            "response": "\n".join(lines),
            "summary": summary,
            "subjects": subjects_payload(state),
            "user_id": lid,
        }

    if low in ("/state full", "/statefull", "/json"):
        summary = state_summary(state)
        active = state.get("active_session") or {}
        lines = [
            f"version: {state.get('version')}",
            f"learner: {(state.get('learner') or {}).get('id')}",
            f"active: {bool(active.get('active'))}",
            f"subject: {active.get('subject_id')}",
            f"concept: {active.get('current_concept_id')}",
            f"activity: {active.get('current_activity')}",
            f"questions: {active.get('question_number')}",
            f"thread: {active.get('session_thread') or '—'}",
            f"last_q: {active.get('last_tutor_question') or '—'}",
            f"due_reviews: {len(state.get('review_queue') or [])}",
            "",
            "All subjects & concepts:",
        ]
        lines.extend(summary.get("subject_lines") or ["  (none)"])
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "state_full",
            "response": "\n".join(lines),
            "summary": summary,
        }

    if low in ("/quit", "/exit", "/stop"):
        active = state.get("active_session") or {}
        if active.get("active"):
            T.close_session(state, active.get("resume_instruction"), user_id=user_id)
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "quit",
            "response": (
                "Sessie opgeslagen. Je kunt later verder met "
                "“ik wil verder met …” of via ☰."
            ),
            "summary": state_summary(state),
            "subjects": subjects_payload(state),
        }

    if low in ("/debug tokens", "/debug", "/metrics"):
        try:
            from metrics_lib import format_breakdown, get_last_breakdown
            base = T.load_system_prompt()
            policy = T.suggest_next_activity(state)
            T.build_messages(base, state, conversation, policy=policy)
            bd = get_last_breakdown() or {}
            text = format_breakdown(bd.get("components") or [], bd.get("total_tokens_est"))
            text += f"\nsystem_hash={bd.get('system_hash')} mode={bd.get('mode')}"
        except Exception as e:
            text = f"Metrics error: {e}"
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "debug",
            "response": text,
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
        # Keep UI history; model still only sees last N messages.
        conversation.append({
            "role": "system",
            "content": "[session boundary — topic switch or reset]",
        })
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "reset",
            "response": "Conversation context reset. Learner state kept.",
        }

    if low.startswith("/new "):
        title = T.short_subject_title(cmd[5:].strip())
        if not title or title == "general":
            return {
                "ok": False,
                "agent": "tutor",
                "kind": "error",
                "response": "Use /new <short topic> (max ~4 words).",
            }
        active = state["active_session"]
        if active.get("active"):
            T.close_session(state, active.get("resume_instruction"), user_id=user_id)
        subject_id = T.ensure_subject(state, title, title)
        T.start_session(state, subject_id, pending_goal=True, user_id=user_id)
        # Keep UI history; model still only sees last N messages.
        conversation.append({
            "role": "system",
            "content": "[session boundary — topic switch or reset]",
        })
        T.save_state(state, user_id=user_id)
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "new_subject",
            "response": f"New subject: {title}. Does that title work (say e.g. 'call it EMS' to rename), and what do you want to achieve?",
            "subject_id": subject_id,
            "subjects": subjects_payload(state),
            "routing": {"action": "new_subject", "subject": subject_id},
            "switched_from_thread": True,
        }

    if low.startswith("/rename"):
        pair = T.parse_subject_rename_pair(cmd)
        if not pair or not pair[1]:
            return {
                "ok": False,
                "agent": "tutor",
                "kind": "error",
                "response": (
                    "Gebruik:\n"
                    "  /rename <nieuwe titel>           (actief onderwerp)\n"
                    "  /rename <oud> -> <nieuw>         (elk onderwerp, ook vanuit lobby)\n"
                    "Voorbeeld: /rename EMS Eco Wet -> EMS"
                ),
            }
        old_hint, new_title = pair
        active = state.get("active_session") or {}
        sid = None
        if old_hint:
            sid = T.resolve_subject_for_rename(state, old_hint)
            if not sid:
                return {
                    "ok": False,
                    "agent": "tutor",
                    "kind": "error",
                    "response": f"Geen onderwerp gevonden dat lijkt op «{old_hint}».",
                }
        else:
            sid = active.get("subject_id")
            if not sid or sid not in state.get("subjects", {}):
                return {
                    "ok": False,
                    "agent": "tutor",
                    "kind": "error",
                    "response": (
                        "Geen actief onderwerp. Open er een in de sidebar, of gebruik\n"
                        "/rename <oud> -> <nieuw>"
                    ),
                }
        old_title = (state["subjects"][sid].get("title") or sid)
        new_t = T.rename_subject_title(state, sid, new_title)
        T.save_state(state, user_id=user_id)
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "message",
            "response": f"Onderwerp «{old_title}» hernoemd naar «{new_t or new_title}».",
            "subject_id": sid,
            "subjects": subjects_payload(state),
            "summary": state_summary(state),
            "stats": {"renamed_subject": True},
        }

    return None


def _quiet_switch(state: dict, conversation: list, subject_id: str, user_id: str | None = None) -> dict:
    """Switch subject. conversation is the *old* thread list (web) or the only list (CLI).

    Only a session-boundary marker is appended to the old conversation.
    Resume banner + question are returned in the payload; the web gateway
    places them on the *target* thread so topics do not leak into each other.
    """
    active = state["active_session"]
    if active.get("active") and active.get("subject_id") != subject_id:
        T.close_session(state, active.get("resume_instruction"), user_id=user_id)
    T.start_session(state, subject_id, is_resume=True, user_id=user_id)
    conversation.append({
        "role": "system",
        "content": "[session boundary — topic switch or reset]",
    })
    # Remove trailing Hervat/resume that an older version may have left on this thread
    while conversation:
        last = conversation[-1]
        if not isinstance(last, dict):
            break
        role = last.get("role")
        content = str(last.get("content") or "")
        if role == "system" and content.startswith("Hervat:"):
            conversation.pop()
            continue
        if role == "assistant" and content and len(conversation) >= 2:
            prev = conversation[-2]
            if (
                isinstance(prev, dict)
                and prev.get("role") == "system"
                and str(prev.get("content") or "").startswith("Hervat:")
            ):
                conversation.pop()
                continue
        break

    banner = resume_payload(state, subject_id)
    resume_q = banner.get("resume_question")
    if resume_q:
        active = state["active_session"]
        active["last_tutor_question"] = str(resume_q).strip()[:300]
        active["resume_instruction"] = str(resume_q).strip()
        recent = list(active.get("recent_questions") or [])
        if not recent or recent[-1] != active["last_tutor_question"]:
            recent.append(active["last_tutor_question"])
        active["recent_questions"] = recent[-5:]
    T.save_state(state, user_id=user_id)
    return {
        "ok": True,
        "agent": "tutor",
        "kind": "resume",
        "response": resume_q or "",
        "resume": banner,
        "subject_id": subject_id,
        "subjects": subjects_payload(state),
        "summary": state_summary(state),
        "switched_from_thread": True,
    }


def handle_turn(
    message: str,
    state: dict | None = None,
    conversation: list | None = None,
    *,
    agent: str = "tutor",
    save: bool = True,
    stream: bool = False,
    on_response_delta=None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Process one user message (see _handle_turn). `save=False` is honoured on EVERY path: chaos testing found that a quiet
    subject switch still wrote learner_state.json (start_session saves) although the caller asked for no persistence."""
    _sp = getattr(T, "set_persistence", None)
    token = _sp(save) if _sp else None
    try:
        return _handle_turn(message, state, conversation, agent=agent, save=save, stream=stream,
                            on_response_delta=on_response_delta, user_id=user_id)
    finally:
        if token is not None:
            T.reset_persistence(token)


def _handle_turn(
    message: str,
    state: dict | None = None,
    conversation: list | None = None,
    *,
    agent: str = "tutor",
    save: bool = True,
    stream: bool = False,
    on_response_delta=None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """
    Process one user message. Returns a JSON-serializable result.

    kind: help | subjects | state | reviews | reset | new_subject |
          resume | clarify | message | error

    stream: pass through to the LLM client (CLI live print / web SSE).
    on_response_delta: optional callback(partial, delta) for token streaming
    without mutating global T.STREAM or T.ask_model.
    """
    if agent != "tutor":
        return {
            "ok": False,
            "agent": agent,
            "kind": "error",
            "response": f"Agent '{agent}' is not registered yet. Only 'tutor' is available.",
        }

    if state is None:
        state = T.load_state(user_id=user_id)
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
    cmd_result = _handle_command(state, conversation, text, user_id=user_id)
    if cmd_result is not None:
        if save:
            T.save_state(state, user_id=user_id)
        return cmd_result

    # ---- Python-first subject switch (do not rely on LLM routing) ----
    # Only short navigational utterances ("ik wil verder met agents").
    # Long answers that merely mention a topic must NOT become switches —
    # otherwise diagnostic/teaching replies are moved to the lobby.
    active = state.get("active_session") or {}
    topic = T.detect_subject_intent(text)
    if topic and _looks_like_navigation(text):
        matches = T.find_matching_subjects(state, topic, limit=3)
        best_id = None
        if matches and matches[0][1] >= 50:
            best_id = matches[0][0]
        else:
            rid = T.resolve_subject_id(state, topic, title=topic)
            if rid and rid in state.get("subjects", {}):
                best_id = rid
        cur = active.get("subject_id") if active.get("active") else None
        if best_id and best_id != cur:
            # Leave current subject; do not treat as a learning answer
            return _quiet_switch(state, conversation, best_id, user_id=user_id)
        # best_id == cur → already here: fall through and teach / assess


    # ---- Lobby / meta (no active teaching subject) ----
    # Overview questions never need the teaching model path.
    if IP.is_list_subjects_query(text) or text.strip().lower() in (
        "lobby", "/lobby", "overzicht", "help me kiezen", "wat nu",
    ):
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "lobby",
            "response": lobby_overview(state),
            "subjects": subjects_payload(state),
            "summary": state_summary(state),
            "thread_id": "_lobby",
        }

    # Pending goal collection (after /new) — also allow renaming the subject title first
    active = state.get("active_session") or {}
    if active.get("active") and active.get("pending_goal"):
        sid = active.get("subject_id")
        rename_to = None
        try:
            rename_to = T.parse_subject_rename(text)
        except Exception:
            rename_to = None
        # Pure rename: update title, keep asking for goal (do not start teaching yet)
        if rename_to and sid and sid in state.get("subjects", {}):
            # If the whole message is mainly a rename (short / matches pattern), only rename
            only_rename = (
                text.strip().lower().startswith("/rename")
                or len(text.strip()) <= len(rename_to) + 28
                or text.strip().lower().startswith("het onderwerp is")
                or text.strip().lower().startswith("noem ")
                or text.strip().lower().startswith("titel")
                or text.strip().lower().startswith("call it")
                or text.strip().lower().startswith("name it")
            )
            if only_rename:
                new_t = T.rename_subject_title(state, sid, rename_to)
                conversation.append({"role": "user", "content": text})
                reply = (
                    f"Ok, onderwerp heet nu «{new_t or rename_to}». "
                    f"Wat wil je hiermee bereiken?"
                )
                conversation.append({"role": "assistant", "content": reply})
                if save:
                    T.save_state(state, user_id=user_id)
                return {
                    "ok": True,
                    "agent": "tutor",
                    "kind": "message",
                    "response": reply,
                    "subject_id": sid,
                    "subjects": subjects_payload(state),
                    "summary": state_summary(state),
                    "stats": {"renamed_subject": True},
                }
            # Rename + goal in one message: apply both
            T.rename_subject_title(state, sid, rename_to)
        if sid and sid in state.get("subjects", {}):
            state["subjects"][sid]["goal"] = text[:300]
            state["subjects"][sid]["updated_at"] = datetime.now().isoformat()
        active["pending_goal"] = False
        if sid and T.subject_needs_diagnostic(state, sid):
            T.begin_subject_diagnostic(state, sid)
        T.save_state(state, user_id=user_id) if save else None
        # Fall through — learner text unchanged in history

    # Pending subject choice (stay / switch / new) — meta, no teaching
    _act_ch = state.get("active_session") or {}
    _pending = _act_ch.get("pending_subject_choice") if isinstance(_act_ch, dict) else None
    if isinstance(_pending, dict) and _pending.get("options"):
        raw = (text or "").strip().lower()
        opts = _pending.get("options") or {}
        chosen = None
        if raw in opts:
            chosen = opts[raw]
        elif raw in ("1", "blijven", "stay", "hier", "verder", "huidige"):
            chosen = opts.get("1") or {"action": "stay", "subject_id": _pending.get("from_subject")}
        elif raw.startswith("nieuw") or raw in ("new", "3"):
            for k, v in opts.items():
                if v.get("action") == "new":
                    chosen = v
                    break
        elif raw.startswith("wissel") or raw in ("switch", "2"):
            for k, v in opts.items():
                if v.get("action") == "switch":
                    chosen = v
                    break
        if chosen:
            _act_ch.pop("pending_subject_choice", None)
            conversation.append({"role": "user", "content": text})
            if chosen.get("action") == "stay":
                # Optional: set focus mode from proposed title
                try:
                    from knowledge.focus import update_session_focus
                    update_session_focus(state, _pending.get("proposed") or text)
                except Exception:
                    pass
                reply = (
                    f"Ok, we blijven bij «{(state.get('subjects') or {}).get(chosen.get('subject_id'), {}).get('title') or chosen.get('subject_id')}». "
                    "Waar wil je nu op inzoomen?"
                )
                conversation.append({"role": "assistant", "content": reply})
                if save:
                    T.save_state(state, user_id=user_id)
                return {
                    "ok": True,
                    "agent": "tutor",
                    "kind": "message",
                    "response": reply,
                    "subject_id": chosen.get("subject_id"),
                    "subjects": subjects_payload(state),
                    "summary": state_summary(state),
                    "stats": {"subject_choice_resolved": "stay"},
                }
            if chosen.get("action") == "switch" and chosen.get("subject_id"):
                return _quiet_switch(state, conversation, chosen["subject_id"], user_id=user_id)
            if chosen.get("action") == "new" and chosen.get("title"):
                # Fall through by synthesizing /new behaviour
                title = T.short_subject_title(str(chosen["title"]))
                if _act_ch.get("active"):
                    T.close_session(state, _act_ch.get("resume_instruction"), user_id=user_id)
                parent = _pending.get("from_subject")
                subject_id = T.ensure_subject(state, title, title, parent_id=parent if parent in (state.get("subjects") or {}) else None)
                T.start_session(state, subject_id, pending_goal=True, user_id=user_id)
                conversation.append({"role": "system", "content": "[session boundary — topic switch or reset]"})
                reply = (
                    f"Nieuw onderwerp: {title}"
                    + (f" (onder «{(state.get('subjects') or {}).get(parent, {}).get('title') or parent}»)" if parent else "")
                    + ". Klopt die titel, en wat wil je hiermee bereiken?"
                )
                conversation.append({"role": "assistant", "content": reply})
                if save:
                    T.save_state(state, user_id=user_id)
                return {
                    "ok": True,
                    "agent": "tutor",
                    "kind": "new_subject",
                    "response": reply,
                    "subject_id": subject_id,
                    "subjects": subjects_payload(state),
                    "summary": state_summary(state),
                    "routing": {"action": "new_subject", "subject": subject_id},
                    "switched_from_thread": True,
                    "stats": {"subject_choice_resolved": "new"},
                }
        # Unclear answer while pending: re-ask briefly without LLM
        conversation.append({"role": "user", "content": text})
        reply = "Antwoord met 1 (blijven), of het nummer van een optie, of: blijven / wisselen / nieuw."
        conversation.append({"role": "assistant", "content": reply})
        if save:
            T.save_state(state, user_id=user_id)
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "subject_choice",
            "response": reply,
            "subjects": subjects_payload(state),
            "summary": state_summary(state),
        }

    # Meta rename (lobby or any thread): do not start teaching
    try:
        _pair = T.parse_subject_rename_pair(text)
    except Exception:
        _pair = None
    if _pair and _pair[1]:
        _old_hint, _new_title = _pair
        _sid = None
        if _old_hint:
            _sid = T.resolve_subject_for_rename(state, _old_hint)
        else:
            _act = state.get("active_session") or {}
            _sid = _act.get("subject_id") if _act.get("active") else None
            if not _sid:
                # single subject? use it
                _subs = list((state.get("subjects") or {}).keys())
                if len(_subs) == 1:
                    _sid = _subs[0]
        if _sid and _sid in state.get("subjects", {}):
            _old_title = state["subjects"][_sid].get("title") or _sid
            _new_t = T.rename_subject_title(state, _sid, _new_title)
            conversation.append({"role": "user", "content": text})
            _reply = f"Onderwerp «{_old_title}» hernoemd naar «{_new_t or _new_title}»."
            conversation.append({"role": "assistant", "content": _reply})
            if save:
                T.save_state(state, user_id=user_id)
            return {
                "ok": True,
                "agent": "tutor",
                "kind": "message",
                "response": _reply,
                "subject_id": _sid,
                "subjects": subjects_payload(state),
                "summary": state_summary(state),
                "stats": {"renamed_subject": True},
            }
        if _old_hint:
            conversation.append({"role": "user", "content": text})
            _reply = (
                f"Ik vond geen onderwerp dat lijkt op «{_old_hint}». "
                "Zeg /subjects om de lijst te zien, of /rename <oud> -> <nieuw>."
            )
            conversation.append({"role": "assistant", "content": _reply})
            if save:
                T.save_state(state, user_id=user_id)
            return {
                "ok": True,
                "agent": "tutor",
                "kind": "message",
                "response": _reply,
                "subjects": subjects_payload(state),
                "summary": state_summary(state),
            }

    base_prompt = T.load_system_prompt()
    conversation.append({"role": "user", "content": text})

    if active.get("active"):
        state["active_session"]["question_number"] = int(
            state["active_session"].get("question_number") or 0
        ) + 1
        state["active_session"]["last_activity"] = datetime.now().isoformat()
        low = text.lower()
        act = state["active_session"]

        # Lag fix: sticky flags from the *previous* assessment must not steer
        # *this* turn unless the current message still matches those intents.
        if not IP.matches_any(low, getattr(IP, "STUCK_PHRASES", ())):
            act["stuck_signal"] = False
        if not IP.matches_any(low, getattr(IP, "CORRECTION_PHRASES", ())):
            act["learner_correction"] = False
        if (
            not IP.matches_any(low, getattr(IP, "STUCK_PHRASES", ()))
            and not IP.matches_any(low, IP.FULL_TEACH_PHRASES)
            and act.get("last_learner_signal") in ("stuck", "confused_by_tutor", None)
        ):
            act["force_scaffold"] = False

        if IP.matches_any(low, IP.REPEAT_ADVANCE_PHRASES):
            act["must_advance"] = True
            act["repeat_streak"] = int(act.get("repeat_streak") or 0) + 1

        # "dat heb ik net gezegd" / "same question" → tutor loop, not learner stuck
        if IP.matches_any(low, getattr(IP, "REPETITION_FEEDBACK_PHRASES", ())):
            act["repetition_feedback"] = True
            act["must_advance"] = True
            act["force_scaffold"] = False
            act["stuck_signal"] = False
            act["repeat_streak"] = int(act.get("repeat_streak") or 0) + 1
            ot = T.get_open_thread(act)
            last_p = (ot or {}).get("last_probe_type") or "restate"
            act["prefer_probe_type"] = T.next_probe_type(last_p)
            if ot:
                T.mark_gap_progress(
                    act,
                    "demonstrated",
                    probe_type=act["prefer_probe_type"],
                )

        if IP.matches_any(low, IP.SKIP_RESUME_PHRASES):
            act["just_resumed"] = False
            act["resume_instruction"] = None
            act["must_advance"] = True

        # Learner insight / sharp side question → park old quiz, follow their thread
        if IP.matches_any(low, getattr(IP, "INSIGHT_PHRASES", ())):
            act["learner_insight"] = True
            act["must_advance"] = True  # do not re-inject old open_thread_q
            act["force_scaffold"] = False
            act["stuck_signal"] = False
            ot = T.get_open_thread(act)
            if ot and ot.get("status") in ("open", "scaffolded", "hinted"):
                # Park, do not close as demonstrated — may resume later
                act["parked_thread_q"] = ot.get("q")
                ot["status"] = "parked"
                ot["closed_reason"] = "parked_for_insight"

        if IP.matches_any(low, getattr(IP, "CORRECTION_PHRASES", ())):
            act["learner_correction"] = True
            act["force_scaffold"] = True
            act["stuck_signal"] = True
        elif IP.matches_any(low, IP.FULL_TEACH_PHRASES):
            act["force_full_teach"] = True
            act["stuck_signal"] = True
        elif IP.matches_any(low, getattr(IP, "STUCK_PHRASES", ())):
            act["stuck_signal"] = True
            act["force_scaffold"] = True

    # Same-turn classification BEFORE policy (never reuse last turn's intent).
    _a0 = state.get("active_session") or {}
    _a0.pop("turn_intent", None)
    _a0.pop("turn_correctness", None)
    _a0.pop("judge", None)
    _a0.pop("_judge_meta", None)
    _classify_turn(state, conversation, text)

    # Learner-named mode (eco / EV Boost / ...) BEFORE the policy: the FOCUS MODE line used to lag one turn.
    try:
        from knowledge.focus import update_session_focus
        update_session_focus(state, text)
    except Exception:
        pass

    # B1: optional evidence pack (local docs + wiki). Gathered BEFORE the judge: the judge must see the sources, otherwise
    # it can only judge relevance and calls a true statement "incorrect" (pilot_ems_v3: 4 false rejects of sheet-true claims).
    _evidence = {"status": "not_found", "snippets": [], "enabled": False}
    try:
        from knowledge.turn_evidence import gather_turn_evidence
        _evidence = gather_turn_evidence(state, text, tutor_mod=T)
    except Exception:
        logger.exception("turn evidence gather failed")

    _judge_turn(state, conversation, text, evidence=_evidence)

    policy = T.suggest_next_activity(state)
    _ed = _evidence_discipline(_evidence)
    if _ed and isinstance(policy, dict) and policy.get("reason") is not None:
        policy = dict(policy)
        policy["reason"] = f"{policy['reason']} | {_ed}"

    # Hold-and-flush: UI callback only receives text that passed the pre-send gates.
    stream_on = bool(stream) and on_response_delta is not None
    held = []
    raw, result, stats = "", None, {}
    if (policy or {}).get("reframe"):
        _txt = None
        try:
            _txt = _reframe(state, message)
        except Exception:
            logger.exception("reframe failed; falling back to the normal turn")
        _reframe_repeat = False
        if _txt:
            _hist0 = [{"role": m.get("role"), "text": str(m.get("content") or "")} for m in conversation
                      if m.get("role") in ("assistant", "user")]
            _prev0 = _last_tutor_text(conversation)
            _is_rep = lambda s: bool(s) and (G.repeat_gate(s, _hist0).blocked or s.strip() == (_prev0 or "").strip())
            if _is_rep(_txt):
                try:
                    _txt2 = _reframe(state, message, avoid=_prev0)
                except Exception:
                    logger.exception("second reframe failed")
                    _txt2 = None
                if _txt2 and not _is_rep(_txt2):
                    _txt = _txt2
                else:
                    _reframe_repeat = True               # last resort: delivered, but logged
        if _txt:
            result = {"routing": {"action": "continue"}, "response": _txt, "assessment": {}, "session": {}}
            stats = {"reframe": "policy", "streamed": False}
            if _reframe_repeat:
                stats["reframe_repeat"] = True
    try:
        messages = T.build_messages(base_prompt, state, conversation, policy=policy)
        try:
            from knowledge.turn_evidence import inject_evidence_into_messages
            messages = inject_evidence_into_messages(messages, _evidence)
        except Exception:
            logger.exception("evidence inject failed")
        if result is None:
            res_main = llm_call(
                "main",
                messages,
                stream=bool(stream),
                on_response_delta=(lambda p, d: held.append((p, d))) if stream_on else None,
            )
            raw, result, stats = res_main.raw, res_main.parsed, res_main.stats
    except Exception as e:
        # Keep the user line on the conversation so a failed turn is still visible
        # after reload (assistant may be missing; better than silent history loss).
        if save:
            try:
                T.save_state(state, user_id=user_id)
            except Exception:
                pass
        return {
            "ok": False,
            "agent": "tutor",
            "kind": "error",
            "response": f"Model error: {type(e).__name__}: {e}",
            "stats": {},
            "summary": state_summary(state),
        }

    stats = dict(stats or {})
    streamed_response = False  # set True only after approved flush
    stats["streamed"] = False

    if not isinstance(result, dict) or not result:
        # Never surface raw model JSON to the learner (truncated output looked like a
        # "routing error" in the web UI). Prefer a partial response field if any.
        safe = ""
        try:
            safe = (T.extract_partial_response_field(raw) or "").strip()
        except Exception:
            safe = ""
        if not safe:
            safe = (
                "Ik kon dit antwoord niet netjes afronden (te lange model-output). "
                "Stuur je laatste bericht gerust opnieuw, of typ /state."
            )
        conversation.append({"role": "assistant", "content": safe})
        if save:
            T.save_state(state, user_id=user_id)
        return {
            "ok": False,
            "agent": "tutor",
            "kind": "message",
            "response": safe,
            "raw_preview": (raw or "")[:200],
            "stats": stats,
            "summary": state_summary(state),
        }

    answer = str(result.get("response") or "").strip()
    assessment = _as_dict(result.get("assessment"))
    session_info = _as_dict(result.get("session"))
    routing = _as_dict(result.get("routing"))
    action = str((routing or {}).get("action") or "continue").lower()
    if action not in ("continue", "clarify", "list_subjects", "switch", "new_subject"):
        # model output is untrusted: an unknown action used to skip every gate and the hold-and-flush yet still deliver a
        # normal reply (chaos: action "bogus"); treat it as a plain turn
        action = "continue"

    _orig_draft = answer
    _concede = bool((policy or {}).get("learner_correction")) or (
        (state.get("active_session") or {}).get("turn_intent") == "challenges_tutor")

    _echo_hit = bool(answer) and action in ("continue", "clarify", "") and getattr(
        T, "response_looks_like_echo_teach", lambda *_: False)(answer)
    if _echo_hit:
        stats = dict(stats or {})
        stats["echo_flagged"] = True             # logged even when the repair is off -> audit the heuristic
        if not ECHO_REPAIR and not _concede:
            stats["echo_skipped"] = "disabled"
    # Soft echo-teach repair (secondary; gate below is the primary anti-repeat).
    if _concede and answer and getattr(T, "response_looks_like_echo_teach", lambda *_: False)(answer):
        stats = dict(stats or {})
        stats["echo_skipped"] = "concede"        # the policy says concede; a rewrite would undo that
    if (
        not _concede
        and ECHO_REPAIR
        and not (stats or {}).get("reframe")          # a policy reframe is not an explain-then-restate turn
        and action in ("continue", "clarify", "")
        and answer
        and getattr(T, "response_looks_like_echo_teach", lambda *_: False)(answer)
    ):
        try:
            repaired, repair_stats = _repair_echo_teach_turn(
                messages, result, answer, policy=policy, context=_repair_context(state, policy)
            )
            if repaired:
                answer = repaired
                result["response"] = repaired
                stats = dict(stats or {})
                stats["echo_repair"] = True
                if repair_stats:
                    stats["echo_repair_s"] = repair_stats.get("total_s")
        except Exception:
            stats = dict(stats or {})
            stats["echo_repair"] = False
            stats["echo_repair_failed"] = True

    # ---- pre-send gates: repeat / template_cap / leak (deterministic) -> ONE repair ----
    _draft = answer
    if action in ("continue", "clarify", "") and answer and not (stats or {}).get("reframe"):
        hist = [
            {"role": m.get("role"), "text": str(m.get("content") or "")}
            for m in conversation
            if m.get("role") in ("assistant", "user")
        ]
        gate = G.repeat_gate(answer, hist)
        if not gate.blocked:
            _xg = _extra_gate(answer, hist, state)
            if _xg is not None:
                gate = _xg
        if not gate.blocked and isinstance(assessment, dict):
            _gc = assessment.get("gap_card") if isinstance(assessment.get("gap_card"), dict) else {}
            _exp = (_gc or {}).get("expected_answer")
            if _exp:
                _prior = [h["text"] for h in hist if h["role"] == "assistant"]
                _lk = G.leak_check(
                    str(_exp),
                    _prior,
                    candidate_pre_question=" ".join(G.split_sentences(answer)[:-1]),
                )
                if _lk.leaked:
                    gate = G.GateResult(True, "leak", _lk.coverage)
        _dup = gate.blocked and gate.reason in ("repeat", "identical_question", "identical_reply", "learner_echo") and float(gate.score or 0) >= 0.95
        if gate.blocked and _concede and _dup:
            # A bare repeat on a turn where the learner corrected us is certainly wrong (pilot_ems_v3 turn 8: byte-identical
            # reply to a challenge). The generic repair is skipped on concede turns, so use the concede-specific small call.
            stats = dict(stats or {})
            _txt = None
            try:
                _txt = _concede_reply(state, hist, message, policy)
            except Exception:
                logger.exception("concede reply failed")
            if _txt and not G.repeat_gate(_txt, hist).blocked and _extra_gate(_txt, hist, state) is None:
                answer = _txt
                result["response"] = _txt
                stats["gate_repair"] = "concede_duplicate"
            else:
                stats["gate_skipped"] = "concede"
                stats["concede_repair_failed"] = True       # last resort: the draft goes out, but it is logged
        elif gate.blocked and _concede:
            stats = dict(stats or {})
            stats["gate_skipped"] = "concede"
        elif gate.blocked:
            _act = state.get("active_session") or {}
            _ot = T.get_open_thread(_act)
            _ladder = ((_ot or {}).get("gap_card") or {}).get("scaffold_ladder") or []
            _target = _act.get("parked_thread_q") or (_ladder[0] if _ladder else None)
            new_ans = None
            try:
                new_ans = _repair_repeat(messages, G.final_question(answer), _target,
                                         context=_repair_context(state, policy) + _gate_hint(gate, state))
            except Exception:
                logger.exception("repeat repair failed")
            stats = dict(stats or {})
            if new_ans and not G.repeat_gate(new_ans, hist).blocked and _extra_gate(new_ans, hist, state) is None:
                answer = new_ans
                result["response"] = new_ans
                stats["gate_repair"] = gate.reason
            else:
                stats["gate_repair_failed"] = gate.reason
                # A second repair would hit the same attractor. Break the loop with a small,
                # different move instead of delivering the duplicate we just detected.
                _txt = None
                try:
                    _txt = _reframe(state, message, avoid=_last_tutor_text(conversation), kind="loop")
                except Exception:
                    logger.exception("reframe after failed repair also failed")
                if _txt:
                    answer = _txt
                    result["response"] = _txt
                    stats["reframe"] = "loop"
                else:
                    stats["reframe_failed"] = True   # last resort: the draft goes out, but it is logged

    if answer != _orig_draft:                      # observability: keep the discarded draft for the event log
        stats = dict(stats or {})
        stats["draft"] = _orig_draft[:1500]
        stats["draft_question"] = G.final_question(_orig_draft)

    _note_probe(state, answer, action)

    # State must describe what the learner actually SAW, not the discarded draft.
    if answer != _draft:
        session_info = dict(session_info or {})
        session_info["last_question"] = G.final_question(answer) or ""
        session_info.pop("premises", None)
        if isinstance(assessment, dict):
            # Keep first-call assessment scores/verdict; drop draft gap_card only.
            assessment = dict(assessment)
            assessment.pop("gap_card", None)

    # Flush approved text to the UI in one go (hold-and-flush).
    if stream_on and answer and action in ("continue", "clarify", ""):
        try:
            on_response_delta(answer, answer)
        except Exception:
            logger.exception("on_response_delta flush failed")
        stats = dict(stats or {})
        stats["streamed"] = True
        streamed_response = True

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
            T.save_state(state, user_id=user_id)
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
            T.save_state(state, user_id=user_id)
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
                T.save_state(state, user_id=user_id)
            return {
                "ok": True,
                "agent": "tutor",
                "kind": "subjects",
                "response": f"Onderwerp \"{raw_id}\" niet gevonden.",
                "subjects": subjects_payload(state),
                "stats": stats,
            }
        # Drop the user message from teaching conversation after switch
        return _quiet_switch(state, conversation, best_id, user_id=user_id)

    if action == "new_subject":
        # Mid-session: sub-topics (e.g. EV Boost within EMS) stay on the current subject
        # unless the learner clearly asked for a separate new subject (/new, "nieuw onderwerp").
        _act_now = state.get("active_session") or {}
        _msg_l = (text or "").lower()
        _explicit_new = (
            _msg_l.startswith("/new")
            or "nieuw onderwerp" in _msg_l
            or "andere subject" in _msg_l
            or "ander onderwerp" in _msg_l
            or "start a new subject" in _msg_l
        )
        if _act_now.get("active") and _act_now.get("subject_id") and not _explicit_new:
            # Offer an explicit choice instead of silently creating a subject or only continuing
            cur_sid = _act_now.get("subject_id")
            cur_title = (state.get("subjects") or {}).get(cur_sid, {}).get("title") or cur_sid
            proposed = (
                (routing.get("proposed_title") or routing.get("subject") or "")
                if isinstance(routing, dict) else ""
            )
            proposed = T.short_subject_title(str(proposed).strip()) if proposed else ""
            matches = []
            if proposed:
                try:
                    matches = T.find_matching_subjects(state, proposed)[:3]
                except Exception:
                    matches = []
            lines = [
                f"Je lijkt een (sub)onderwerp aan te snijden"
                + (f" («{proposed}»)" if proposed else "")
                + f" terwijl je in «{cur_title}» zit.",
                "",
                "Wat wil je?",
                f"  1) Verder onder «{cur_title}» (aanbevolen als het een mode/onderdeel is)",
            ]
            opt_map = {"1": {"action": "stay", "subject_id": cur_sid}}
            n = 2
            for mid, score in matches:
                if mid == cur_sid:
                    continue
                mt = (state.get("subjects") or {}).get(mid, {}).get("title") or mid
                lines.append(f"  {n}) Wisselen naar bestaand «{mt}»")
                opt_map[str(n)] = {"action": "switch", "subject_id": mid}
                n += 1
                if n > 4:
                    break
            if proposed and proposed.lower() != str(cur_title).lower():
                lines.append(f"  {n}) Nieuw onderwerp «{proposed}» starten")
                opt_map[str(n)] = {"action": "new", "title": proposed}
            lines.append("")
            lines.append("Antwoord met 1, 2, … of kort: blijven / wisselen / nieuw.")
            choice_text = "\n".join(lines)
            _act_now["pending_subject_choice"] = {
                "options": opt_map,
                "proposed": proposed,
                "from_subject": cur_sid,
            }
            conversation.append({"role": "assistant", "content": choice_text})
            if save:
                T.save_state(state, user_id=user_id)
            return {
                "ok": True,
                "agent": "tutor",
                "kind": "subject_choice",
                "response": choice_text,
                "subject_id": cur_sid,
                "subjects": subjects_payload(state),
                "summary": state_summary(state),
                "stats": {**(stats or {}), "subject_choice": True},
            }

    if action == "new_subject":
        title = routing.get("proposed_title") or routing.get("subject")
        if not title or len(str(title).strip()) > 60:
            if save:
                T.save_state(state, user_id=user_id)
            return {
                "ok": False,
                "agent": "tutor",
                "kind": "error",
                "response": "Could not create subject (title missing or too long).",
                "stats": stats,
            }
        title = T.short_subject_title(str(title).strip())
        if active.get("active"):
            T.close_session(state, active.get("resume_instruction"), user_id=user_id)
        tax = routing.get("taxonomy") if isinstance(routing.get("taxonomy"), dict) else {}
        parent_id = tax.get("parent_id") or routing.get("parent_id")
        if parent_id:
            parent_id = T.resolve_subject_id(state, parent_id, title=str(parent_id)) or T.normalize_id(parent_id)
            if parent_id not in state.get("subjects", {}):
                parent_id = None
        subject_id = T.ensure_subject(state, title, title, parent_id=parent_id)
        T.apply_taxonomy_proposals(state, routing, focus_subject_id=subject_id)
        T.start_session(state, subject_id, pending_goal=True, user_id=user_id)
        # Keep UI history; model still only sees last N messages.
        conversation.append({
            "role": "system",
            "content": "[session boundary — topic switch or reset]",
        })
        if save:
            T.save_state(state, user_id=user_id)
        return {
            "ok": True,
            "agent": "tutor",
            "kind": "new_subject",
            "response": f"Nieuw onderwerp: {title}. Klopt die titel (zeg bv. 'het onderwerp is EMS' om te hernoemen), en wat wil je hiermee bereiken?",
            "subject_id": subject_id,
            "subjects": subjects_payload(state),
            "stats": stats,
            "summary": state_summary(state),
            "routing": {"action": "new_subject", "subject": subject_id},
            "switched_from_thread": True,
        }

    # ---- continue: assessment ----
    if not state["active_session"].get("active"):
        sid = None
        if isinstance(assessment, dict):
            sid = assessment.get("subject")
        if sid and T.is_valid_id(sid):
            T.start_session(
                state,
                T.ensure_subject(state, sid, sid),
                user_id=user_id,
            )

    # Verdict authority: the JUDGE (impartial, sees the premises) beats the tutor's own verdict. A misconception
    # is only created after a CONFIRMED error; this also closes the old hole where a PARTIAL verdict plus a
    # proposed misconception bypassed every check (the unverified key "temperature" became a misconception
    # although the premises fixed the temperature).
    _act = state.get("active_session") or {}
    _meta = _act.pop("_judge_meta", None) or {}
    _j = _act.get("judge") if isinstance(_act.get("judge"), dict) else None
    stats = dict(stats or {})
    stats["judge_called"] = bool(_meta.get("called"))
    if _meta.get("style"):
        stats["judge_style"] = _meta["style"]
    if _meta.get("truncated"):
        stats["judge_truncated"] = True            # the token budget ran out before the JSON: visible, not silent
    if _meta.get("retry"):
        stats["judge_retry"] = True
    if _meta.get("failed"):
        stats["judge_failed"] = True
    if _meta.get("evidence"):
        stats["judge_evidence"] = _meta["evidence"]
    if int(_act.get("off_target_streak") or 0):
        stats["off_target_streak"] = int(_act["off_target_streak"])
    if _j:
        stats["judge"] = dict(_j)
        if _j.get("downgraded"):
            stats["judge_downgraded"] = True          # a true/false claim without a verifiable quote -> unverified
    if isinstance(assessment, dict):
        _verdict = str(assessment.get("verdict") or "").lower()
        _mc_ok = False
        if _j:
            assessment = dict(assessment)
            assessment["verdict"] = {"correct": "correct", "partial": "partial", "incorrect": "incorrect",
                                     "uncertain": "partial", "off_target": "partial"}[_j["verdict"]]
            if _j["verdict"] in ("uncertain", "off_target"):
                assessment["_skip_fsrs"] = True          # no evidence on the probe: neither credit nor penalty
            if _j["verdict"] == "uncertain":
                _act["soft_incorrect"] = True
            _mc_ok = _j["verdict"] == "incorrect"
        else:
            _ot = T.get_open_thread(_act)
            _gc = (_ot or {}).get("gap_card") if _ot else None
            if not isinstance(_gc, dict):
                _gc = assessment.get("gap_card") if isinstance(assessment.get("gap_card"), dict) else {}
            _status = (_gc or {}).get("status") if isinstance(_gc, dict) else "model_only"
            if _status not in (None, "model_only", "disputed"):
                _mc_ok = True                                  # key verified by a source / a human
            elif _verdict in ("incorrect", "wrong", "false"):
                _prem = ((_ot or {}).get("premises") or (session_info or {}).get("premises") or [])
                _key = ((_gc or {}).get("key") if isinstance(_gc, dict) else None)
                _ev = _evaluate_correction(_prem, text, _key)
                stats["correction_gate"] = _ev
                _mc_ok = _ev == "incorrect"
                if _ev != "incorrect":
                    assessment = dict(assessment)
                    assessment["verdict"] = "partial"
                    assessment["_skip_fsrs"] = True
                    _act["soft_incorrect"] = True
        if not _mc_ok and assessment.get("misconceptions"):
            assessment = dict(assessment)
            assessment["misconceptions"] = []

    try:
        T.apply_assessment(state, assessment)
    except Exception:
        logger.exception("apply_assessment failed; learner state may not have been updated this turn")

    if not isinstance(session_info, dict):
        session_info = {}
    # Policy wins for close/full-teach so open gaps don't stick after resolution
    if policy.get("close_thread"):
        session_info["close_thread"] = True
        session_info["thread_resolved"] = True
    if policy.get("full_teach"):
        session_info["close_thread"] = True
        session_info["thread_resolved"] = True

    ri = session_info.get("resume_instruction")
    if ri and T.is_learner_facing_resume(str(ri)):
        state["active_session"]["resume_instruction"] = str(ri).strip()

    if not answer:
        # The model returned an EMPTY reply. This used to become a hardcoded Dutch sentence AFTER the gates: language-bound, the
        # most banned question ("explain in your own words"), and identical every time. Ask once, briefly; else re-ask the open
        # question (language-correct, it came from the model); the static sentence is the last resort and is logged.
        stats = dict(stats or {})
        _t = None
        try:
            _t = _empty_reply(state, message, policy)
        except Exception:
            logger.exception("empty-reply retry failed")
        if _t:
            answer, stats["empty_fallback"] = _t, "model"
        else:
            _q = str((T.get_open_thread(state.get("active_session") or {}) or {}).get("q") or "").strip()
            if _q:
                answer, stats["empty_fallback"] = _q, "reask"
            else:
                answer, stats["empty_fallback"] = "Kun je dat in eigen woorden toelichten?", "static"
        result["response"] = answer

    T.update_session_memory(state, session_info, answer)
    state["active_session"]["just_resumed"] = False
    state["active_session"]["last_activity"] = datetime.now().isoformat()
    T.rebuild_review_queue(state)

    conversation.append({"role": "assistant", "content": answer})
    # Do NOT truncate the shared conversation list here.
    # MAX_CONVERSATION_MESSAGES only applies when building the LLM prompt
    # (see tutor.build_messages). UI / web threads keep a longer history
    # (gateway MAX_CONV_MESSAGES). Truncating here made chats vanish on refresh.

    if save:
        T.save_state(state, user_id=user_id)

    # Compact assessment for event log / UI (not full score dict)
    assess_out = {}
    if isinstance(assessment, dict):
        for k in ("activity", "verdict", "last_verdict", "subject", "concept"):
            if assessment.get(k) is not None:
                assess_out[k] = assessment.get(k)

    _act_out = state.get("active_session") or {}
    _ot_out = T.get_open_thread(_act_out) if hasattr(T, "get_open_thread") else None
    _prem_out = None
    if isinstance(_ot_out, dict) and _ot_out.get("premises"):
        _prem_out = list(_ot_out.get("premises") or [])[:6]
    stats = dict(stats or {})
    if _act_out.get("turn_intent"):
        stats.setdefault("turn_intent", _act_out.get("turn_intent"))
    if _act_out.get("turn_correctness"):
        stats.setdefault("turn_correctness", _act_out.get("turn_correctness"))

    # B1: learner-visible sources (Python-owned footer; model must not invent these)
    _sources = []
    try:
        from knowledge.turn_evidence import sources_payload, format_sources_footer
        if _evidence.get("status") in ("found", "weak") and _evidence.get("snippets"):
            _sources = sources_payload(_evidence)
            stats["evidence_status"] = _evidence.get("status")
            stats["evidence_query"] = (_evidence.get("query") or "")[:200]
            stats["evidence_wiki_ok"] = bool(_evidence.get("wiki_ok"))
            stats["evidence_local_ok"] = bool(_evidence.get("local_ok"))
            foot = format_sources_footer(_evidence)
            if foot and answer and "Bronnen:" not in answer:
                answer = answer.rstrip() + foot
            if _sources and conversation:
                last = conversation[-1]
                if isinstance(last, dict) and last.get("role") == "assistant":
                    last["sources"] = list(_sources)
                    import re as _re
                    body = str(last.get("content") or "")
                    body = _re.sub(r"\n*—\s*\nBronnen:[\s\S]*$", "", body).strip()
                    last["content"] = body
    except Exception:
        logger.exception("evidence footer failed")

    return {
        "ok": True,
        "agent": "tutor",
        "kind": "message",
        "response": answer,
        "sources": _sources,
        "routing": {"action": action},
        "policy": {
            "activity": policy.get("activity"),
            "force": policy.get("force"),
            "reason": policy.get("reason"),
            "concept_id": policy.get("concept_id"),
            "scaffold": bool(policy.get("scaffold")),
            "full_teach": bool(policy.get("full_teach")),
        },
        "assessment": assess_out,
        "turn_intent": _act_out.get("turn_intent"),
        "turn_correctness": _act_out.get("turn_correctness"),
        "premises": _prem_out,
        "stats": stats,
        "streamed_response": streamed_response,
        "summary": state_summary(state),
    }


def bootstrap(user_id: str | None = None) -> dict:
    """Load state for user_id and return initial payload for the UI."""
    state = T.load_state(user_id=user_id)
    return {
        "ok": True,
        "agent": "tutor",
        "model": T.MODEL_NAME,
        "summary": state_summary(state),
        "subjects": subjects_payload(state),
        "user_id": user_id,
    }
