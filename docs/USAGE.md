# Usage

## Web

1. Open the gateway URL (private VPN or HTTPS tunnel).
2. Log in with an account created by the admin.
3. Use the lobby to pick or start a subject, or type naturally.
4. Useful commands in the chat:
   - `/new <short title>` — new subject  
   - `/rename <title>` — rename the active subject  
   - `/state` — short status (if enabled in UI)  
   - Topic switch / resume via the sidebar when available  

Sources under an answer (e.g. `ems.md`) open a read-only view of a local knowledge file when configured.

## CLI (developer)

From the project root with the virtualenv active:

```bash
# typical checks
python -m py_compile core.py policy.py tutor.py
python -m pytest -q

# evidence probe (local docs + optional Wikipedia)
python tools/knowledge_probe.py "your query"

# events (if used)
python tools/tail_events.py <user> --last 20 --verbose
```

Learner data lives under `data/` and must not be committed.

## Private lesson notes

Place markdown notes in `knowledge/docs/` on the server (this directory is gitignored for private content).  
The tutor retrieves short snippets; it does not paste the whole file every turn.
