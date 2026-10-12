# Socratic Offline Tutor — Architecture Roadmap & Research Notes

Status: living document  
Last updated: 2026-10-04  
Project: `/home/sun/dev/tutor` (server) · artifacts mirror in this workspace

---

## 1. Project goal (unchanged)

Not a chatbot that answers. A **persistent learner coach** that:

1. Discovers what the learner wants to learn  
2. Asks Socratic questions  
3. Analyses answers and reasoning  
4. Detects knowledge gaps and misconceptions  
5. Gives minimal help  
6. Lets the learner reach insight  
7. Asks for explanation in own words  
8. Applies understanding  
9. Tests transfer  
10. Stores concrete evidence  
11. Plans retrieval and review  

**Architecture one-liner**

> Local LLM-driven Socratic tutor where the LLM decides *how* to speak pedagogically, while Python owns a validated persistent learner model (subjects, concepts, 5D mastery, misconceptions, evidence, sessions, spaced-repetition state).

---

## 2. Current stack (v0.2.1 → evolving)

| Piece | Choice |
|-------|--------|
| Runtime | Linux + Ollama (native `/api/chat`) |
| Model | `qwen3.8:27b` (or `gemma4:e4b` / smaller for speed) |
| State | `learner_state.json` (atomic writes, migration) |
| Prompt | `system_prompt.md` + injected state JSON |
| App | `tutor.py` single-process CLI |

**Key design rules already in code**

- Python owns validation, persistence, score clamps, session lifecycle  
- LLM returns structured JSON: `response`, `routing`, `assessment`, `session`  
- Thinking off by default (`THINK = False`) on native API  
- Situation packing: `teach` vs `meta` context  
- Working memory: `session_thread`, `last_tutor_question`, `recent_questions`  
- Anti-repeat rules in prompt + state  

---

## 3. Research: what other systems do well

**How we use prior art (important):**

This project studies public AI-tutor designs for **ideas and architecture patterns**.
We do **not** copy external repositories wholesale. Implementation in this repo
(tutor.py, core.py, prompts, evals, catalog seed) is original work written for
this project.

| Kind of reuse | Our rule |
|---------------|----------|
| Ideas / pedagogy / architecture | OK — normal engineering research |
| Design patterns (orchestrator, abstain, eval harness) | OK — reimplemented in our own code |
| **Source code or substantial text** from another repo | Only if that project's **LICENSE** allows it, and with required attribution |

Sources reviewed (2026-10): NavyashreeNS/ai-tutor, carlosceja27/ai-tutor, utsabpanta/ai-tutoring, Bhala-Srinivash/agent-tutor-skill, hanselhansel/ai-learning-engine, Tutor MCP, OpenTutor variants, various Hermes skills.

### 3.1 NavyashreeNS/ai-tutor — highest leverage for learner model

Licence (upstream): **MIT** (confirmed on GitHub README / LICENSE).

**Core idea:** four subsystems; **only one** uses an LLM.

| Subsystem | Deterministic? | Role |
|-----------|----------------|------|
| Knowledge Dependency Graph | yes | prerequisites (hard/soft) |
| GRADE | yes | correct / incorrect |
| Bayesian Student Belief Model | yes | mastery update |
| Prerequisite-Graph Question Selector | yes | which concept + which question |
| Misconception Hypothesis Classifier | **only LLM call** | *which* mental model |

**Learn / adapt (ideas only — reimplemented here)**

- **Named misconception catalogue** (closed set per concept), not only free-form text  
- Classifier may **abstain** — forced wrong diagnosis is worse than none  
- **Asymmetric penalty**: named MC costs more mastery than unexplained error  
- Selector teaches the **deepest gap**, not the nearest symptom  
- Reply templates that avoid leaking the full answer as free text (useful for quizzes)

### 3.2 carlosceja27/ai-tutor — pedagogy as skill

Licence (upstream): **Apache 2.0** (LICENSE + NOTICE in repo).

- Modes: `/learn` `/practice` `/review` `/quiz` `/explain`  
- “Never think for the learner”  
- Retrieval before review; layered hints; interleaving  

**Learn / adapt (ideas only):** modes as **policies on the same state**, chosen by orchestrator — not separate agents and not always user-driven.  
No code or SKILL.md text was copied from that repo into this project.

### 3.3 utsabpanta/ai-tutoring — local Socratic + evals

Licence (upstream): **MIT** (stated on GitHub).

- Ollama-first, anti-homework-dump  
- **Eval suite** as quality gate (anti-dump cases, etc.)  
- Versioned prompts  

**Learn / adapt (ideas only):** small `evals/` with fixed dialogues (no answer leak, no question repeat, resume = retrieval first). Our eval cases and runner are written for this codebase.

### 3.4 FSRS / teaching-loop skills (Bhala, hanselhansel, OpenTutor)

