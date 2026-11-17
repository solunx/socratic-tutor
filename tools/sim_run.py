#!/usr/bin/env python3
"""Light Behaviour Lab runner — collect tutor↔student chats for later analysis.

  # Dry (no GPU)
  python tools/sim_run.py --dry

  # Baseline-style live batch → sim/runs/<id>/
  python tools/sim_run.py --live --baseline --max-turns 12

  # Custom
  python tools/sim_run.py --live --personas beginner,critical \\
    --scenarios ems_eco_basics,ems_ev_boost_under_parent --seeds 0,1 --write-run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASELINE_PERSONAS = [
    "beginner", "struggling", "partial_understanding",
    "confident_wrong", "critical", "frustrated",
]
BASELINE_SCENARIOS = [
    "ems_eco_basics",
    "ems_ev_boost_under_parent",
    "open_topic_warmtepomp",
]


def stub_handle_turn(message, state=None, conversation=None, **kwargs):
    conversation = conversation if conversation is not None else []
    conversation.append({"role": "user", "content": message})
    low = message.lower()
    if low.startswith("/new"):
        reply = "Nieuw onderwerp: EMS. Wat wil je hiermee bereiken?"
        kind = "new_subject"
        sid = "ems"
    elif "hernoem" in low or "onderwerp is" in low:
        reply = "Onderwerp hernoemd naar «EMS»."
        kind = "message"
        sid = "ems"
    else:
        reply = "Stub: wat denk je dat de volgende stap is, en waarom?"
        kind = "message"
        sid = "ems"
    conversation.append({"role": "assistant", "content": reply})
    return {"ok": True, "kind": kind, "response": reply, "subject_id": sid, "stats": {}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--baseline", action="store_true", help="6 personas × 3 scenarios × seeds 0,1")
    ap.add_argument("--personas", default=None)
    ap.add_argument("--scenarios", default=None)
    ap.add_argument("--seeds", default="0,1")
    ap.add_argument("--max-turns", type=int, default=None)
    ap.add_argument("--write-run", action="store_true", help="write sim/runs/<id>/")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--out", default=None, help="also write single JSON report path")
    args = ap.parse_args(argv)

    if not args.dry and not args.live:
        args.dry = True

    if args.baseline:
        personas = BASELINE_PERSONAS
        scenarios = BASELINE_SCENARIOS
        args.write_run = True
        if args.max_turns is None:
            args.max_turns = 12
    else:
        personas = [x.strip() for x in (args.personas or "beginner,critical").split(",") if x.strip()]
        scenarios = [x.strip() for x in (args.scenarios or "ems_eco_basics").split(",") if x.strip()]

    seeds = [int(x) for x in args.seeds.split(",") if x.strip() != ""]

    from sim.runner import run_batch

    ht = stub_handle_turn if args.dry else None
    report = run_batch(
        persona_names=personas,
        scenario_names=scenarios,
        seeds=seeds,
        handle_turn=ht,
        max_turns=args.max_turns,
        run_id=args.run_id,
        write_run=args.write_run or bool(args.out),
    )
    # if --out only, still write one file
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)

    print(json.dumps({
        "n_sessions": report["n_sessions"],
        "summary": report["summary"],
        "run_id": report.get("run_id"),
        "run_dir": report.get("run_dir"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
