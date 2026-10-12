#!/usr/bin/env python3
"""
LLM call observability for the Socratic tutor.

- Estimates tokens per prompt component (chars/4 + optional Ollama /api/tokenize)
- Logs one JSON line per call to metrics/llm_calls.jsonl
- Never logs full prompt text by default (only lengths + optional hash)

Usage from code:
  from metrics_lib import attach_breakdown, log_call_from_stats, format_breakdown

CLI:
  python metrics_lib.py breakdown          # dry breakdown on current state
  python metrics_lib.py summary            # aggregate last N log lines
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
METRICS_DIR = BASE_DIR / "metrics"
METRICS_LOG = METRICS_DIR / "llm_calls.jsonl"

# Set METRICS_ENABLED=0 to disable disk logging
METRICS_ENABLED = os.environ.get("METRICS_ENABLED", "1") != "0"
METRICS_STORE_PROMPT_HASH = True


def estimate_tokens(text: str) -> int:
    """Rough token estimate without a model-specific tokenizer (~4 chars/token)."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def ollama_token_count(text: str, model: str, host: str) -> int | None:
    """Exact count via Ollama /api/tokenize when available."""
    if not text:
        return 0
    try:
        import requests

        r = requests.post(
            f"{host.rstrip('/')}/api/tokenize",
            json={"model": model, "content": text},
            timeout=30,
        )
        if r.status_code != 200:
            # older ollama: try prompt field
            r = requests.post(
                f"{host.rstrip('/')}/api/tokenize",
                json={"model": model, "prompt": text},
                timeout=30,
            )
        if r.status_code != 200:
            return None
        data = r.json()
        tokens = data.get("tokens")
        if isinstance(tokens, list):
            return len(tokens)
        if isinstance(data.get("count"), int):
            return data["count"]
    except Exception:
        return None
    return None


def component_stats(name: str, text: str, model: str | None = None, host: str | None = None) -> dict:
    text = text or ""
    est = estimate_tokens(text)
    row = {
        "name": name,
        "chars": len(text),
        "tokens_est": est,
    }
    if model and host and len(text) < 200_000:
        exact = ollama_token_count(text, model, host)
        if exact is not None:
            row["tokens"] = exact
    return row


def prompt_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def ensure_metrics_dir():
    METRICS_DIR.mkdir(parents=True, exist_ok=True)


def append_metric(record: dict) -> None:
    if not METRICS_ENABLED:
        return
    ensure_metrics_dir()
    record = dict(record)
    record.setdefault("ts", datetime.now().isoformat())
    with open(METRICS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def format_breakdown(components: list[dict], total_est: int | None = None) -> str:
    lines = ["Prompt component breakdown (est. tokens ≈ chars/4):"]
    for c in components:
        tok = c.get("tokens", c.get("tokens_est", 0))
        lines.append(f"  {c['name']:22s}  chars={c['chars']:6d}  tokens≈{tok:5d}")
    if total_est is not None:
        lines.append(f"  {'TOTAL_EST':22s}  tokens≈{total_est:5d}")
    return "\n".join(lines)


def snapshot_vram() -> dict | None:
    """Optional nvidia-smi snapshot (best effort)."""
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=3,
        )
        line = out.strip().splitlines()[0]
        used, total, util = [x.strip() for x in line.split(",")]
        return {
            "vram_used_mb": int(float(used)),
            "vram_total_mb": int(float(total)),
            "gpu_util_pct": int(float(util)),
        }
    except Exception:
        return None


def build_breakdown_from_parts(parts: dict, conversation: list | None = None) -> dict:
    """
    parts: named text fragments that form the system message.
    conversation: list of {role, content}
    """
    components = []
    for name, text in parts.items():
        if text is None:
            continue
        components.append(component_stats(name, str(text)))

    hist_chars = 0
    hist_msgs = 0
    if conversation:
        for m in conversation:
            if isinstance(m, dict):
                hist_chars += len(str(m.get("content") or ""))
                hist_msgs += 1
        components.append(
            component_stats("conversation_history", "x" * hist_chars if hist_chars else "")
        )
        # fix chars to real
        components[-1]["chars"] = hist_chars
        components[-1]["tokens_est"] = estimate_tokens("x" * hist_chars) if hist_chars else 0
        components[-1]["messages"] = hist_msgs

    total_est = sum(c.get("tokens_est", 0) for c in components)
    return {
        "components": components,
        "total_tokens_est": total_est,
        "n_history_messages": hist_msgs,
    }


# last breakdown attached by tutor.build_messages
_last_breakdown: dict | None = None


def set_last_breakdown(b: dict | None):
    global _last_breakdown
    _last_breakdown = b


