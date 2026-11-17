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

## Evidence / retrieval (separate capability)

Package: `knowledge/` — **not** tutor-only. Other agents should use the same module.

| Surface | Who uses it |
|---------|-------------|
| `knowledge.api.retrieve(query)` | Any agent: grounded snippets |
| `knowledge.api.retrieve_local(query)` | Offline / local-only |
| `knowledge.turn_evidence.gather_turn_evidence(...)` | **Tutor only** (session → query + UI footer) |
| `knowledge.focus` | **Tutor only** (learner focus phrase) |

- **Local:** search over `knowledge/docs/` (or `TUTOR_KNOWLEDGE_DIR`); section-oriented chunks.
- **Wikipedia:** optional; failures set `wiki_ok=False` and do not break the turn.
- **Offline:** local-only or empty pack; caller continues without sources.
- **Contract:** see `knowledge/README.md`. Backends may later use BM25/embeddings **behind** `api.retrieve` without changing agents.
- **Attribution:** Python-owned `sources` on the API message; model does not invent URLs.

## Capability modules (plug-in view)

| Module | Role | Consumers |
|--------|------|-----------|
| `knowledge/` | Retrieval + evidence packs | Tutor, future agents |
| `gateway/` | Auth, HTTP API, multi-user | Web UI, clients |
| `web/` | Browser UI | Humans |
| `core` / `policy` / `tutor` | Socratic loop, gates, state | Tutor product only |
| `sim/` | Behaviour lab / student agents | Evaluation |

Shared infrastructure (retrieval, gateway) should stay free of tutor pedagogy so new agents can attach without forking the stack.

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

## Focus mode and subject choice (incremental)

- **Focus mode:** if the learner names a mode (e.g. Eco, EV Boost), `active_session.focus_mode` is set. Policy appends a FOCUS MODE line so the model stays on that mode’s rules.
- **Subject choice:** when the model proposes `new_subject` mid-session without an explicit `/new`, the tutor asks stay / switch to match / create new (with optional parent link) instead of deciding silently.
- Local evidence prefers paths matching the current subject/mode tokens; `archive/` folders under `knowledge/docs` are skipped.
