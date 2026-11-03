# Architecture

High-level shape of the tutor. Implementation details live in code (`core.py`, `tutor.py`, `policy.py`, `knowledge/`).

## Responsibilities

| Layer | Owns |
|-------|------|
| **Python** | Learner state, subjects/concepts, policy (next activity), gates, FSRS-like scheduling, evidence retrieval, message persistence |
| **LLM** | Wording of questions and feedback within policy constraints; JSON assessment fields |
| **Gateway** | Auth, multi-user isolation, web UI, streaming |

The model **formulates**; Python **enforces** boundaries (one open pedagogical gap, no silent topic hijack, sources only from retrieval).

## Session flow

1. Learner message (web or CLI).
2. Optional same-turn intent / correctness signals (and optional judge call).
3. Policy suggests activity (probe type, scaffold, rotate, reframe, …).
4. Optional **evidence pack** from local notes + Wikipedia (best-effort).
5. Main model turn with system prompt + policy + evidence.
6. Pre-send gates (repeat / leak / repair heuristics) where configured.
7. Persist assessment, open threads, and optional `sources` on the assistant message.

## Gap card and learner signals

An open thread may carry a **gap card**: learning *plan*, not a certificate of truth.

- Status values include `model_only`, `source_backed`, `disputed`, `human_checked`.
- Only Python (or a future verifier), not the LLM, should promote status to source-backed.
- Learner signals (`correct`, `partial`, `stuck`, `challenges_tutor`, …) steer the *next* policy turn.

Pedagogy can advance while a gap remains `model_only` (learning progress ≠ verified fact).

## Evidence (local docs + optional Wikipedia)

Package: `knowledge/`

- **Local:** keyword search over `knowledge/docs/` (private lesson notes; not required in git).
- **Wikipedia:** optional; failures must not break the turn (`wiki_ok=False`).
- **Offline:** local-only or empty pack; tutor continues without sources.
- **Injection:** short SOURCE EVIDENCE block in the system prompt when hits exist.
- **Attribution:** structured `sources` on the API message (e.g. `ems.md`); UI may show a sources block.
- **No topic tables** in code: queries are built from the current gap, concept, and learner text.

Embeddings / full RAG are a later step behind the same `evidence_for()` idea.

## Subjects

- Created via natural language or `/new <title>`.
- After create, the learner can **rename** (`het onderwerp is EMS`, `/rename EMS`) before or while setting a goal.
- Mid-session, opening a *related* mode (e.g. EV Boost under EMS) should **not** silently create a new subject; prefer staying on the current subject unless the learner explicitly asks for a new one (`/new`, “nieuw onderwerp”, …).
- Future work: lobby-side **subject choice** when the model proposes switch/new (match existing subjects, ask the learner).

## Multi-user

- Gateway auth; state under `data/users/<id>/` (gitignored).
- Never expose Ollama to the public internet; only the gateway (behind HTTPS/VPN) should be reachable by learners.

## Observability

- Per-user events and metrics tools under `tools/` (e.g. `tail_events`, cache probes).
- Contract tests under `tests/` guard policy and evidence behaviour without requiring a live GPU.