def get_last_breakdown() -> dict | None:
    return _last_breakdown


def log_call_from_stats(stats: dict | None, breakdown: dict | None = None, extra: dict | None = None):
    """Merge Ollama stats + prompt breakdown into one jsonl record."""
    breakdown = breakdown if breakdown is not None else _last_breakdown
    stats = stats or {}
    rec = {
        "event": "llm_call",
        "model": stats.get("model"),
        "streamed": stats.get("streamed"),
        "think": stats.get("think_requested"),
        "json_retry": stats.get("json_retry"),
        "prompt_eval_count": stats.get("prompt_eval_count"),
        "eval_count": stats.get("eval_count"),
        "total_s": stats.get("total_s"),
        "ttft_s": stats.get("ttft_s"),
        "prompt_eval_s": stats.get("prompt_eval_s"),
        "eval_s": stats.get("eval_s"),
        "load_s": stats.get("load_s"),
        "out_tok_per_s": stats.get("out_tok_per_s"),
    }
    if breakdown:
        rec["total_tokens_est"] = breakdown.get("total_tokens_est")
        rec["components"] = [
            {
                "name": c["name"],
                "chars": c["chars"],
                "tokens_est": c.get("tokens_est"),
                "tokens": c.get("tokens"),
            }
            for c in breakdown.get("components") or []
        ]
        if METRICS_STORE_PROMPT_HASH and breakdown.get("system_hash"):
            rec["system_hash"] = breakdown["system_hash"]
        if breakdown.get("prefix_hash"):
            rec["prefix_hash"] = breakdown["prefix_hash"]
            rec["prefix_chars"] = breakdown.get("prefix_chars")
            rec["dynamic_chars"] = breakdown.get("dynamic_chars")
    vram = snapshot_vram()
    if vram:
        rec["gpu"] = vram
    if extra:
        rec.update(extra)
    append_metric(rec)
    return rec


def print_summary(n: int = 20):
    if not METRICS_LOG.exists():
        print("No metrics log yet:", METRICS_LOG)
        return
    lines = METRICS_LOG.read_text(encoding="utf-8").strip().splitlines()
    rows = []
    for line in lines[-n:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not rows:
        print("No records.")
        return
    print(f"Last {len(rows)} LLM calls:")
    print(f"{'ts':20s} {'in':>6} {'out':>5} {'s':>6} {'ttft':>5} {'est':>6}")
    for r in rows:
        ts = (r.get("ts") or "")[:19]
        inn = r.get("prompt_eval_count") or 0
        out = r.get("eval_count") or 0
        tot = r.get("total_s")
        ttft = r.get("ttft_s")
        est = r.get("total_tokens_est")
        print(
            f"{ts:20s} {inn:6} {out:5} "
            f"{(tot if tot is not None else 0):6.1f} "
            f"{(ttft if ttft is not None else 0):5.2f} "
            f"{(est or 0):6}"
        )
    # average component share from last call with components
    for r in reversed(rows):
        comps = r.get("components") or []
        if comps:
            print("\nLast call component share (est):")
            total = sum(c.get("tokens_est") or 0 for c in comps) or 1
            for c in sorted(comps, key=lambda x: -(x.get("tokens_est") or 0)):
                te = c.get("tokens_est") or 0
                pct = 100.0 * te / total
                print(f"  {c['name']:22s} {te:5d}  ({pct:5.1f}%)")
            break


def cli_breakdown():
    """Dry-run: build messages for current learner state and print sizes."""
    # When run as __main__, tutor imports this file as module "metrics_lib"
    # (separate from __main__). Read breakdown from that module object.
    import metrics_lib as ml
    import tutor as T

    state = T.load_state()
    base = T.load_system_prompt()
    policy = T.suggest_next_activity(state)
    messages = T.build_messages(base, state, [], policy=policy)
    b = ml.get_last_breakdown()
    if not b:
        print("No breakdown attached (build_messages should set it).")
        sys_msg = messages[0]["content"] if messages else ""
        print(f"system chars={len(sys_msg)} tokens_est≈{estimate_tokens(sys_msg)}")
        return
    print(format_breakdown(b["components"], b.get("total_tokens_est")))
    print(f"\nsystem_hash={b.get('system_hash')}")
    print(f"mode={b.get('mode')} policy={policy.get('activity')}")


if __name__ == "__main__":
    import sys

    cmd = (sys.argv[1] if len(sys.argv) > 1 else "summary").lower()
    if cmd in ("breakdown", "break", "dry"):
        cli_breakdown()
    elif cmd in ("summary", "sum", "log"):
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        print_summary(n)
    else:
        print("Usage: python metrics_lib.py [breakdown|summary] [n]")
