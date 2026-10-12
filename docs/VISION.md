# Vision — Socratic Offline Tutor

Status: product vision + journey log  
Audience: future self, collaborators, and anyone reading the repo  

This document restores and expands the original project specification (handoff from early design work) and records how the running system (v0.4.x) maps to that intent.

---

## 1. Original project goal

Build a **local / offline Socratic Learning Tutor**.

The goal is **not** primarily to give good answers.  
The goal is that the **learner becomes better at reasoning and understanding**.

The tutor must:

1. Determine what the learner wants to learn  
2. Ask questions  
3. Analyse the learner’s answer and reasoning  
4. Detect knowledge gaps and misconceptions  
5. Give as little help as possible  
6. Let the learner reach insight themselves  
7. After insight, ask for explanation in own words  
8. Have understanding applied  
9. Test transfer to a new context  
10. Store concrete evidence  
11. Plan later retrieval and review  

It should function as a **personal learning coach**, not a generic chatbot.

### Architecture one-liner (from the original handoff)

> A local LLM-driven Socratic tutor where the **LLM decides how to act pedagogically**, while **Python manages a validated persistent learner model** with subjects, concepts, five-dimensional mastery, misconceptions, evidence, sessions, and spaced-repetition state.

### End-state questions the system should answer

Not only: *What did the learner ask?*

But especially:

- What is the learner trying to learn?  
- What do they actually understand?  
- What can they retrieve from memory?  
- What can they explain?  
- What can they apply?  
- Can they transfer the concept?  
- Which misconceptions exist?  
- What evidence supports those conclusions?  
- What should be reviewed now?  
- What is the most valuable next learning activity?

---

## 2. Original environment constraints

- Linux server with GPU (e.g. RTX 3090), project under `~/dev/tutor`  
- Remote use via SSH / Tailscale / editor  
- Inference **local** via **Ollama**  
- Models evolved: Gemma-class → Qwen 3.x 27B with thinking disabled for latency  
- Early files: `tutor.py`, `system_prompt.md`, `learner_state.json`

### Explicit non-goals (from early design)

Avoid until the base works:

- Writing learner state blindly from raw LLM output without validation  
- Crashing on malformed JSON  
- `/reset` deleting learner state  
- Forcing fixed textbook chapters  
- Answering when a Socratic question is better  
- Embeddings / vector DB without a measured need  
- Multi-agent pedagogy before the single loop is solid  

These constraints still shape the roadmap.

---

## 3. The persistent learner model

### Subjects and concepts

- **Subject:** a learning theme (e.g. “AI-agents”, “zon”, “Gras groeien”) with a **goal** in the learner’s words  
- **Concept:** a unit under a subject (e.g. harness, quantum_tunneling) with mastery and evidence  

Over time, concepts form a **tree** (parent/child) and **prerequisite** edges (soft/hard). Aliases allow natural language resume (“tunneling” → quantum_tunneling).

### Five dimensions (progress metrics)

Per concept, continuous scores in \[0, 1\]:

| Key | Role in coaching |
|-----|------------------|
| `knowledge` | “Have I seen this?” |
| `retrieval` | “Can I pull it without notes?” |
| `application` | “Can I use it on a problem?” |
| `explanation` | “Can I teach it back?” |
| `transfer` | “Does it work in a new setting?” |

Updates come from structured `assessment.dimensions` on each turn, clamped and smoothed in Python—not raw model floats written unchecked.

### Misconceptions

Each concept can hold a list of misconceptions:

- `description`, optional catalogue `id`  
- `severity`: low / medium / high  
- `status`: open / improving / resolved  
- `evidence`, remediation hints  

High open severity can **force** a misconception-check activity. Background **hygiene** merges near-duplicates and ages stale items so the list does not grow without bound. Resolution is driven by solid correct performance and explicit assessment signals—not by the learner editing JSON.

### Evidence and history

- Short evidence strings on concepts and misconceptions  
- Session history of resume points (not full chat transcripts in state)  
- Chat transcripts for UI live in `web_conversation.json` (per-subject threads)

