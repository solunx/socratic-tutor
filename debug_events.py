#!/usr/bin/env python3
"""
Per-user event log for debugging multi-thread / lobby / switch UX / pedagogy.

Writes one JSON line per event under:
  data/users/<user_id>/events.jsonl

Never logs passwords. Message text is truncated (see MAX_TEXT).
Disable with TUTOR_EVENT_LOG=0.

CLI:
  python tools/tail_events.py [user_id] [--last N] [--verbose] [--switches]
  python tools/tail_events.py --list
  python -m debug_events [user_id] [--last N]
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "users"
MAX_TEXT = int(os.environ.get("TUTOR_EVENT_LOG_MAX_TEXT", "1200"))
ENABLED = os.environ.get("TUTOR_EVENT_LOG", "1") != "0"


def _path(user_id: str) -> Path:
    uid = (user_id or "unknown").strip() or "unknown"
    uid = "".join(c for c in uid if c.isalnum() or c in "-_")[:64] or "unknown"
    d = DATA_DIR / uid
    d.mkdir(parents=True, exist_ok=True)
    return d / "events.jsonl"


def list_users_with_events() -> list[str]:
    if not DATA_DIR.is_dir():
        return []
    out = []
    for d in sorted(DATA_DIR.iterdir()):
        if d.is_dir() and (d / "events.jsonl").exists():
            out.append(d.name)
    return out


def _clip(text: str | None, n: int = MAX_TEXT) -> str:
    t = (text or "").replace("\r\n", "\n").strip()
    if len(t) <= n:
        return t
    return t[: n - 1] + "…"


def log_event(user_id: str, event: str, **fields: Any) -> None:
    """Append one event. Safe to call from request handlers (best-effort)."""
    if not ENABLED:
        return
    try:
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": str(event),
            "user": user_id or "unknown",
        }
        for k, v in fields.items():
            if v is None:
                continue
            if k in ("text", "response", "message", "user_text", "assistant_text"):
                rec[k] = _clip(str(v))
            elif isinstance(v, (str, int, float, bool)):
                rec[k] = v
            else:
                try:
                    rec[k] = json.loads(json.dumps(v, default=str))
                except Exception:
                    rec[k] = str(v)[:500]
        p = _path(user_id)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
        try:
            mirror = BASE_DIR / "metrics" / "events_all.jsonl"
            mirror.parent.mkdir(parents=True, exist_ok=True)
            with open(mirror, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass
    except Exception:
        pass


def log_turn(
    user_id: str,
    *,
    message: str,
    thread_from: str | None,
    thread_to: str | None,
    result: dict | None,
) -> None:
    """Log one completed tutor turn (after gateway has final thread_id)."""
    r = result or {}
    routing = r.get("routing") if isinstance(r.get("routing"), dict) else {}
    policy = r.get("policy") if isinstance(r.get("policy"), dict) else {}
    assessment = r.get("assessment") if isinstance(r.get("assessment"), dict) else {}
    stats = r.get("stats") if isinstance(r.get("stats"), dict) else {}
    log_event(
        user_id,
        "turn",
        thread_from=thread_from or "",
        thread_to=thread_to or "",
        switched=bool(
            thread_from
            and thread_to
            and str(thread_from) != str(thread_to)
        ),
        kind=r.get("kind") or "",
        ok=bool(r.get("ok", True)),
        routing_action=str(routing.get("action") or ""),
        subject_id=r.get("subject_id") or routing.get("subject") or "",
        policy=policy or None,
        policy_activity=str(policy.get("activity") or ""),
        policy_force=bool(policy.get("force")) if policy else None,
        policy_reason=_clip(str(policy.get("reason") or ""), 240) or None,
        turn_intent=r.get("turn_intent") or stats.get("turn_intent") or None,
        turn_correctness=r.get("turn_correctness") or stats.get("turn_correctness") or None,
        premises=r.get("premises") if r.get("premises") is not None else None,
        gate_repair=stats.get("gate_repair"),
        gate_repair_failed=stats.get("gate_repair_failed"),
        echo_repair=stats.get("echo_repair"),
        echo_skipped=stats.get("echo_skipped"),
        gate_skipped=stats.get("gate_skipped"),
        reframe=stats.get("reframe"),
        reframe_failed=stats.get("reframe_failed"),
        draft=_clip(stats.get("draft"), 1500) if stats.get("draft") else None,
        draft_question=stats.get("draft_question"),
        judge_called=stats.get("judge_called"),
        judge_style=stats.get("judge_style"),
        judge_truncated=stats.get("judge_truncated"),
        judge_analysis=(_clip(str((stats.get("judge") or {}).get("analysis") or ""), 400) or None) if isinstance(stats.get("judge"), dict) else None,
        echo_flagged=stats.get("echo_flagged"),
        judge_failed=stats.get("judge_failed"),
        judge_verdict=(stats.get("judge") or {}).get("verdict") if isinstance(stats.get("judge"), dict) else None,
        judge_reason=(stats.get("judge") or {}).get("reason") if isinstance(stats.get("judge"), dict) else None,
        judge_missing=(stats.get("judge") or {}).get("missing") if isinstance(stats.get("judge"), dict) else None,
        judge_support=_clip((stats.get("judge") or {}).get("support"), 200) if isinstance(stats.get("judge"), dict) and (stats.get("judge") or {}).get("support") else None,
        judge_support_verified=(stats.get("judge") or {}).get("support_verified") if isinstance(stats.get("judge"), dict) else None,
        judge_downgraded=stats.get("judge_downgraded"),
        judge_retry=stats.get("judge_retry"),
        judge_claim=(stats.get("judge") or {}).get("claim") if isinstance(stats.get("judge"), dict) else None,
        judge_responsive=(stats.get("judge") or {}).get("responsive") if isinstance(stats.get("judge"), dict) else None,
        judge_evidence=stats.get("judge_evidence"),
        off_target_streak=stats.get("off_target_streak"),
        concede_repair_failed=stats.get("concede_repair_failed"),
        empty_fallback=stats.get("empty_fallback"),
        reframe_repeat=stats.get("reframe_repeat"),
        correction_gate=stats.get("correction_gate") or assessment.get("correction_gate"),
        assessment_activity=str(assessment.get("activity") or "") or None,
        verdict=str(assessment.get("verdict") or assessment.get("last_verdict") or "") or None,
        message=_clip(message),
        response=_clip(r.get("response") or ""),
        error=_clip(r.get("error")) if r.get("error") else None,
        response_chars=len(str(r.get("response") or "")),
    )


def read_events(user_id: str, last: int = 50) -> list[dict]:
    p = _path(user_id)
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[-max(1, last) :]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"raw": line[:200]})
    return out


def format_events(events: list[dict], *, verbose: bool = False, switches_only: bool = False) -> str:
    rows = []
    for e in events:
        if switches_only and not (
            e.get("switched") or e.get("event") in ("login", "logout", "switch")
        ):
            if e.get("event") == "turn" and e.get("routing_action") in (
                "switch", "new_subject", "list_subjects"
            ):
                pass
            elif e.get("event") != "turn" or not e.get("switched"):
                continue
        ts = e.get("ts", "")
        ev = e.get("event", "")
        if ev == "turn":
            sw = " SWITCH" if e.get("switched") else ""
            pol = e.get("policy_activity") or "-"
            if e.get("policy_force"):
                pol += "(force)"
            rows.append(
                f"{ts}  turn{sw}  {e.get('thread_from','')} → {e.get('thread_to','')}  "
                f"kind={e.get('kind')}  route={e.get('routing_action') or '-'}  "
                f"policy={pol}"
            )
            if e.get("policy_reason") and (verbose or e.get("policy_force")):
                rows.append(f"         policy_reason: {e['policy_reason'][:180]}")
            if e.get("verdict") or e.get("assessment_activity"):
                rows.append(
                    f"         assess: activity={e.get('assessment_activity') or '-'} "
                    f"verdict={e.get('verdict') or '-'}"
                )
            if e.get("turn_intent") or e.get("turn_correctness"):
                rows.append(
                    f"         intent={e.get('turn_intent') or '-'} "
                    f"correctness={e.get('turn_correctness') or '-'}"
                )
            gates = []
            if e.get("judge_verdict"):
                rows.append(f"         judge: {e.get('judge_verdict')}"
                            + (f" (claim={e.get('judge_claim')}, responsive={e.get('judge_responsive')})" if e.get("judge_claim") else "")
                            + f" - {str(e.get('judge_reason') or '')[:120]}"
                            + (f"  | missing: {str(e.get('judge_missing'))[:80]}" if e.get("judge_missing") else ""))
            if e.get("judge_analysis") and verbose:
                rows.append(f"         judge analysis: {str(e['judge_analysis'])[:240]}")
            for gk in ("gate_repair", "gate_repair_failed", "echo_flagged", "echo_repair", "echo_skipped", "gate_skipped",
                       "reframe", "reframe_failed", "judge_failed", "judge_truncated", "correction_gate",
                       "off_target_streak", "concede_repair_failed", "empty_fallback", "reframe_repeat",
                       "judge_downgraded", "judge_retry"):
                if e.get(gk) is not None:
                    gates.append(f"{gk}={e.get(gk)}")
            if gates:
                rows.append("         gates: " + ", ".join(gates))
            if e.get("draft") and (verbose or e.get("gate_repair") or e.get("reframe")):
                rows.append(f"         DRAFT (replaced): {str(e['draft'])[:300]}")
            if e.get("premises") and verbose:
                rows.append(f"         premises: {e.get('premises')}")
            msg_n = 400 if verbose else 180
            if e.get("message"):
                rows.append(f"         YOU: {e['message'][:msg_n]}")
            if e.get("response"):
                rows.append(
                    f"         BOT({e.get('response_chars') or '?'}c): "
                    f"{e['response'][:msg_n]}"
                )
        elif ev in ("login", "logout"):
            rows.append(f"{ts}  {ev}  user={e.get('user')}  ip={e.get('ip') or '-'}")
        else:
            extra = {k: v for k, v in e.items() if k not in ("ts", "event", "user")}
            rows.append(f"{ts}  {ev}  " + json.dumps(extra, ensure_ascii=False)[:300])
    return "\n".join(rows)


def ping(user_id: str, note: str = "debug_events.ping") -> str:
    log_event(user_id, "ping", note=note)
    return str(_path(user_id))


if __name__ == "__main__":
    import sys
    args = [a for a in sys.argv[1:] if a]
    user = "george"
    last = 40
    verbose = "--verbose" in args or "-v" in args
    switches = "--switches" in args
    if "--list" in args:
        print("users with events:", ", ".join(list_users_with_events()) or "(none)")
        raise SystemExit(0)
    args = [a for a in args if a not in ("--verbose", "-v", "--switches", "--list")]
    if args and not args[0].startswith("-"):
        user = args.pop(0)
    if "--last" in args:
        i = args.index("--last")
        last = int(args[i + 1])
    ev = read_events(user, last=last)
    print(f"# {user}  {_path(user)}  (last {last})")
    if not ev:
        print("(empty — use the web UI once, then re-run)")
    else:
        print(format_events(ev, verbose=verbose, switches_only=switches))
