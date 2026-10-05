# Local Socratic Learning Tutor — Vision & Project Specification

Status: design / living document  
Purpose: product vision and pedagogical goals for the local offline Socratic Learning Tutor.

## 1. Project goal

We are building a **local/offline Socratic Learning Tutor**.

The goal is **not** primarily that the tutor gives good answers.  
The goal is that the **learner** becomes better at reasoning and understanding.

The tutor should therefore:

1. Determine what the learner wants to learn  
2. Ask questions  
3. Analyse the learner’s answer and reasoning  
4. Detect knowledge gaps and misconceptions  
5. Give as little help as possible  
6. Let the learner reach insight themselves  
7. After insight, ask for an explanation in their own words  
8. Have them apply the understanding  
9. Test transfer to a new context  
10. Store concrete evidence  
11. Plan later retrieval and review  

The tutor functions more as a **personal learning coach** than as a ordinary chatbot.

## 2. Environment

- Runs locally on a private Linux host with GPU (Ollama)  
- Access for the operator via private network (e.g. Tailscale), not the public internet  
- Core files: `tutor.py` (CLI), `core.py` (channel-agnostic turn handler), `system_prompt.md`, `learner_state.json`  
- Web UI + gateway for phone/laptop on the same private network  

## 3. Architecture (one-sentence)

A local LLM-driven Socratic tutor where the LLM decides how to act pedagogically, while Python maintains a validated persistent learner model: subjects, concepts, five-dimensional mastery, misconceptions, evidence, sessions, and spaced-repetition state.

## 4. What the system should know

Not only:

> What did the learner ask?

But especially:

- What is the learner trying to learn?  
- What does the learner actually understand?  
- What can they retrieve from memory?  
- What can they explain?  
- What can they apply?  
- Can they transfer the concept?  
- Which misconceptions exist?  
- Which evidence supports those conclusions?  
- What should be reviewed now?  
- What is likely the most valuable next learning activity?  

End state: a **persistent learner model + pedagogical decision loop**, not a generic chat bot.

## 5. Design principles

- **Python owns state** — validated, persistent, never trust raw LLM JSON blindly  
- **LLM owns pedagogy** — questions, scaffolding, activity choice within policy  
- **Socratic first** — prefer questions over answer dumps  
- **Orchestrator** — deterministic priorities (review, high-severity MC, prerequisites, resume retrieval, weak dimension)  
- **Channel-agnostic core** — CLI, web, and future agents call the same `handle_turn`  
- **Private by default** — no public exposure; learner data stays local  
- **Prior art** — study other open tutors for ideas; do not copy code wholesale (see `ROADMAP.md` §11)  

## 6. Non-goals (for now)

- Multi-agent swarms before the single-loop base works  
- Vector DB without a concrete need  
- Public SaaS / accounts  
- Replacing human teachers  

## 7. Related docs

- `ROADMAP.md` — phases, research notes, decision log  
- `system_prompt.md` — LLM contract  
- `LICENSE` — MIT  
- `gateway/` + `web/` — private-network UI  