### Spaced repetition

FSRS-inspired fields on concepts (`stability`, `difficulty`, `reps`, `lapses`, `next_review`, …). Grades derive from assessment quality. Values are **clamped** so pathological explosions cannot dominate scheduling. The learner never has to run FSRS manually.

### Active session

Tracks current subject/concept, activity type, question count, open threads (gaps that must be closed), recent questions (anti-repeat), bans, resume instruction, and policy flags (`must_advance`, `force_full_teach`, …).

---

## 4. Pedagogical decision loop

```
User message
    → optional slash command (no LLM)
    → build messages (fixed prompt prefix + dynamic state)
    → LLM JSON: response, routing, assessment, session
    → Python: validate, apply assessment, apply taxonomy/prereqs
    → policy for next activity (may force)
    → persist state
```

**Routing** (LLM suggests, Python executes carefully): continue, switch, new_subject, list_subjects, clarify.

**Policy** (Python, deterministic priorities), roughly:

1. Forced full-teach / scaffold when stuck or “go slow” / formula request  
2. After correct verdict → advance activity type (application/transfer)  
3. Anti-repeat / must_advance loops  
4. High-severity open misconception  
5. Weak hard prerequisite  
6. Just resumed → retrieval  
7. Weakest dimension on focus concept  
8. Else free (LLM chooses)

This split keeps pedagogy **inspectable and testable** (`evals/`) while language stays flexible.

---

## 5. Journey: from handoff to v0.4.x

| Phase | What we learned / built |
|-------|-------------------------|
| Early CLI | Socratic prompt alone is not enough; state must be real |
| Subject UX | Natural language switch + `/subjects`; avoid subject sprawl via aliases/tree |
| JSON reliability | Schema + retry; streaming must not double-print UI |
| Latency | Thinking off; shorter prompt; **fixed prefix then dynamic state** for cache-friendly prompts; measure before vector DB |
| Pedagogy bugs | Repeat questions, ignored learner words, misleading analogies, dumping jargon → policy + static rules (one new term, no bad analogies, verdict advance) |
| Misconceptions | Catalogue + pending free-form + hygiene; no learner-facing admin |
| Tree / prereqs | `ensure_concept_tree`, soft/hard upgrade, `/tree` |
| Web | Private gateway, mobile UI, SSE streaming, **per-subject threads** |
| Evals | Policy cases for MC, FSRS, taxonomy, prereqs, verdict advance |

### What stayed true from day one

- Local / private by default  
- Coach ≠ answer engine  
- Python owns truth about the learner  
- Evidence and dimensions beat “vibes”  

### What we deliberately postponed

- Vector memory / semantic search (until metrics demand it)  
- Multi-agent “roles” as separate processes (policy activities cover the pedagogical roles)  
- Public exposure of the gateway  
- Fully automatic long-term subject merge UI (tree + aliases first)

---

## 6. Interfaces and multi-agent future

The gateway is shaped as a **multi-agent** HTTP surface (`agent=tutor` today). The same Tailscale UI and later STT/TTS path should serve other local agents (research, inquiry, …) without redesigning networking.

Planned voice path: **on-device** STT/TTS (e.g. iPhone), text only over the tailnet—GPU stays for LLM inference.

---

## 7. Success criteria

The project succeeds when a learner can return weeks later and the system:

- Knows **subjects and goals**  
- Shows a coherent **concept tree**  
- Resumes with **retrieval**, not a random lecture  
- Tracks **dimensions and misconceptions** with evidence  
- Schedules **reviews** without user babysitting  
- Feels **faster to insight** than a plain chat model—even when the chat model is the same weights  

Speed and UX matter, but the north star remains the original handoff:

**persistent learner model + pedagogical decision loop.**

---

## 8. Related documents

- `README.md` — how to run and what the code does  
- `ROADMAP.md` — research, prior art, decision log  
- `system_prompt.md` — live pedagogical instructions to the model  
- `evals/` — executable specification of policy behaviour  
