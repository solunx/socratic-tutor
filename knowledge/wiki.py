"""Wikipedia summary fetch (nl first, en fallback). No topic hardcoding.

Network failures return empty results — callers keep working on local docs only.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

_API = "https://{lang}.wikipedia.org/w/api.php"
_USER_AGENT = "SocraticTutor/0.1 (local educational tool; contact: local-admin)"
_TIMEOUT = 8

# Structural title noise (language-agnostic patterns) — NOT subject lists
_TITLE_NOISE = re.compile(
    r"(?i)\b("
    r"lijst van|list of|disambiguation|doorverwijspagina|"
    r"afkortingen|abbreviations|glossary|index \(|"
    r"film\b|album\b|lied\b|single\b|televisie|tv[- ]serie|"
    r"voetbal|football club|\(doorverwijzing\)"
    r")\b"
)

_WORD = re.compile(r"[a-zA-Zà-öø-ÿÀ-ÖØ-ß0-9]{2,}")


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _WORD.finditer(text or "")}


def _get(url: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """GET JSON or None on any network/HTTP/parse failure (offline-safe)."""
    try:
        import requests

        r = requests.get(
            url,
            params=params,
            headers={"User-Agent": _USER_AGENT},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.info("wikipedia request failed: %s", e)
        return None


def _normalize_title(q: str) -> str:
    q = (q or "").strip()
    q = re.sub(r"\s+", " ", q)
    return q[:200]


def title_looks_useful(title: str) -> bool:
    """Reject list/disambiguation/entertainment-style titles without topic lists."""
    t = (title or "").strip()
    if not t or len(t) < 2:
        return False
    if _TITLE_NOISE.search(t):
        return False
    return True


def title_relevance(query: str, title: str, snippet: str = "") -> float:
    """Overlap score between query tokens and title (+ light snippet boost)."""
    q = _tokens(query)
    if not q:
        return 0.0
    t = _tokens(title)
    s = _tokens(snippet) if snippet else set()
    if not t:
        return 0.0
    inter_t = len(q & t)
    inter_s = len(q & s)
    # require at least one title token overlap for non-trivial queries
    if inter_t == 0 and len(q) >= 2:
        return 0.0
    return inter_t * 2.0 + inter_s * 0.25 + inter_t / max(len(q), 1)


def search_hits(query: str, *, lang: str = "nl", limit: int = 8) -> list[dict[str, Any]]:
    """
    Raw MediaWiki search hits: [{title, snippet, score}].
    Empty list if offline or no results.
    """
    q = _normalize_title(query)
    if not q:
        return []
    data = _get(
        _API.format(lang=lang),
        {
            "action": "query",
            "list": "search",
            "srsearch": q,
            "srlimit": max(1, min(limit, 10)),
            "srprop": "snippet|title",
            "format": "json",
        },
    )
    if not data:
        return []
    raw = ((data.get("query") or {}).get("search")) or []
    out: list[dict[str, Any]] = []
    for h in raw:
        title = str(h.get("title") or "").strip()
        if not title or not title_looks_useful(title):
            continue
        # strip HTML tags from snippet
        snip = re.sub(r"<[^>]+>", "", str(h.get("snippet") or ""))
        score = title_relevance(q, title, snip)
        if score <= 0 and len(_tokens(q)) >= 2:
            continue
        out.append({"title": title, "snippet": snip, "score": score, "lang": lang})
    out.sort(key=lambda x: -x["score"])
    return out


def search_titles(query: str, *, lang: str = "nl", limit: int = 5) -> list[str]:
    """Titles only (back-compat)."""
    return [h["title"] for h in search_hits(query, lang=lang, limit=limit)][:limit]


def fetch_extract(title: str, *, lang: str = "nl", chars: int = 1200) -> dict[str, Any] | None:
    """Plain-text extract for an exact page title, or None."""
    title = _normalize_title(title)
    if not title:
        return None
    data = _get(
        _API.format(lang=lang),
        {
            "action": "query",
            "prop": "extracts",
            "exintro": "1",
            "explaintext": "1",
            "exchars": max(200, min(int(chars), 2000)),
            "titles": title,
            "redirects": "1",
            "format": "json",
        },
    )
    if not data:
        return None
    pages = ((data.get("query") or {}).get("pages")) or {}
    for pid, page in pages.items():
        if str(pid) == "-1" or page.get("missing") is not None:
            return None
        text = (page.get("extract") or "").strip()
        if not text:
            return None
        resolved = str(page.get("title") or title)
        if not title_looks_useful(resolved):
            return None
        url = f"https://{lang}.wikipedia.org/wiki/{quote(resolved.replace(' ', '_'))}"
        return {
            "title": resolved,
            "lang": lang,
            "text": text[:chars],
            "url": url,
            "source": f"wikipedia:{lang}:{resolved}",
        }
    return None


def fetch_summary(query: str, *, lang: str = "nl", chars: int = 1200) -> dict[str, Any] | None:
    """
    Query-only retrieval: try exact title, then ranked search hits; then English.
    No subject-specific hint tables.
    """
    q = _normalize_title(query)
    if not q:
        return None

    hit = fetch_extract(q, lang=lang, chars=chars)
    if hit and title_relevance(q, hit["title"]) > 0:
        return hit

    for h in search_hits(q, lang=lang, limit=6):
        if h["score"] <= 0:
            continue
        hit = fetch_extract(h["title"], lang=lang, chars=chars)
        if hit:
            return hit

    if lang != "en":
        hit = fetch_extract(q, lang="en", chars=chars)
        if hit and title_relevance(q, hit["title"]) > 0:
            return hit
        for h in search_hits(q, lang="en", limit=6):
            if h["score"] <= 0:
                continue
            hit = fetch_extract(h["title"], lang="en", chars=chars)
            if hit:
                return hit
    return None


def wiki_reachable(lang: str = "nl") -> bool:
    """Cheap connectivity probe (optional diagnostics)."""
    data = _get(
        _API.format(lang=lang),
        {"action": "query", "meta": "siteinfo", "format": "json"},
    )
    return bool(data)
