# Vision

A **local-first Socratic tutor**: not a chatbot that answers for you.

Python owns the durable learner model (subjects, concepts, evidence, scheduling).
A local LLM (e.g. Ollama) chooses *how* to speak; it does not own truth or session structure.

## Core questions

- What is the learner trying to learn?
- What can they retrieve, explain, apply, and transfer?
- Which misconceptions remain — and how do we know?

## Design principles

| Principle | Practice |
|-----------|----------|
| Socratic | Prefer questions over lectures |
| Retrieval practice | Resume and weak areas start from memory, not a summary dump |
| Spaced repetition | FSRS-inspired intervals on concepts |
| Desirable difficulty | Minimal help; scaffold only when stuck |
| Bounded claims | Hard “wrong” only when evidence justifies it; otherwise probe |
| Local-first | Runs on your hardware; optional external sources must degrade safely |
| No domain hardcoding | Subject matter lives in dialogue and optional `knowledge/docs`, not in Python topic tables |

## Non-goals

- Replacing textbooks or certified instruction
- Unattended public multi-tenant SaaS
- Trusting the model as a sole source of factual truth
