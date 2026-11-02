#!/usr/bin/env python3
"""
Manual check of Phase B0 evidence packs (Wikipedia + local docs).

  python tools/knowledge_probe.py "pp-keten stappen"
  python tools/knowledge_probe.py "Coulomb barrière" --no-wiki
  python tools/knowledge_probe.py "proton-proton chain" --no-local

Writes nothing. Needs network for Wikipedia.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", help="claim or topic to gather evidence for")
    ap.add_argument("--no-wiki", action="store_true")
    ap.add_argument("--no-local", action="store_true")
    ap.add_argument("--json", action="store_true", help="print raw JSON pack")
    a = ap.parse_args(argv)

    from knowledge.verify import evidence_for, format_evidence_for_prompt

    pack = evidence_for(
        a.query,
        use_wiki=not a.no_wiki,
        use_local=not a.no_local,
    )
    if a.json:
        print(json.dumps(pack, ensure_ascii=False, indent=2))
        return 0

    print(f"query: {pack['query']!r}")
    print(f"status: {pack['status']}   snippets: {len(pack['snippets'])}")
    for i, s in enumerate(pack["snippets"], 1):
        print(f"\n--- {i}. [{s.get('kind')}] {s.get('title')}  ({s.get('source')})")
        if s.get("url"):
            print(f"    {s['url']}")
        text = (s.get("text") or "")[:500]
        print(f"    {text}{'…' if len(s.get('text') or '') > 500 else ''}")
    block = format_evidence_for_prompt(pack)
    if block:
        print("\n=== prompt block (truncated) ===")
        print(block[:1200])
    return 0 if pack["status"] != "not_found" else 1


if __name__ == "__main__":
    sys.exit(main())