- **FSRS** adaptive intervals > fixed day buckets (algorithm from spaced-repetition literature; our scheduler is a **simplified FSRS-inspired** implementation, not a vendor of another project's source tree)  
- Loop: Explain → Example → Check → Evaluate → Practice  
- Zero-hint quizzes; pre-testing; teach-back; confidence calibration  

**Learn / adapt (ideas):** FSRS-like `next_review`; teach-back activity; pre-test on concept resume.

**Licence caution:** `hanselhansel/ai-learning-engine` and `zijinz456/OpenTutor` were **not** confirmed here as MIT/Apache from a primary LICENSE file. Treat as **inspiration only** until LICENSE is verified. Public on GitHub ≠ free to copy code.

### 3.5 Multi-agent (LangGraph, etc.)

Common lesson: **not eight LLMs** — one **router + policies**:

```
Orchestrator (Python rules ± light LLM)
  → Socratic | Retrieval | Quiz | Teach-back | MC probe
```

Multi-agent is premature until a single model systematically fails specialization (matches original project “don’t add multi-agent before the base works”).

---

## 4. What we already do better than many repos

- Persistent multi-subject state with **5 mastery dimensions**  
- Evidence arrays + open misconception tracking  
- Natural-language subject routing + `/new` hybrid UX  
- Session thread + anti-repeat + situation packing  
- Strict Python/LLM separation for state writes  

## 5. What they do better (gap list)

| Gap | Priority |
|-----|----------|
| Deterministic **what next** (activity + concept) before LLM speaks | **P0** |
| Named misconception IDs / closed-set diagnosis | P1 |
| Eval harness (anti-leak, anti-repeat, resume) | P1 |
| FSRS (or SM-2) instead of coarse day buckets | P2 |
| Prerequisite edges (hard/soft) between concepts | P2 |
| True multi-agent orchestration | P3 — not now |

---

## 6. Target architecture v0.3+

```
                    YOUR TUTOR
                         │
       ┌─────────────────┼─────────────────┐
       │                 │                 │
 Learning brain    Learning policies    Dialogue
 (Python state)    (Python rules)       (LLM only)
       │                 │                 │
 5D mastery        next activity        Socratic text
 named MC (later)  FSRS schedule        JSON assessment
 prerequisites     anti-repeat          session_thread
 evidence          mode selection
```

**Layer 1 — Learning brain (state)**  
subjects → concepts → K/R/A/E/T, misconceptions, evidence, last_practiced, next_review, session history.

**Layer 2 — Learning policies (orchestrator)**  
Chooses activity (and optionally concept) from state *before* the LLM call. LLM may refine within bounds, not ignore open high-severity MCs.

**Layer 3 — Dialogue**  
LLM produces learner-facing text + structured assessment + session memory fields.

---

## 7. Implementation backlog (step by step)

### Phase A — Orchestrator (DONE 2026-10-04)

- [x] `suggest_next_activity(state) -> {activity, concept_id, reason, force}`
- [x] Priority order:
  1. Due review for current subject → `REVIEW` (force)
  2. Open high-severity misconception on focus concept → `MISCONCEPTION_CHECK` (force)
  3. Just resumed → `RETRIEVAL` (force)
  4. Weakest dimension on focus concept below 0.45 → mapped activity (soft)
  5. Any open misconception → `MISCONCEPTION_CHECK` (soft)
  6. Else FREE (LLM may choose)
- [x] Injected as `orchestrator` object in state JSON + policy_hint in system message
- [x] Log line: `[policy] activity=… (force|soft) concept=… | reason`
- [x] Soft/force constraint documented for the LLM

### Phase B — Eval harness (DONE 2026-10-04)

- [x] `evals/cases/` JSON cases: structure, anti-repeat, anti-dump, resume-retrieval, list routing, policy high-MC
- [x] `evals/run_evals.py` — Ollama LLM cases + pure policy cases
- [x] CLI: `--list`, `--case`, `--policy`, `--json-out`

### Phase C — Named misconceptions (DONE 2026-10-05)

- [x] `misconception_catalog.json` — closed set (zon + ai_agents seeds)
- [x] Match by id or fuzzy description; abstain if weak match
- [x] Dedupe open MCs by catalogue id in `apply_assessment`
- [x] Inject relevant catalogue slice into teach prompt
- [x] Free-form still allowed when no catalogue fit
- [x] Eval case `07_catalog_match`

### Phase D — Scheduling (DONE 2026-10-05)

- [x] FSRS-inspired scheduler (no external deps): stability, difficulty, reps, lapses
- [x] Grade 1–4 from assessment + open high-severity MCs
- [x] High-severity open MC caps interval at 1 day
- [x] Eval case `08_fsrs_schedule`

### Phase E — Taxonomy + auto-MC growth (DONE 2026-10-05, foundation)

- [x] State v3: subject `parent_id` / `related` / `aliases`; concept `aliases`
- [x] Alias-aware subject resolve (minder wildgroei)
- [x] `link_subjects` parent/related
- [x] `pending_catalog` + auto-commit herhaalde free-form MCs (multi-subject)
- [x] Eval `09_taxonomy_and_pending_mc`
- [x] LLM `routing.taxonomy` (parent / related / aliases) + Python apply
- [ ] Hard subject-merge met redirect (later; soft merge via aliases only)

### Phase E2 — Prerequisites (DONE 2026-10-05)

- [x] `concept.prerequisites: [{id, strength: hard|soft}]`
- [x] Orchestrator redirects to deepest weak hard prereq (force)
- [x] LLM may propose edges via `assessment.prerequisites`
- [x] Eval `11_prerequisites`

### Phase F — UX / product (streaming DONE 2026-10-05)

- [x] Streaming tokens: live-print JSON `response` while generating (`STREAM=True`)
- [x] Stats line shows `stream`; no double-print after full JSON
- [ ] Web or Telegram surface
- [ ] Richer topic tree UI

---

## 8. Performance notes (measured)

| Phase | Input tokens | Typical latency (qwen3.8:27b) |
|-------|--------------|------------------------------|
| Before compact | ~10k | 15–25 s |
| After compact + native think=false | ~4.5–5.5k | 5–11 s |

Further wins: smaller model for iteration, streaming, still-leaner system_prompt, shorter JSON schema reminders.

Thinking control: field is **`think`** (not `thinking`) on native `/api/chat`. Tutor-only; does not change global `ollama run`.

---

## 9. File map

| File | Role |
|------|------|
| `tutor.py` | App, state, orchestrator, Ollama client |
| `system_prompt.md` | Pedagogical constitution + JSON contract |
| `learner_state.json` | Persistent learner model |
| `ROADMAP.md` | This document |
| `evals/` | (planned) regression cases |

---

## 10. Decision log

| Date | Decision |
|------|----------|
| 2026-10 | Keep single-process Python + Ollama; no multi-agent yet |
| 2026-10 | Native `/api/chat` + `think=false` for Qwen3.8 |
| 2026-10 | Situation packing teach/meta; session_thread + anti-repeat |
| 2026-10 | Deterministic activity orchestrator (Phase A) shipped |
| 2026-10 | Prior art: study ideas/patterns only; no wholesale code copy; licence-safe attribution if ever vendoring |
| 2026-10 | Eval harness Phase B shipped (`evals/run_evals.py`) |
| 2026-10 | Named misconception catalogue (Phase C) shipped |
| 2026-10 | FSRS-inspired scheduling (Phase D) shipped |
| 2026-10 | Taxonomy + pending MC auto-promote (Phase E foundation) |
| 2026-10 | Next: LLM propose_parent / richer tree, or streaming |
| 2026-10 | Observability: metrics_lib + per-component token breakdown; measure before optimising |

---

## 11. References (repos) — prior art, not vendored code

| Repo | Licence | How we used it |
|------|---------|----------------|
| [NavyashreeNS/ai-tutor](https://github.com/NavyashreeNS/ai-tutor) | MIT | Ideas: MC catalogue, abstain, deepest gap |
| [carlosceja27/ai-tutor](https://github.com/carlosceja27/ai-tutor) | Apache 2.0 | Ideas: retrieval-first, policy modes |
| [utsabpanta/ai-tutoring](https://github.com/utsabpanta/ai-tutoring) | MIT | Ideas: local Ollama, eval harness |
| [Bhala-Srinivash/agent-tutor-skill](https://github.com/Bhala-Srinivash/agent-tutor-skill) | MIT | Ideas: FSRS, teaching loop |
| [hanselhansel/ai-learning-engine](https://github.com/hanselhansel/ai-learning-engine) | MIT | Ideas: adaptive learning, mastery, Socratic loop |
| [ArnaudGuiovanna/tutor-mcp](https://github.com/ArnaudGuiovanna/tutor-mcp) | MIT | Ideas: adaptive tutoring architecture |
| [zijinz456/OpenTutor](https://github.com/zijinz456/OpenTutor) | MIT | Ideas: local-first adaptive learning |

These repositories were reviewed as prior art and architectural/pedagogical references.
No third-party source code is currently vendored or copied into this repository.

Note: a top-level MIT/Apache licence covers that project's own code; bundled datasets,
images, or third-party libraries may carry separate terms. We only reuse **ideas/patterns**.

If this repository later includes third-party source files, preserve the applicable
LICENSE/NOTICE requirements under `third_party/` and/or a NOTICE file.

---

*When implementing, tick checkboxes in §7 and append to §10. Do not invent multi-agent or vector DB until Phase A–C are solid.*
