# Phase A — learner_signal + gap card

Design goal: the LLM **formulates** the lesson; **Python** enforces boundaries
(one open gap, no silent topic change, no hard “wrong” on unverified keys).

## What shipped

### 1. `learner_signal` (assessment JSON)

Enum (language-agnostic):

| Signal | Python effect (next policy turn) |
|--------|----------------------------------|
| `correct` | clear stuck flags; `must_advance`; probe rotates (apply/contrast/…) |
| `partial` | **not** stuck-scaffold; mark `partial_ok`; often advance probe type |
| `stuck` | force scaffold on **same** open gap |
| `challenges_tutor` | correction path; gap_card → `disputed` |
| `confused_by_tutor` | restate **same** gap simpler |

Phrase lists (`intent_phrases`) remain a **backup before** the LLM responds.
After the model answers, `apply_learner_signal` stores the structured signal.

### Phase A2 — progress & anti-echo

- Gap **learning** fields: `progress` (`open` | `partial_ok` | `demonstrated` | `closed`)
  and `last_probe_type` (`restate` | `missing_piece` | `apply` | `contrast` | `predict`).
- Pedagogy may advance while `gap_card.status` stays `model_only` (learn ≠ verified truth).
- `REPETITION_FEEDBACK_PHRASES` (“dat heb ik net gezegd”, “same question”) → policy forces
  acknowledge + **new** probe type (not silent statement, not same restate).
- Eval: `evals/cases/19_progress_and_probe_types.json`.

### 2. `gap_card` (on the open thread)

Learning **plan**, not a truth certificate:

```text
target, key, expected_answer, scaffold_ladder[]
status: model_only | source_backed | disputed | human_checked
sources: []              # reserved for verifier / RAG
retracted_claims: []
```

- Default / LLM-proposed status is always **`model_only`**.
- The model **cannot** self-set `source_backed` or `human_checked`.
- `challenges_tutor` → status `disputed`.
- Later (Phase B): external verifier or RAG may set `source_backed` + `sources`.

### 3. Epistemic behaviour

When `status` is `model_only` or `disputed`, policy reasons tell the model:

- do **not** hard-reject the learner from the key alone;
- prefer questions; admit uncertainty;
- never restate `retracted_claims`.

### 4. Eval

`evals/cases/18_learner_signal_and_gap_card.json` (policy-only).

```bash
python3 evals/run_evals.py --policy --case 18
```

## Not in Phase A

- Wikipedia / knowledge_verifier (Phase B)
- Full RAG (Phase C)
- Pre-send validation against a streamed reply
- Domain facts hardcoded in product logic (those belong in **test** fixtures)

## Files touched

- `tutor.py` — signal + gap_card helpers, schema, state slice
- `policy.py` — correction / confused / stuck reasons + epistemic notes
- `system_prompt.md` — EPISTEMICS section
- `evals/cases/18_*.json`, `evals/run_evals.py`
