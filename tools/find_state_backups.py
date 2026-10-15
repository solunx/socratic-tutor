#!/usr/bin/env python3
"""Scan project for learner_state dumps that look like a full george profile."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEYWORDS = ("zon", "ai_agents", "fascia", "nuclear_fusion", "harness")


def score(path: Path) -> tuple:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return (-1, path, "unreadable")
    if not isinstance(raw, dict):
        return (-1, path, "not dict")
    subs = raw.get("subjects") or {}
    keys = set(subs.keys()) if isinstance(subs, dict) else set()
    hits = sum(1 for k in KEYWORDS if k in keys or any(k in str(x) for x in keys))
    lid = (raw.get("learner") or {}).get("id")
    return (hits, path, f"id={lid} subjects={len(keys)} hits={hits} size={path.stat().st_size}")


def main():
    candidates = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        name = p.name.lower()
        if "learner_state" in name or name.endswith("_full.json") or "state_full" in name:
            if "node_modules" in str(p) or ".venv" in str(p):
                continue
            candidates.append(score(p))
    candidates.sort(key=lambda x: (-x[0], -x[1].stat().st_size if x[1].exists() else 0))
    print("Best candidates (hits on zon/ai_agents/fascia/…):")
    for hits, path, info in candidates[:20]:
        if hits < 0:
            continue
        print(f"  [{hits}] {path}")
        print(f"       {info}")
    if not candidates:
        print("None found under", ROOT)


if __name__ == "__main__":
    main()
