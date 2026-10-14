#!/usr/bin/env python3
"""Create or reset a tutor user (local multi-user).

  python tools/create_user.py alice
  python tools/create_user.py alice --password 'secretpass'
  python tools/create_user.py alice --reset-password
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import auth


def main():
    p = argparse.ArgumentParser(description="Create/reset tutor users")
    p.add_argument("username", help="login name (a-z 0-9 _ -)")
    p.add_argument("--password", help="password (prompt if omitted)")
    p.add_argument("--display-name", default=None)
    p.add_argument("--reset-password", action="store_true", help="reset existing user password")
    p.add_argument("--migrate-legacy", action="store_true", help="move root state into this user dir")
    args = p.parse_args()

    uid = args.username.strip().lower()
    if args.migrate_legacy:
        notes = auth.migrate_legacy_files(uid)
        print("migrate:", notes or "nothing to move")

    pw = args.password
    if not pw:
        pw = getpass.getpass("Password: ")
        pw2 = getpass.getpass("Again: ")
        if pw != pw2:
            print("Passwords do not match")
            sys.exit(1)

    if args.reset_password:
        data = auth.load_users()
        if uid not in data["users"]:
            print(f"Unknown user: {uid}")
            sys.exit(1)
        data["users"][uid]["password_hash"] = auth.hash_password(pw)
        auth.save_users(data)
        print(f"Password updated for {uid}")
        return

    try:
        auth.create_user(uid, pw, display_name=args.display_name)
        print(f"Created user: {uid}")
        print(f"Data dir: {auth.user_dir(uid)}")
    except ValueError as e:
        print(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
