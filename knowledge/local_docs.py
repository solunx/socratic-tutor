"""Load and search lesson notes under knowledge/docs/.

Retrieval is generic: text files are split into chunks (see knowledge.chunking),
ranked by keyword overlap with the query, and best chunks returned.

Future file types (pdf, docx, xlsx) should convert to text, then use the same
chunk + rank path — callers still only use knowledge.api.retrieve.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from knowledge.chunking import excerpt_for_query, split_into_chunks, tokens as _tokens

logger = logging.getLogger(__name__)

DOCS_DIR = Path(__file__).resolve().parent / "docs"
_WORD = re.compile(r"[a-zA-Zà-öø-ÿÀ-ÖØ-ß0-9]{3,}")


def docs_root() -> Path:
    import os
    override = (os.environ.get("TUTOR_KNOWLEDGE_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return DOCS_DIR


def list_docs() -> list[Path]:
    root = docs_root()
    if not root.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in (".md", ".txt"):
            continue
        if p.name.upper() == "README.MD":
            continue
        parts_lower = {x.lower() for x in p.relative_to(root).parts}
        if "archive" in parts_lower or "archives" in parts_lower:
            continue
        out.append(p)
    return out


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        logger.exception("read failed %s", path)
        return ""


def load_all() -> list[dict[str, Any]]:
    items = []
    for p in list_docs():
        text = _read(p).strip()
        if not text:
            continue
        title = p.stem.replace("_", " ")
        for line in text.splitlines()[:15]:
            if line.startswith("#"):
                title = line.lstrip("#").strip() or title
                break
        rel = str(p.relative_to(docs_root()))
        items.append({
            "id": rel,
            "path": str(p),
            "title": title,
            "text": text,
            "source": f"local:{rel}",
        })
    return items


def _path_boost_score(doc_id: str, boosts: list[str]) -> float:
    if not boosts:
        return 0.0
    doc_id = (doc_id or "").lower().replace("\\", "/")
    compact = doc_id.replace("_", "").replace("-", "").replace("/", "")
    for b in boosts:
        if not b:
            continue
        if b in compact:
            return 2.5
        if b in doc_id:
            return 2.0
    return 0.0


def search_local(
    query: str,
    *,
    limit: int = 3,
    snippet_chars: int = 800,
    path_boosts: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Keyword overlap ranking over chunks (not document prefixes).

    Snippet text is query-centered when clipped so decisive sentences
    near the end of a chunk are not dropped.
    """
    qtok = _tokens(query)
    if not qtok:
        return []
    boosts = [b.lower() for b in (path_boosts or []) if b]
    scored: list[tuple[float, dict[str, Any]]] = []

    for doc in load_all():
        body = doc["text"]
        title_tok = _tokens(doc["title"])
        path_bonus = _path_boost_score(doc.get("id") or "", boosts)
        chunks = split_into_chunks(body)
        if not chunks:
            continue

        for ch in chunks:
            ctext = ch["text"]
            ctok = _tokens(ctext)
            if not ctok:
                continue
            overlap = len(qtok & ctok)
            if overlap == 0 and path_bonus <= 0:
                continue
            title_hit = len(qtok & title_tok) * 0.5
            head = ch.get("heading") or ""
            head_hit = len(qtok & _tokens(head)) * 0.75
            density = overlap / max(1, len(ctok)) ** 0.5
            score = float(overlap) + title_hit + head_hit + density + path_bonus
            # Prefer denser short chunks slightly
            if len(ctext) < 400:
                score += 0.15
            excerpt = excerpt_for_query(ctext, query, max_chars=snippet_chars)
            scored.append((score, {
                "source": doc["source"],
                "title": doc["title"],
                "text": excerpt,
                "url": None,
                "kind": "local",
                "score": round(score, 3),
                "heading": head or None,
                "path": doc.get("id"),
            }))

    scored.sort(key=lambda x: (-x[0], x[1].get("path") or "", x[1].get("heading") or ""))
    # Dedupe near-identical excerpts
    out: list[dict[str, Any]] = []
    seen_txt: set[str] = set()
    for score, item in scored:
        key = (item.get("text") or "")[:120].lower()
        if key in seen_txt:
            continue
        seen_txt.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out
