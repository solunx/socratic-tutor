# Knowledge / retrieval module

Standalone capability: **find grounded snippets** from local notes and
(optional) Wikipedia. Not tutor-specific. Other agents should plug in here
instead of reimplementing search.

## Public API

```python
from knowledge.api import retrieve, retrieve_local, format_for_prompt

pack = retrieve("EV Boost zonder import", max_snippets=3)
# pack["status"] in {"found", "weak", "not_found"}
# pack["snippets"] — list of {source, title, text, url, kind, score?}

# Offline / no wiki:
pack = retrieve_local("same query", limit=4)

# For an LLM system message:
block = format_for_prompt(pack, max_chars=1600)
```

`retrieve` never raises. Empty / failed search → `status="not_found"`.

## Layout

| Path | Role |
|------|------|
| `api.py` | **Stable contract** for all consumers |
| `verify.py` | Combines backends into one pack (implementation of `retrieve`) |
| `local_docs.py` | Local markdown under `knowledge/docs/` (or `TUTOR_KNOWLEDGE_DIR`) |
| `wiki.py` | Wikipedia MediaWiki API (best-effort) |
| `turn_evidence.py` | **Tutor-only**: query from session + UI footer |
| `focus.py` | **Tutor-only**: learner focus phrase for ranking hints |
| `docs/` | Local lesson / domain notes (gitignored if private) |

## Design principles

1. **Retrieval ≠ tutoring.** Policy, judge, Socratic turns live in `core` /
   `policy` / `tutor`. This package only answers: *what text supports this query?*
2. **Backends are swappable.** Replace or add vector/BM25 behind `api.retrieve`
   without changing agent call sites.
3. **Industry defaults for chunking.** Prefer section (`##`) boundaries, then
   size-limited pieces with overlap — not one whole file as a single blob.
4. **Citations stay Python-owned.** Agents display `sources` from the pack;
   the model does not invent URLs.

## Tutor integration

```text
handle_turn
  → turn_evidence.gather_turn_evidence(state, learner_text)  # builds query
  → api.retrieve / evidence_for
  → inject into system prompt + sources_payload for the web UI
```

Other agents skip `turn_evidence` and call `retrieve(query)` directly.

## Env

| Variable | Effect |
|----------|--------|
| `TUTOR_EVIDENCE=0` | Tutor skips evidence for the turn |
| `TUTOR_EVIDENCE_WIKI=0` | No Wikipedia calls |
| `TUTOR_KNOWLEDGE_DIR` | Alternate docs root |


## Future backends (same API)

All of these plug in **behind** `api.retrieve` — agents keep the same call:

| Backend | Status | Notes |
|---------|--------|--------|
| Local `.md` / `.txt` | **now** | `local_docs` + `chunking` |
| Wikipedia | **now** | optional; `TUTOR_EVIDENCE_WIKI=0` disables |
| Vector / hybrid RAG | planned | embeddings + BM25 still return the same pack shape |
| PDF / DOCX / XLSX | planned | parse → text → same chunk + rank (e.g. Docling / markitdown) |

Do not add agent-specific branching in backends. Convert to text, chunk, rank, return snippets.
