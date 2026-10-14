"""
Simple local multi-user auth for the tutor gateway.

- PBKDF2-SHA256 password hashes (stdlib only)
- Signed session tokens (HMAC) in an HTTP-only cookie
- User registry: data/users.json
- Per-user data: data/users/<user_id>/{learner_state.json, web_conversation.json}

Suitable for friends on Tailscale, or on the public internet **only** behind
TLS (Caddy / Cloudflare Tunnel / Tailscale Funnel) with strong unique passwords.
There is no self-signup: accounts are created by the admin (`tools/create_user.py`).
See docs/PUBLIC_ACCESS.md.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
USERS_FILE = DATA_DIR / "users.json"
SECRET_FILE = DATA_DIR / "session_secret.key"

# Cookie
COOKIE_NAME = "tutor_session"
SESSION_DAYS = int(os.environ.get("TUTOR_SESSION_DAYS", "30"))

# Username: lowercase letters, digits, underscore, hyphen; 2–32 chars
USER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_secret() -> bytes:
    _ensure_data_dir()
    if SECRET_FILE.exists():
        return SECRET_FILE.read_bytes().strip()
    secret = secrets.token_bytes(32)
    SECRET_FILE.write_bytes(secret)
    try:
        SECRET_FILE.chmod(0o600)
    except OSError:
        pass
    return secret


def load_users() -> dict[str, Any]:
    _ensure_data_dir()
    if not USERS_FILE.exists():
        return {"version": 1, "users": {}}
    try:
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"version": 1, "users": {}}
        data.setdefault("version", 1)
        data.setdefault("users", {})
        return data
    except Exception:
        return {"version": 1, "users": {}}


def save_users(data: dict) -> None:
    _ensure_data_dir()
    tmp = USERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(USERS_FILE)
    try:
        USERS_FILE.chmod(0o600)
    except OSError:
        pass


def hash_password(password: str, *, iterations: int = 200_000) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
    )
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt, digest = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iters_s)
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
        )
        return hmac.compare_digest(dk.hex(), digest)
    except Exception:
        return False


def valid_username(username: str) -> bool:
    return bool(USER_RE.match((username or "").strip().lower()))


def create_user(username: str, password: str, display_name: str | None = None) -> str:
    """Create a user. Returns normalized user_id. Raises ValueError on bad input."""
    uid = (username or "").strip().lower()
    if not valid_username(uid):
        raise ValueError(
            "Username: 2–32 chars, start with letter/digit, only a-z 0-9 _ -"
        )
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    data = load_users()
    if uid in data["users"]:
        raise ValueError(f"User already exists: {uid}")
    data["users"][uid] = {
        "password_hash": hash_password(password),
        "display_name": (display_name or uid).strip()[:64],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    save_users(data)
    user_dir(uid).mkdir(parents=True, exist_ok=True)
    return uid


def verify_user(username: str, password: str) -> str | None:
    """Return user_id on success, else None."""
    uid = (username or "").strip().lower()
    data = load_users()
    entry = (data.get("users") or {}).get(uid)
    if not entry:
        return None
    if verify_password(password, entry.get("password_hash") or ""):
        return uid
    return None


def user_dir(user_id: str) -> Path:
    return DATA_DIR / "users" / user_id


def state_path(user_id: str) -> Path:
    return user_dir(user_id) / "learner_state.json"


def conv_path(user_id: str) -> Path:
    return user_dir(user_id) / "web_conversation.json"


def list_usernames() -> list[str]:
    return sorted((load_users().get("users") or {}).keys())


def ensure_default_user(username: str = "default", password: str | None = None) -> str:
    """
    Ensure at least one user exists. If registry empty and legacy learner_state.json
    exists, create a default user and leave migration of files to migrate_legacy_files().
    Password defaults to a random one printed once — or set TUTOR_BOOTSTRAP_PASSWORD.
    """
    data = load_users()
    if data.get("users"):
        return username if username in data["users"] else next(iter(data["users"]))
    pw = password or os.environ.get("TUTOR_BOOTSTRAP_PASSWORD") or secrets.token_urlsafe(12)
    try:
        create_user(username, pw, display_name=username)
        print(f"[auth] Created bootstrap user '{username}'. Password: {pw}")
        print("[auth] Change it with: python tools/create_user.py --reset-password", username)
    except ValueError:
        pass
    return username


def migrate_legacy_files(user_id: str = "default") -> list[str]:
    """Move root learner_state.json / web_conversation.json into user dir if needed."""
    notes = []
    ud = user_dir(user_id)
    ud.mkdir(parents=True, exist_ok=True)
    legacy_state = ROOT / "learner_state.json"
    legacy_conv = ROOT / "web_conversation.json"
    target_state = state_path(user_id)
    target_conv = conv_path(user_id)
    if legacy_state.exists() and not target_state.exists():
        legacy_state.replace(target_state)
        notes.append(f"moved learner_state.json → {target_state.relative_to(ROOT)}")
    if legacy_conv.exists() and not target_conv.exists():
        legacy_conv.replace(target_conv)
        notes.append(f"moved web_conversation.json → {target_conv.relative_to(ROOT)}")
    return notes


def make_session_token(user_id: str, days: int = SESSION_DAYS) -> str:
    exp = int(time.time()) + days * 86400
    payload = f"{user_id}:{exp}"
    sig = hmac.new(get_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def parse_session_token(token: str | None) -> str | None:
    if not token or token.count(":") < 2:
        return None
    try:
        user_id, exp_s, sig = token.rsplit(":", 2)
        exp = int(exp_s)
        if exp < int(time.time()):
            return None
        payload = f"{user_id}:{exp_s}"
        expect = hmac.new(get_secret(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, sig):
            return None
        if user_id not in (load_users().get("users") or {}):
            return None
        return user_id
    except Exception:
        return None
