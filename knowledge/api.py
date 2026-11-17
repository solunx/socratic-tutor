"""
Stable retrieval API — usable by the tutor and by other agents.

This module is the only contract other code should depend on for
"search my knowledge sources". Backends (local files, Wikipedia, later
vector stores) live behind retrieve(); callers never import backends
directly unless they are extending the stack.

Pack shape (always the same)::

    {
      "query": str,
      "status": "found" | "weak" | "not_found",
      "snippets": [
        {
          "source": str,      # e.g. "local:ems.md" or "wikipedia:Title"
          "title": str,
          "text": str,        # excerpt for the model
          "url": str | None,
          "kind": "local" | "wikipedia" | ...,
          "score": float | None,
        },
        ...
      ],
      "wiki_ok": bool,
      "local_ok": bool,
    }

Never raises: on failure returns status=not_found with empty snippets.
"""
from __future__ import annotations

from typing import Any

from knowledge.verify import evidence_for, format_evidence_for_prompt

# Re-export under neutral names so non-tutor agents don't look "tutor-y"
retrieve = evidence_for
format_for_prompt = format_evidence_for_prompt

__all__ = [
    "retrieve",
    "evidence_for",
    "format_for_prompt",
    "format_evidence_for_prompt",
]


def retrieve_local(
    query: str,
    *,
    limit: int = 4,
    path_boosts: list[str] | None = None,
) -> dict[str, Any]:
    """Local-docs only (offline-safe). Same pack shape as retrieve()."""
    return evidence_for(
        query,
        max_snippets=limit,
        use_wiki=False,
        use_local=True,
        path_boosts=path_boosts,
    )
