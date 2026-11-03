# Socratic Offline Tutor

**Local-first personal learning coach** — not a chatbot that answers for you.

Python owns the persistent learner model; a local LLM (e.g. Ollama) handles pedagogical wording under policy constraints.

> What is the learner trying to learn? What can they retrieve, explain, apply, and transfer — and which misconceptions remain?

## Features

- Socratic questioning with policy-driven activities and open gap tracking
- Per-concept mastery dimensions and FSRS-inspired review
- Multi-user web gateway (login, isolated state)
- Optional **evidence** from private notes (`knowledge/docs`) and Wikipedia (offline-safe)
- Subject rename and safeguards against silent topic splits mid-session

## Documentation

| Doc | Contents |
|-----|----------|
| [docs/VISION.md](docs/VISION.md) | Goals and non-goals |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Shipped vs next |
| [docs/USAGE.md](docs/USAGE.md) | Web and CLI usage |
| [docs/PUBLIC_ACCESS.md](docs/PUBLIC_ACCESS.md) | HTTPS / VPN access for remote learners (generic) |

## Quick start (developer)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # if present
# Configure Ollama and model name in your environment / tutor settings
python -m uvicorn gateway.app:app --host 127.0.0.1 --port 8790
```

Create users with `python tools/create_user.py <name>`.
Do **not** expose Ollama on the public internet.

## Privacy

- `data/`, `.env`, and private `knowledge/docs/` content are gitignored.
- Do not commit secrets, real tunnel hostnames, or learner transcripts.

## License

See the repository license file if present; otherwise treat as private until one is added.
