"""Load and search lesson notes under knowledge/docs/ (no embeddings, no topic lists)."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DOCS_DIR = Path(__file__).resolve().parent / "docs"
_WORD = re.compile(r"[a-zA-Zà-öø-ÿÀ-ÖØ-ß0-9]{2,}")


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
    return {m.group(0).lower() for m in _WORD.finditer(text or "")}


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
    """Keyword overlap ranking — offline, any subject (whatever is in docs/)."""
    qtok = _tokens(query)
    if not qtok:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for doc in load_all():
        body = doc["text"]
        title_tok = _tokens(doc["title"])
        dtok = title_tok | _tokens(body[:5000])
        if not dtok:
            continue
        overlap = qtok & dtok
        if not overlap:
            continue
        title_hits = len(qtok & title_tok)
        if len(overlap) < 2 and title_hits < 1:
            continue
        score = len(overlap) + 2.5 * title_hits + len(overlap) / max(len(qtok), 1)
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
    return [s for _, s in scored[: max(1, limit)]]
