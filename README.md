# Socratic Offline Tutor

**Local-first personal learning coach** — not a chatbot that answers for you.

The tutor helps you *reason*, *retrieve*, *explain*, and *transfer* knowledge.  
Python owns the persistent learner model; a local LLM (Ollama) decides how to speak pedagogically.

> *What is the learner trying to learn? What do they actually understand?  
> What can they retrieve, explain, apply, and transfer — and which misconceptions remain?*

---

## Gebruikershandleiding (alleen web)

Voor vrienden / leerlingen die **alleen de browser** gebruiken:

→ **[docs/HOW_TO_USE.md](docs/HOW_TO_USE.md)** (Nederlands, geen CLI/Python)

Beheerder: accounts aanmaken met `python tools/create_user.py <naam>`, public link via Tailscale Funnel — zie [docs/PUBLIC_ACCESS.md](docs/PUBLIC_ACCESS.md).

---

## What it does

1. Discovers what you want to learn (natural language or `/new`)
2. Asks **Socratic** questions (minimal direct answers)
3. Analyses your answer and reasoning
4. Detects knowledge gaps and **misconceptions**
5. Gives as little help as needed — prefers questions and scaffolds
6. Lets you reach insight yourself
7. Asks you to explain in your own words
8. Pushes **application** and **transfer**
9. Stores **evidence** on every meaningful turn
10. Schedules **spaced repetition** (FSRS-inspired)
11. Resumes sessions with retrieval first, not a cold restart

It is closer to a **personal learning coach** than to ChatGPT-with-memory.

---

## Pedagogical framework

### Learning science principles (design targets)

| Principle | How the tutor uses it |
|-----------|------------------------|
| **Socratic method** | Prefer questions over lectures; force reasoning |
| **Retrieval practice** | Resume and weak dimensions → retrieval prompts |
| **Spaced repetition** | FSRS-like stability / next_review on concepts |
| **Desirable difficulty** | Minimal help; scaffold only when stuck |
| **Immediate feedback** | Incorrect → clear signal; misconception handling close to the error |
| **Zone of proximal development** | One missing piece + simpler check; not a textbook dump |
| **Transfer** | After solid local mastery, new contexts |
| **Explain in own words** | Explanation dimension and teach-back style prompts |

### Five mastery dimensions (per concept)

Each concept under a subject tracks scores in **\[0, 1\]**:

| Dimension | Meaning |
|-----------|---------|
| **Knowledge** | Factual / conceptual familiarity |
| **Retrieval** | Can produce the idea from memory without cues |
| **Application** | Can use it in a concrete problem |
| **Explanation** | Can state it clearly in own words |
| **Transfer** | Can use it in a new or analogous context |

Policy (Python) often picks the next activity from the **weakest** dimension, with guards so transfer is not forced while knowledge/retrieval/explanation are still thin.

### Activity types

`RETRIEVAL` · `EXPLANATION` · `APPLICATION` · `TRANSFER` · `MISCONCEPTION_CHECK` · `FULL_TEACH` (slow / formula / “go slow”) · free LLM choice when no strong signal.

After a **correct** verdict, policy advances toward application/transfer instead of re-asking the same “explain why” check.

### Misconceptions

- Free-form + optional **named catalogue** (fuzzy match)
- Status: `open` → `improving` → `resolved`
- Hygiene: dedupe, aging, auto-demote after solid correct turns (domain-agnostic)
- High-severity open MCs can **force** `MISCONCEPTION_CHECK`

### Concept tree & prerequisites

- Subjects → concepts with optional `parent_concept_id`
- Soft/hard **prerequisites**; weak hard prereqs redirect focus deeper first
- LLM may propose taxonomy (parent, related, aliases); Python applies conservatively
- CLI/Web: `/tree`

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Interfaces                                             │
│   CLI (tutor.py)  ·  Web UI (private network / HTTPS)  ·  (future TTS) │
└───────────────────────────┬─────────────────────────────┘
                            │  core.handle_turn()
┌───────────────────────────▼─────────────────────────────┐
│  Python (authority)                                     │
│   · learner_state.json (atomic writes)                  │
│   · policy: suggest_next_activity()                     │
│   · FSRS-ish scheduling, hygiene, tree, routing apply   │
│   · evals/ regression harness                           │
└───────────────────────────┬─────────────────────────────┘
                            │  structured JSON
