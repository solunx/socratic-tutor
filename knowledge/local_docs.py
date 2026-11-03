"""Load and search lesson notes under knowledge/docs/ (no embeddings, no topic lists)."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DOCS_DIR = Path(__file__).resolve().parent / "docs"
# Min length 3 avoids noise like de/en/op matching every Dutch doc
_WORD = re.compile(r"[a-zA-Zà-öø-ÿÀ-ÖØ-ß0-9]{3,}")
_STOP = frozenset({
    "the", "and", "for", "are", "but", "not", "you", "all", "can", "had", "her", "was", "one",
    "our", "out", "has", "his", "how", "its", "may", "new", "now", "old", "see", "way", "who",
    "did", "get", "let", "put", "say", "she", "too", "use",
    "een", "van", "voor", "met", "het", "dat", "die", "dit", "als", "ook", "nog", "wel", "niet",
    "dan", "bij", "naar", "over", "onder", "waar", "geen", "meer", "zeer", "deze", "zijn", "wordt",
    "worden", "heeft", "hebben", "kunnen", "moeten", "zou", "zijn", "haar", "hem",
})


def docs_root() -> Path:
    return DOCS_DIR


def list_docs() -> list[Path]:
    root = docs_root()
    if not root.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in (".md", ".txt") and p.name.upper() != "README.MD":
            out.append(p)
    return out


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        logger.exception("read failed %s", path)
        return ""


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _WORD.finditer(text or "") if m.group(0).lower() not in _STOP}


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


def search_local(query: str, *, limit: int = 3, snippet_chars: int = 800) -> list[dict[str, Any]]:
    """Keyword overlap ranking — prefer a clear top hit; drop weak runners-up."""
    qtok = _tokens(query)
    if not qtok:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for doc in load_all():
        body = doc["text"]
        title_tok = _tokens(doc["title"])
        dtok = title_tok | _tokens(body[:8000])
        if not dtok:
            continue
        overlap = qtok & dtok
        if not overlap:
            continue
        title_hits = len(qtok & title_tok)
        # Need at least 2 content tokens or 1 title token from the query
        if len(overlap) < 2 and title_hits < 1:
            continue
        score = len(overlap) + 3.0 * title_hits + len(overlap) / max(len(qtok), 1)
        snippet = body[:snippet_chars].strip()
        scored.append((score, {
            "source": doc["source"],
            "title": doc["title"],
            "text": snippet,
            "path": doc["path"],
            "score": round(score, 3),
            "url": None,
        }))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return []
    top = scored[0][0]
    # Keep only strong hits: >= 55% of best score and absolute floor
    floor = max(2.5, top * 0.55)
    filtered = [(s, d) for s, d in scored if s >= floor]
    # If the leader is clearly alone, only return that one
    if len(filtered) >= 2 and filtered[0][0] >= filtered[1][0] * 1.35:
        filtered = filtered[:1]
    return [d for _, d in filtered[: max(1, limit)]]
