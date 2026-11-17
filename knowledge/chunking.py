"""
Document chunking + query-centered excerpts.

Industry-aligned defaults (no domain hardcoding):
  - Prefer markdown/section boundaries, then size limits with soft breaks
  - When truncating for a model window, center on query term hits (not prefix-only)

Used by the local backend; other backends can reuse the same helpers.
Future: swap in LangChain/LlamaIndex splitters behind the same functions
without changing knowledge.api.retrieve.
"""
from __future__ import annotations

import re
from typing import Any

_WORD = re.compile(r"[a-zA-Zà-öø-ÿÀ-ÖØ-ß0-9]{3,}")
_HEADING = re.compile(r"^(#{1,6})\s+\S", re.M)

# ~512-token ballpark in characters for Latin text; soft industry default
_MIN_CHUNK = 80
_MAX_CHUNK = 900
_MERGE_UNDER = 120


def tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _WORD.finditer(text or "")}


def split_into_chunks(
    text: str,
    *,
    max_chars: int = _MAX_CHUNK,
    min_chars: int = _MIN_CHUNK,
    merge_under: int = _MERGE_UNDER,
) -> list[dict[str, Any]]:
    """
    Split plain text / markdown into retrieval chunks.

    1. Split on markdown headings and blank lines.
    2. Merge tiny fragments into the previous chunk.
    3. Hard-split oversize blocks on paragraph/sentence boundaries.
    Each chunk may carry a nearby heading as context prefix.
    """
    raw = (text or "").replace("\r\n", "\n").strip()
    if not raw:
        return []

    parts: list[str] = []
    buf: list[str] = []
    for line in raw.split("\n"):
        if _HEADING.match(line) and buf:
            parts.append("\n".join(buf).strip())
            buf = [line]
        elif line.strip() == "" and buf:
            parts.append("\n".join(buf).strip())
            buf = []
        else:
            buf.append(line)
    if buf:
        parts.append("\n".join(buf).strip())

    parts = [p for p in parts if p]

    final_blocks: list[str] = []
    for block in parts:
        if len(block) <= max_chars:
            final_blocks.append(block)
            continue
        start = 0
        while start < len(block):
            end = min(start + max_chars, len(block))
            window = block[start:end]
            if end < len(block):
                cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("\n"))
                if cut >= max_chars // 3:
                    end = start + cut + (2 if window[cut:cut + 2] == "\n\n" else 1)
            piece = block[start:end].strip()
            if piece:
                final_blocks.append(piece)
            start = end if end > start else start + max_chars

    chunks: list[dict[str, Any]] = []
    last_heading = ""
    for i, block in enumerate(final_blocks):
        heading = ""
        first = block.split("\n", 1)[0]
        if _HEADING.match(first):
            heading = first.lstrip("#").strip()
            last_heading = heading
        elif last_heading:
            heading = last_heading

        if heading and not _HEADING.match(block.split("\n", 1)[0]):
            display = f"## {heading}\n{block}"
        else:
            display = block

        if len(display.strip()) < min_chars and chunks:
            prev = chunks[-1]
            combined = (prev["text"] + "\n\n" + display).strip()
            if len(combined) <= int(max_chars * 1.25):
                prev["text"] = combined
                continue

        chunks.append({
            "text": display.strip(),
            "heading": heading or None,
            "index": i,
        })
    return chunks


def excerpt_for_query(text: str, query: str = "", *, max_chars: int = 350) -> str:
    """
    Truncate text for a narrow model window (e.g. judge).

    If query tokens appear in the text, return a window centered on the densest
    hit region so decisive sentences are not lost to a blind prefix cut.
    """
    t = (text or "").strip()
    if not t:
        return ""
    if len(t) <= max_chars:
        return t
    qtok = tokens(query)
    if not qtok:
        return t[:max_chars]

    lower = t.lower()
    # Score sliding windows by how many distinct query tokens they contain
    best_start = 0
    best_score = -1
    step = max(40, max_chars // 6)
    for start in range(0, max(1, len(t) - max_chars + 1), step):
        window = lower[start : start + max_chars]
        score = sum(1 for tok in qtok if tok in window)
        # Prefer earlier windows on ties (stable)
        if score > best_score:
            best_score = score
            best_start = start

    if best_score <= 0:
        return t[:max_chars]

    # Snap start slightly earlier to keep a sentence beginning when possible
    start = best_start
    if start > 0:
        snap = t.rfind(". ", max(0, start - 80), start)
        if snap != -1:
            start = snap + 2
    end = min(len(t), start + max_chars)
    out = t[start:end].strip()
    if start > 0:
        out = "…" + out
    if end < len(t):
        out = out + "…"
    return out