┌───────────────────────────▼─────────────────────────────┐
│  Local LLM via Ollama                                   │
│   · system_prompt.md + fixed prefix + dynamic state     │
│   · response + routing + assessment + session           │
└─────────────────────────────────────────────────────────┘
```

**One-liner:** local LLM decides *how* to teach; Python owns *what is true about the learner*.

### Important files

| Path | Role |
|------|------|
| `core.py` | Shared turn handler (CLI + web): commands, routing, lobby |
| `tutor.py` | State, prompt build, CLI entry |
| `llm.py` | Ollama client: stream, JSON retry, metrics |
| `policy.py` | Deterministic next-activity rules |
| `intent_phrases.py` | NL intent phrases (repeat / skip / subjects) |
| `auth.py` | Multi-user: PBKDF2 passwords, session cookies |
| `system_prompt.md` | Pedagogical system prompt |
| `gateway/app.py` | FastAPI: auth, SSE, per-subject threads, lobby |
| `web/index.html` | Mobile-friendly UI (login, lobby, streaming) |
| `tools/create_user.py` | Create / reset user accounts |
| `tools/check_users.py` | Inspect per-user state/threads on disk |
| `tools/test_isolation_save.py` | Regression: no cross-user `save_state` |
| `tools/restore_george_clean.py` | Optional: restore one account from backup |
| `data/users/<id>/` | Per-user state + chats (not committed) |
| `evals/` | Policy + multi-turn regression cases |
| `docs/HOW_TO_USE.md` | End-user guide (web only) |
| `docs/PUBLIC_ACCESS.md` | HTTPS / Funnel for friends outside Tailscale |
| `docs/VISION.md` | Product vision & pedagogy |
| `ROADMAP.md` | Research notes, prior art, decision log |

### Interfaces

- **CLI:** `python tutor.py` — streaming, colors, `/tree`, `/state`, …
- **Web:** gateway on a **private / Tailscale** address only — not public internet  
  - Streaming tokens (SSE)  
  - **Per-subject chat threads** in the sidebar  
  - Shared learner state with CLI

---

## Quick start

```bash
# Ollama model available, e.g. qwen3.8:27b
cd ~/dev/tutor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-web.txt   # fastapi, uvicorn, requests, …

# CLI
python tutor.py

# Web (private bind)
python -m uvicorn gateway.app:app --host 0.0.0.0 --port 8790
# Open http://<private-or-tailscale-host>:8790/ from phone/laptop on the tailnet
```

Useful commands: `/help` · `/subjects` · `/tree` · `/state` · `/state full` · `/review` · `/quit`

Natural language works in any language the model handles (“ik wil verder met agents”, “let’s go back to the sun”).

---

## Evals

```bash
python3 evals/run_evals.py --policy    # fast, no LLM
python3 evals/run_evals.py             # includes LLM cases (needs Ollama)
```

Policy cases cover JSON shape, anti-repeat intent, resume retrieval, MC force, FSRS, taxonomy, prerequisites, verdict→application, MC auto-improve.

---

## Design rules (non-negotiable)

- **No domain hardcoding** in policy (no “sun/pion-only” rules); patterns are generic  
- **Do not copy** external tutor repos wholesale; borrow *policies*, reimplement  
- Prefer **measure then optimize** (metrics, prompt prefix, conversation cap)  
- Learner should not manage FSRS/MC catalogues manually — background hygiene  
- Safety: gateway stays on private network; no public port-forward

---

## License

MIT (see `LICENSE` if present). Prior-art references and their licenses are listed in `ROADMAP.md`.

---

## Status

**v0.5.1** — multi-user web + CLI, lobby, per-subject threads, streaming, policy/FSRS/tree/MC hygiene, diagnostic intake, **strict per-user isolation** on save/load.

See `docs/VISION.md` for the original goals and how the system evolved.

### Multi-user isolation (important)

| Rule | Implementation |
|------|----------------|
| One directory per user | `data/users/<id>/{learner_state.json, web_conversation.json}` |
| Cookie → user id | HMAC session token; every API load/save uses that id |
| No silent “default user” | `STRICT_USERS=True` in the gateway; `save_state` resolves path from **explicit `user_id`** or **`state.learner.id`**, never maps another user’s blob onto `DEFAULT_USER_ID` |
| Cross-write blocked | `save_state` raises if `learner.id` ≠ path user |
| Regression | `python tools/test_isolation_save.py` must **PASS** after changes to session/save paths |

Root cause of an earlier mix (vriend1 overwriting george): `start_session` / `close_session` called `save_state(state)` **without** `user_id`, which fell back to `DEFAULT_USER_ID`. Fixed in v0.5.1.


## Multi-user & public access

- **Auth:** username/password (`auth.py`), HTTP-only session cookie, per-user data under `data/users/<id>/`.
- **No self-signup** — `python tools/create_user.py <user>`
- **CLI:** `python tutor.py` prompts for account (or `TUTOR_USER=george python tutor.py`).
- **Tailscale:** private access on your network.
- **Public friends:** [docs/PUBLIC_ACCESS.md](docs/PUBLIC_ACCESS.md) (Tailscale Funnel recommended).
- Do **not** expose Ollama publicly.
- API responses: `Cache-Control: private, no-store` + `Vary: Cookie` (no proxy reuse across users).
