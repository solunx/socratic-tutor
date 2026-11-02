#!/usr/bin/env python3
"""
Does Ollama reuse the prompt prefix between requests?   (v2 — fixes a bug in v1)

BUG IN v1: it judged by `prompt_eval_count`. In your Ollama that number is the FULL prompt size even when the KV cache
is hit (your probe: 16132 tokens in every request, but 15041 ms cold vs 769 ms warm). The truth is the TIME
(`prompt_eval_duration`). v2 uses time: implied speed = tokens / seconds; ~1,100 tok/s = nothing reused,
>4,000 tok/s = cache hit.

    python tools/cache_probe.py --model qwen3.8:27b [--gap 120] [--keep-alive 30m]

Requests (num_predict=8):
  1 first            system = STATIC + DYN1, user A
  2 same system      system = STATIC + DYN1, user B      -> cache hit expected
  3 changed tail     system = STATIC + DYN2, user B      -> only the tail should be re-evaluated
  4 side call        classify-sized prompt (~400 tokens), like the tutor's classifier
  5 after side call system = STATIC + DYN2, user B       -> does an interleaved call evict the cache?
  6 after idle gap   same as 5 after --gap seconds       -> does idle time / unloading kill it? (skip with --gap 0)
Also prints /api/ps. Use --keep-alive to test whether a longer keep_alive changes request 6 and the load time.
"""
from __future__ import annotations

import argparse
import sys
import time

WORDS = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau "
         "upsilon phi chi psi omega").split()
REF_COLD_TOK_PER_S = 1100          # measured on this box: tutor main calls evaluate ~1,080 tok/s when nothing is reused
CACHE_LIKE_TOK_PER_S = 4000


def make_text(n_words: int, salt: str = "") -> str:
    return " ".join(f"{WORDS[i % len(WORDS)]}{salt}{i // len(WORDS)}" for i in range(n_words))


def speed(r: dict) -> float:
    """Implied prompt-evaluation speed in tokens/second (the truth), from the TIME, not the count."""
    return r["tokens"] / max(r["ms"], 1) * 1000.0


def hit(r: dict) -> bool:
    return speed(r) >= CACHE_LIKE_TOK_PER_S


def interpret(rs: list[dict]) -> list[str]:
    """rs = 5 or 6 dicts with 'tokens' and 'ms' (prompt_eval_duration). Pure function (unit-tested)."""
    r1, r2, r3, r4, r5 = rs[:5]
    out = []
    out.append("speeds (tok/s): " + ", ".join(f"{i + 1}:{speed(r):.0f}" for i, r in enumerate(rs))
               + f"   [~{REF_COLD_TOK_PER_S} = nothing reused, >{CACHE_LIKE_TOK_PER_S} = cache hit]")
    if not hit(r2):
        out.append("NO PREFIX REUSE for an identical system prompt. Check `ollama ps` CONTEXT (context shifting), "
                   "OLLAMA_NUM_PARALLEL, flash attention / KV-cache type, and the Ollama log.")
        return out
    out.append("prefix caching WORKS for an identical system prompt.")
    out.append("changed tail: only the tail is re-evaluated -> good." if hit(r3) else
               "a changed tail re-evaluates everything: volatile text sits too early in the prompt.")
    out.append("an interleaved side call does NOT evict the cache." if hit(r5) else
               "an interleaved side call EVICTS the cache -> OLLAMA_NUM_PARALLEL=2, or run classify/reframe on a small separate model.")
    if len(rs) > 5:
        out.append("after the idle gap the cache is STILL there." if hit(rs[5]) else
                   "after the idle gap the cache is GONE -> idle eviction/unload: raise keep_alive "
                   "(OLLAMA_KEEP_ALIVE or per-request keep_alive) and pre-warm.")
    return out


def _post(host: str, path: str, payload: dict, timeout: int = 900) -> dict:
    import requests
    r = requests.post(host.rstrip("/") + path, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def chat(host, model, system, user, think=False, keep_alive=None):
    payload = {"model": model, "stream": False, "think": think,
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
               "options": {"num_predict": 8, "temperature": 0}}
    if keep_alive:
        payload["keep_alive"] = keep_alive
    d = _post(host, "/api/chat", payload)
    return {"tokens": int(d.get("prompt_eval_count") or 0), "ms": round((d.get("prompt_eval_duration") or 0) / 1e6),
            "load_ms": round((d.get("load_duration") or 0) / 1e6)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True)
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--prefix-words", type=int, default=4500)
    ap.add_argument("--gap", type=int, default=120, help="idle seconds before request 6 (0 = skip)")
    ap.add_argument("--keep-alive", default=None, help='e.g. "30m" or "-1"')
    ap.add_argument("--think", action="store_true")
    a = ap.parse_args(argv)
    static = make_text(a.prefix_words)
    s1, s2 = static + "\nSTATE: one", static + "\nSTATE: two (changed tail)"
    side = "You classify ONE learner message. JSON only. " + make_text(300, "c")
    plan = [("1 first", s1, "question A"), ("2 same system", s1, "question B"), ("3 changed tail", s2, "question B"),
            ("4 side call", side, "hello"), ("5 after side call", s2, "question B")]
    if a.gap > 0:
        plan.append((f"6 after {a.gap}s idle", s2, "question B"))
    print(f"{'request':20s} {'tokens':>8s} {'prompt eval ms':>15s} {'tok/s':>8s} {'load ms':>8s}")
    rs = []
    for i, (name, sysm, user) in enumerate(plan):
        if i == 5:
            time.sleep(a.gap)
        r = chat(a.host, a.model, sysm, user, think=a.think, keep_alive=a.keep_alive)
        rs.append(r)
        print(f"{name:20s} {r['tokens']:>8d} {r['ms']:>15d} {speed(r):>8.0f} {r['load_ms']:>8d}")
    print()
    for line in interpret(rs):
        print("-", line)
    try:
        import requests
        for m in requests.get(a.host.rstrip("/") + "/api/ps", timeout=10).json().get("models", []):
            print(f"\n/api/ps: {m.get('name')} size_vram={m.get('size_vram')} context_length={m.get('context_length')} expires_at={m.get('expires_at')}")
    except Exception as e:
        print(f"\n(/api/ps unavailable: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
