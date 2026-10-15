#!/usr/bin/env python3
"""Tail per-user tutor event log (debugging switches / lobby / intake)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from debug_events import format_events, read_events, _path  # noqa: E402


def main():
    args = [a for a in sys.argv[1:] if a]
    user = "george"
    last = 40
    if args and not args[0].startswith("-"):
        user = args.pop(0)
    if "--last" in args:
        i = args.index("--last")
        last = int(args[i + 1])
    ev = read_events(user, last=last)
    print(f"# {user}  {_path(user)}  (last {last})")
    if not ev:
        print("(empty — use the web UI once, then re-run)")
        return
    print(format_events(ev))


if __name__ == "__main__":
    main()
