#!/usr/bin/env python3
"""Show which learner_state files exist and which subjects they contain."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "users"
LEGACY = ROOT / "learner_state.json"


def summarize(path: Path) -> str:
    if not path.exists():
        return "(missing)"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return f"(read error: {e})"
    lid = (raw.get("learner") or {}).get("id")
    subs = raw.get("subjects") or {}
    titles = []
    for sid, s in list(subs.items())[:12]:
        if isinstance(s, dict):
            titles.append(s.get("title") or sid)
        else:
            titles.append(sid)
    more = f" (+{len(subs)-12})" if len(subs) > 12 else ""
    return f"learner.id={lid!r}  subjects={len(subs)}  [{', '.join(titles)}{more}]"


def main():
    print("=== Legacy root ===")
    print(f"  {LEGACY}")
    print(f"    {summarize(LEGACY)}")
    print("=== data/users/* ===")
    if not DATA.exists():
        print("  (no data/users dir)")
        return
    for d in sorted(DATA.iterdir()):
        if not d.is_dir():
            continue
        p = d / "learner_state.json"
        print(f"  {d.name}/")
        print(f"    state: {summarize(p)}")
        conv = d / "web_conversation.json"
        if conv.exists():
            try:
                c = json.loads(conv.read_text(encoding="utf-8"))
                th = c.get("threads") or {}
                print(f"    threads: {list(th.keys())[:15]}")
            except Exception as e:
                print(f"    threads: (error {e})")
        bak = list(d.glob("learner_state.json.bak*")) + list(ROOT.glob("learner_state.json.bak*"))
        for b in bak[:5]:
            print(f"    bak: {b.name} → {summarize(b)}")


if __name__ == "__main__":
    main()
