# Capability modules

How pieces of this repo are meant to stay separable so **other agents** can reuse infrastructure without taking on tutor pedagogy.

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  web/       │────▶│  gateway/    │────▶│  tutor loop │
│  (UI)       │     │  (auth, API) │     │  core/policy│
└─────────────┘     └──────┬───────┘     └──────┬──────┘
                           │                    │
                           │              ┌─────▼──────┐
                           │              │ knowledge/ │
                           │              │ api.retrieve│
                           └──────────────┤ backends   │
                                          └────────────┘
                                                ▲
                                    future agents│
```

## Rules

1. **Retrieval** (`knowledge.api`) has no Socratic policy, no learner state, no judge.
2. **Gateway** has no domain lessons; it routes users and serves files.
3. **Tutor loop** may call retrieval and gateway; retrieval must not import `core`/`policy`.
4. New agents: `from knowledge.api import retrieve` — same packs, same citations shape.

## Tutor-only adapters

- `knowledge.turn_evidence` — builds a query from open threads / focus.
- `knowledge.focus` — stores a learner-named focus label on the session.

These may import tutor helpers; backends and `api.retrieve` must not.
