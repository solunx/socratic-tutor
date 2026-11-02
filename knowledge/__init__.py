"""Phase B0 — trusted evidence from Wikipedia + local lesson notes.

The LLM never sets gap_card.status to source_backed; only Python (verify.py)
may attach sources. See docs/PHASE_B0_SOURCES.md.
"""
from knowledge.verify import evidence_for

__all__ = ["evidence_for"]
