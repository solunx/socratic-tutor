"""
Product code and prompts must be subject-agnostic AND language-agnostic.
Subject data belongs in data files (misconception_catalog.json, taxonomy_defaults.json), not in code.
Test fixtures are exempt. Add words here when a new leak shows up in review.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PRODUCT = ["tutor.py", "core.py", "policy.py", "llm.py", "intent_phrases.py", "guards.py",
           "system_prompt.md", "gateway/app.py"]
BANNED = ["proton", "neutron", "coulomb", "kracht", "deeltje", "stekker", "bindings-vs", "chain step",
          "heavier nucleus", "positron", "isotoop", "deuterium", "kernfusie", "pp-keten", "pp_chain",
          "nuclear_fusion", "quantum_tunneling"]


def _hits():
    out = []
    for rel in PRODUCT:
        p = ROOT / rel
        if not p.exists():
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            low = line.lower()
            out += [(rel, n, w) for w in BANNED if w in low]
    return out


def test_no_subject_words_in_product_code():
    hits = _hits()
    assert not hits, "domain words in product code:\n" + "\n".join(f"  {f}:{n}  {w}" for f, n, w in hits)
