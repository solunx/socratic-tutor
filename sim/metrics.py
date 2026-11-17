"""Aggregate metrics from simulated sessions (no manual transcript reading required)."""
from __future__ import annotations

from collections import Counter
from typing import Any


def turn_metrics(record: dict[str, Any]) -> dict[str, Any]:
    res = record.get("tutor_result") or {}
    stats = res.get("stats") or {}
    judge = stats.get("judge")
    verdict = None
    if isinstance(judge, dict):
        verdict = judge.get("verdict")
    else:
        verdict = stats.get("judge_verdict")
    return {
        "ok": bool(res.get("ok", True)),
        "kind": res.get("kind"),
        "intent_planned": record.get("intent"),
        "subject_id": res.get("subject_id") or record.get("subject_id"),
        "judge_verdict": verdict,
        "blocked_new_subject": bool(stats.get("blocked_new_subject") or stats.get("subject_choice")),
        "subject_choice": res.get("kind") == "subject_choice" or bool(stats.get("subject_choice")),
        "response_len": len(str(res.get("response") or "")),
        "has_sources_footer": "Bronnen:" in str(res.get("response") or ""),
        "sec": (stats.get("timing") or {}).get("total_s") if isinstance(stats.get("timing"), dict) else None,
    }


def _false_reject(record: dict[str, Any]) -> bool:
    if record.get("intent") != "answer":
        return False
    stats = (record.get("tutor_result") or {}).get("stats") or {}
    judge = stats.get("judge")
    if isinstance(judge, dict):
        return judge.get("verdict") == "incorrect"
    return False


def summarize_metrics(session_rows: list[dict[str, Any]]) -> dict[str, Any]:
    turns = [turn_metrics(r) for r in session_rows]
    kinds = Counter(t.get("kind") for t in turns)
    intents = Counter(t.get("intent_planned") for t in turns)
    n = max(len(turns), 1)
    return {
        "n_turns": len(turns),
        "n_ok": sum(1 for t in turns if t.get("ok")),
        "kinds": dict(kinds),
        "intents": dict(intents),
        "subject_choice_rate": sum(1 for t in turns if t.get("subject_choice")) / n,
        "sources_footer_rate": sum(1 for t in turns if t.get("has_sources_footer")) / n,
        "mean_response_len": sum(t.get("response_len") or 0 for t in turns) / n,
        "false_reject_candidates": sum(1 for r in session_rows if _false_reject(r)),
    }
