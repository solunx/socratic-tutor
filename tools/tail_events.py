#!/usr/bin/env python3
"""Tail per-user tutor event log (debugging switches / lobby / intake / pedagogy).

Examples:
  python tools/tail_events.py vriend1 --last 80
  python tools/tail_events.py george --verbose --last 30
  python tools/tail_events.py george --switches
  python tools/tail_events.py --list
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from debug_events import (  # noqa: E402
    format_events,
    list_users_with_events,
    read_events,
    _path,
)


def main():
    args = [a for a in sys.argv[1:] if a]
    if "--list" in args:
        users = list_users_with_events()
        print("users with events:", ", ".join(users) if users else "(none)")
        return

    user = "george"
    last = 40
    verbose = "--verbose" in args or "-v" in args
    switches = "--switches" in args
    args = [a for a in args if a not in ("--verbose", "-v", "--switches")]
    if args and not args[0].startswith("-"):
        user = args.pop(0)
    if "--last" in args:
        i = args.index("--last")
        last = int(args[i + 1])

    ev = read_events(user, last=last)
    print(f"# {user}  {_path(user)}  (last {last})")
    if not ev:
        print("(empty — use the web UI once, then re-run)")
        print("hint: python tools/tail_events.py --list")
        return
    print(format_events(ev, verbose=verbose, switches_only=switches))


if __name__ == "__main__":
    main()
