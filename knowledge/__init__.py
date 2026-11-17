"""
Knowledge / retrieval package — separate capability module.

Boundary
--------
* **Public API (any agent):** ``knowledge.api.retrieve``, ``retrieve_local``,
  ``format_for_prompt``. Prefer ``from knowledge.api import retrieve``.
* **Tutor turn glue (tutor only):** ``knowledge.turn_evidence.gather_turn_evidence``
  builds a query from session state and attaches footers for the web UI.
* **Session focus (tutor only):** ``knowledge.focus`` — not part of retrieval.
* **Backends (internal):** ``local_docs``, ``wiki`` — swap or extend behind
  ``api.retrieve``; do not call from agents unless you are maintaining backends.

Docs for other agents: ``knowledge/README.md``.

Environment
-----------
* ``TUTOR_EVIDENCE=0`` — disable gathering in the tutor loop
* ``TUTOR_EVIDENCE_WIKI=0`` — local only (offline / tests)
* ``TUTOR_KNOWLEDGE_DIR`` — override path to markdown notes
"""
from knowledge.api import (
    evidence_for,
    format_evidence_for_prompt,
    format_for_prompt,
    retrieve,
    retrieve_local,
)

__all__ = [
    "retrieve",
    "retrieve_local",
    "evidence_for",
    "format_for_prompt",
    "format_evidence_for_prompt",
]
