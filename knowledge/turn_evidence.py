"""B1 — gather evidence for the current turn (any subject, no topic tables).

Used by core.handle_turn: inject into the system prompt and attach sources
to the API response / optional response footer.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

_WORD = re.compile(r"[a-zA-Zà-öø-ÿÀ-ÖØ-ß0-9]{2,}")


def evidence_enabled() -> bool:
    return os.environ.get("TUTOR_EVIDENCE", "1").strip() != "0"


def query_for_turn(state: dict | None, learner_text: str, *, tutor_mod=None) -> str:
    """Build a generic retrieval query from open gap + concept + learner message."""
    parts: list[str] = []
    act = (state or {}).get("active_session") or {}
    try:
        get_ot = getattr(tutor_mod, "get_open_thread", None) if tutor_mod else None
        ot = get_ot(act) if callable(get_ot) else None
        if not isinstance(ot, dict):
            threads = act.get("open_threads") or []
            ot = next((t for t in threads if isinstance(t, dict) and t.get("status") != "closed"), None)
        if isinstance(ot, dict):
            q = str(ot.get("q") or "").strip()
            if q:
                parts.append(q[:220])
            gc = ot.get("gap_card") if isinstance(ot.get("gap_card"), dict) else {}
            for key in ("target", "key", "expected_answer"):
                val = str((gc or {}).get(key) or "").strip()
                if val:
                    parts.append(val[:160])
            for p in (ot.get("premises") or [])[:3]:
                if str(p).strip():
                    parts.append(str(p).strip()[:120])
    except Exception:
        logger.exception("query_for_turn open_thread failed")

    cid = str(act.get("current_concept_id") or "").replace("_", " ").strip()
    if cid:
        parts.append(cid)
    sid = str(act.get("subject_id") or "").replace("_", " ").strip()
    if sid and sid not in (cid or ""):
        parts.append(sid)

    msg = (learner_text or "").strip()
    if msg:
        parts.append(msg[:280])

    fm = act.get("focus_mode") if isinstance(act.get("focus_mode"), dict) else None
    if fm and fm.get("label"):
        parts.insert(0, str(fm["label"]))

    # de-dupe similar chunks
    seen: set[str] = set()
    uniq: list[str] = []
    for p in parts:
        k = p.lower()[:80]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)
    return " ".join(uniq)[:450].strip()


def gather_turn_evidence(state: dict | None, learner_text: str, *, tutor_mod=None) -> dict[str, Any]:
    """
    Returns evidence pack + meta. Never raises.
    Empty pack when disabled or nothing found — tutor continues as before.
    """
    empty = {
        "query": "",
        "status": "not_found",
        "snippets": [],
        "wiki_ok": False,
        "local_ok": False,
        "enabled": evidence_enabled(),
    }
    if not evidence_enabled():
        return empty
    try:
        from knowledge.verify import evidence_for

        q = query_for_turn(state, learner_text, tutor_mod=tutor_mod)
        if not q:
            return empty
        try:
            from knowledge.focus import path_boost_tokens
            boosts = path_boost_tokens(state, learner_text)
        except Exception:
            boosts = []
        pack = evidence_for(q, max_snippets=2, path_boosts=boosts or None)
        pack["enabled"] = True
        pack["query"] = q
        pack["path_boosts"] = boosts
        return pack
    except Exception:
        logger.exception("gather_turn_evidence failed")
        return empty


def sources_payload(pack: dict[str, Any] | None) -> list[dict[str, Any]]:
    """UI-friendly source list (only real retrieval hits).

    Multiple chunks from the same document collapse to one footer line
    (dedupe by source / local path). Quotes from extra chunks are dropped
    from the UI list; the prompt still receives all snippets.
    """
    if not pack or not pack.get("snippets"):
        return []
    out = []
    seen: set[str] = set()
    for s in pack["snippets"][:6]:
        src = (s.get("source") or "").strip()
        # Dedupe key: local path or full source id
        if src.startswith("local:"):
            key = "local:" + src[len("local:"):].lstrip("/").lower()
        elif src:
            key = src.lower()
        else:
            key = (s.get("url") or s.get("title") or str(id(s))).lower()
        if key in seen:
            continue
        seen.add(key)

        quote = re.sub(r"\s+", " ", str(s.get("text") or "")).strip()[:220]
        url = s.get("url")
        rel = None
        if src.startswith("local:"):
            rel = src[len("local:"):].lstrip("/")
            if not url:
                url = "/api/v1/knowledge-doc?path=" + rel
        if rel:
            label = rel.split("/")[-1]
        else:
            label = s.get("title") or s.get("source")
        out.append({
            "kind": s.get("kind") or "unknown",
            "title": label,
            "source": s.get("source"),
            "url": url,
            "quote": quote or None,
        })
        if len(out) >= 4:
            break
    return out


def format_sources_footer(pack: dict[str, Any] | None) -> str:
    """Learner-visible attribution block (Python-owned, not model-written)."""
    src = sources_payload(pack)
    if not src:
        return ""
    lines = ["", "—", "Bronnen:"]
    for s in src:
        label = s.get("title") or s.get("source") or "bron"
        kind = s.get("kind") or ""
        line = f"• [{kind}] {label}"
        if s.get("url"):
            line += f" — {s['url']}"
        elif s.get("source"):
            line += f" ({s['source']})"
        lines.append(line)
    return "\n".join(lines)


_INJECT_HEADER = (
    "SOURCE EVIDENCE (retrieved by the system for THIS turn; prefer these over "
    "generic memory when they apply; do not invent other sources or URLs; if the "
    "learner asks about this material, ground the answer in these snippets; if "
    "something is not covered, say so briefly):\n"
)


def inject_evidence_into_messages(messages: list, pack: dict[str, Any] | None) -> list:
    """Append evidence block to the system message. Returns same list (mutated)."""
    if not messages or not pack or pack.get("status") == "not_found":
        return messages
    try:
        from knowledge.verify import format_evidence_for_prompt

        block = format_evidence_for_prompt(pack, max_chars=1600)
        if not block:
            return messages
        # format_evidence already has a header; prepend instruction once
        extra = _INJECT_HEADER + block.split("\n", 1)[-1] if block.startswith("SOURCE") else _INJECT_HEADER + block
        # simpler: use full format_evidence + short instruction
        extra = (
            "\n\nSOURCE EVIDENCE (system-retrieved; prefer over generic memory when relevant; "
            "never invent sources/URLs; if not covered, say so):\n"
            + block
        )
        if messages[0].get("role") == "system":
            messages[0] = dict(messages[0])
            messages[0]["content"] = str(messages[0].get("content") or "") + extra
    except Exception:
        logger.exception("inject_evidence_into_messages failed")
    return messages
