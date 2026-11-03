# Roadmap

Shipped capabilities and intended next steps. This is not a changelog of every experiment.

## Shipped (stable enough to document)

- Local Ollama-backed Socratic tutoring (CLI + web gateway)
- Per-user state, login, isolated data directories
- Subjects, concepts, five mastery dimensions, FSRS-inspired review
- Policy-driven activities, open threads, gap cards, learner signals
- Intent / correctness helpers; optional impartial judge path (still imperfect on hard cases)
- Evidence layer: local `knowledge/docs` + optional Wikipedia, offline-safe, UI sources
- Subject rename (`/rename`, natural-language rename while goal is pending)
- Guardrails against mid-session silent `new_subject` for sub-topics
- Eval harness and contract tests for policy/evidence

## Next (priority order may change)

1. **Subject choice in lobby** — when a turn looks like switch/new, match existing subjects and ask the learner (stay / switch / confirm new); do not decide silently.
2. **Judge / feedback quality** — reliable verdicts on contested answers; less false praise; less empty “you already explained…” loops.
3. **Richer evidence** — better ranking, optional RAG over private notes; safer question pre-flight against sources.
4. **UI polish** — first-class source chips, fewer history edge cases after reconnect.
5. **Ops** — clearer deploy recipes without publishing private hostnames or secrets.

## Explicitly out of scope for now

- Hardcoded curriculum graphs per school subject in product code
- Fully automated multi-tenant public SaaS
- Treating Wikipedia as sole authority
