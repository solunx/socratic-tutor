# Phase B0 — Wikipedia + local docs (no RAG yet)

**Status:** scaffolding only. Not wired into `handle_turn` / judge / gap_card writes.

## Why

The judge grades against the tutor’s premises. False premises (wrong pp step, Coulomb sum vs product)
cannot be fixed by a better verdict style. Evidence must come from **outside** the tutoring model.

## Layout

```text
knowledge/
  wiki.py           # MediaWiki API (nl → en fallback)
  local_docs.py     # keyword search over knowledge/docs/*.md
  verify.py         # evidence_for(claim) → {status, snippets}
  docs/             # your verified notes
    pp_keten.md
    cno_cyclus.md
tools/knowledge_probe.py
tests/test_knowledge_b0.py
```

## API

```python
from knowledge import evidence_for
from knowledge.verify import format_evidence_for_prompt

pack = evidence_for("pp-keten stappen")
# pack["status"] in {"found", "weak", "not_found"}
# pack["snippets"] = [{source, title, text, url, kind: wikipedia|local}, ...]
```

- **Local hits** use simple token overlap (no embeddings).
- **Wikipedia** uses search + plain extract; failures → empty, never raise into the tutor loop.
- The model must **never** set `gap_card.status = source_backed`; only a future Python hook may.

## Manual check on gpu01

```bash
python -m pytest tests/test_knowledge_b0.py -q
python tools/knowledge_probe.py "pp-keten deuterium"
python tools/knowledge_probe.py "CNO cyclus temperatuur"
python tools/knowledge_probe.py "coulomb barrière Z1 Z2"
```

## Not in B0

- Vector RAG / chunk embeddings (Phase C)
- Auto-updating gap_card.status
- Pre-flight that solves a predictive question from sources (B1)
- Blocking turns when wiki is down

## Next (B1, after judge density is fixed)

1. On new predictive `gap_card`, call `evidence_for(key)` and attach `sources` (status still careful).
2. Inject `format_evidence_for_prompt(pack)` into policy/prompt when status is found.
3. Optional: same pack to the judge so it is not premise-only.

## Update (no hardcoding + offline)

- Removed topic→page hint tables. Queries are free-text only.
- Wiki failures (timeout, 429, DNS) → `wiki_ok=False`; local still used; if nothing → `not_found` and the tutor continues without sources.
- Env `TUTOR_EVIDENCE_WIKI=0` forces local-only.
- Structural title filter only (lists, disambiguation, …) — not subject lists.

## B1 — wired into handle_turn (2026-11-02)

- `knowledge/turn_evidence.py`: builds a **generic** query from open gap + concept + learner text (no topic tables).
- `core.handle_turn`: gathers evidence → injects into the system message → appends a **Bronnen:** footer to the response and returns `sources: [...]` in the API payload.
- Disable: `TUTOR_EVIDENCE=0`. Wiki only off: `TUTOR_EVIDENCE_WIKI=0`.
- Model is instructed not to invent sources; footer is Python-owned.
- Still no PDF page anchors / RAG; local `.md` + optional Wikipedia only.
