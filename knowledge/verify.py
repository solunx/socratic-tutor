"""Combine Wikipedia + local docs into a short evidence pack.

No topic hardcoding. Offline / wiki-down: local only, or empty pack — never raises.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from knowledge import local_docs, wiki

logger = logging.getLogger(__name__)

# Env: TUTOR_EVIDENCE_WIKI=0 forces local-only (or tests without network)
def _wiki_enabled_default() -> bool:
    return os.environ.get("TUTOR_EVIDENCE_WIKI", "1").strip() != "0"


def evidence_for(
    claim: str,
    *,
    max_snippets: int = 2,
    wiki_chars: int = 900,
    local_chars: int = 700,
    use_wiki: bool | None = None,
    use_local: bool = True,
    min_local_score: float = 2.5,
    path_boosts: list[str] | None = None,
) -> dict[str, Any]:
    """
    Build an evidence pack for a claim / topic string (any subject).

    Returns:
      {
        "query": str,
        "status": "found" | "weak" | "not_found",
        "snippets": [...],
        "wiki_ok": bool,       # False if wiki skipped or network failed
        "local_ok": bool,
      }

    Policy for the tutor loop (B1+):
      - wiki down / disabled → still return local hits if any
      - nothing found → status not_found; caller continues model_only
      - never raises into handle_turn
    """
    claim = (claim or "").strip()
    snippets: list[dict[str, Any]] = []
    wiki_ok = False
    local_ok = False

    if use_wiki is None:
        use_wiki = _wiki_enabled_default()

    # --- local first (always available offline) ---
    if use_local and claim:
        try:
            hits = local_docs.search_local(claim, limit=max_snippets, snippet_chars=local_chars, path_boosts=path_boosts)
            for hit in hits:
                if float(hit.get("score") or 0) < min_local_score:
                    continue
                local_ok = True
                snippets.append({
                    "source": hit["source"],
                    "title": hit.get("title") or hit["source"],
                    "text": hit.get("text") or "",
                    "url": hit.get("url"),
                    "kind": "local",
                    "score": hit.get("score"),
                })
        except Exception:
            logger.exception("local_docs search failed")

    # --- wikipedia (best-effort; failures do not abort) ---
    if use_wiki and claim:
        try:
            # One ranked multi-hit path: search → filter → extract top titles
            wiki_snips: list[dict[str, Any]] = []
            seen_sources: set[str] = set()

            def _add_from_lang(lang: str) -> None:
                nonlocal wiki_ok
                hits = wiki.search_hits(claim, lang=lang, limit=8)
                if hits is not None:
                    # search_hits returns [] on network fail OR no results;
                    # distinguish by a tiny siteinfo only if we got nothing and no local
                    pass
                for h in hits:
                    if len(wiki_snips) >= 2:
                        break
                    if h.get("score", 0) <= 0:
                        continue
                    ext = wiki.fetch_extract(h["title"], lang=lang, chars=wiki_chars)
                    if not ext:
                        continue
                    wiki_ok = True
                    if ext["source"] in seen_sources:
                        continue
                    seen_sources.add(ext["source"])
                    wiki_snips.append({
                        "source": ext["source"],
                        "title": ext["title"],
                        "text": ext["text"],
                        "url": ext.get("url"),
                        "kind": "wikipedia",
                        "score": h.get("score"),
                    })

            _add_from_lang("nl")
            if len(wiki_snips) < 1:
                _add_from_lang("en")

            # If search returned nothing useful, try exact-title extract once
            if not wiki_snips:
                for lang in ("nl", "en"):
                    ext = wiki.fetch_extract(claim[:200], lang=lang, chars=wiki_chars)
                    if ext and wiki.title_relevance(claim, ext["title"]) > 0:
                        wiki_ok = True
                        wiki_snips.append({
                            "source": ext["source"],
                            "title": ext["title"],
                            "text": ext["text"],
                            "url": ext.get("url"),
                            "kind": "wikipedia",
                        })
                        break

            snippets.extend(wiki_snips)
        except Exception:
            logger.exception("wikipedia evidence failed (continuing with local only)")
            wiki_ok = False

    # Prefer local, then wiki; cap total
    local_s = [s for s in snippets if s.get("kind") == "local"]
    wiki_s = [s for s in snippets if s.get("kind") == "wikipedia"]
    ordered = local_s + wiki_s
    ordered = ordered[: max(1, max_snippets)] if ordered else []

    if not ordered:
        status = "not_found"
    elif local_s or len(ordered) >= 2:
        status = "found"
    else:
        status = "weak"

    return {
        "query": claim,
        "status": status,
        "snippets": ordered,
        "wiki_ok": wiki_ok,
        "local_ok": local_ok,
    }


def format_evidence_for_prompt(pack: dict[str, Any], *, max_chars: int = 1800) -> str:
    """Compact block for a future policy/prompt injection (optional)."""
    if not pack or not pack.get("snippets"):
        return ""
    lines = [
        f"SOURCE EVIDENCE (status={pack.get('status')}, "
        f"wiki_ok={pack.get('wiki_ok')}, local_ok={pack.get('local_ok')}, "
        f"query={pack.get('query')!r}):"
    ]
    budget = max_chars
    for s in pack["snippets"]:
        head = f"- [{s.get('kind')}] {s.get('title')} ({s.get('source')})"
        if s.get("url"):
            head += f" {s['url']}"
        body = (s.get("text") or "").strip().replace("\n", " ")
        chunk = f"{head}\n  {body}"
        if len(chunk) > budget:
            chunk = chunk[: budget - 20] + "…"
        lines.append(chunk)
        budget -= len(chunk)
        if budget < 100:
            break
    return "\n".join(lines)
