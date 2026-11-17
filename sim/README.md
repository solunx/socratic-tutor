# Light Behaviour Lab

Collect tutor↔student conversations to **improve Socratic behaviour** (not a research platform).

## Standard

See [SOCRATIC_STANDARD.md](SOCRATIC_STANDARD.md) — 8 principles used when analysing a batch.

## Quick start

```bash
# unit / stub
python -m pytest tests/test_sim_harness.py -q
python tools/sim_run.py --dry --write-run

# live baseline (~6 profiles × 3 scenarios × 2 seeds × ≤12 turns — long on GPU)
python tools/sim_run.py --live --baseline
# → sim/runs/<run_id>/{manifest.json,sessions.jsonl,summary.json}
```

## Analyse

Pretty-print:

```bash
python3 - <<'PY'
import json
from pathlib import Path
d = sorted(Path("sim/runs").iterdir())[-1]
print("run", d)
for line in (d/"sessions.jsonl").read_text().splitlines()[:2]:
    s = json.loads(line)
    print(s["persona"], s["scenario"], s["metrics"])
PY
```

Then ask Grok/Claude: score against SOCRATIC_STANDARD, list top-3 failure modes with turn refs.

## Layout

```text
sim/personas/*.json      # 6 cognitive/behaviour profiles
sim/scenarios/*.json     # opening + truth_sheet + optional setup (/new EMS)
sim/runs/                # gitignored batch output
```
