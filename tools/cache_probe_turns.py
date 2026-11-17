#!/usr/bin/env python3
"""
REAL-FLOW cache probe: do the tutor's own side calls (classify, judge) evict the prompt cache of the MAIN call?

Evidence so far (gpu01): the simple probe and the production-shaped probe both HIT (7.3k tokens in 0.7 s), a replay of a real session shows
~80% of the prompt is byte-identical between consecutive turns, yet in the logs real main calls hit only ~10% of the time and
0 of 33 since the classifier exists. What the earlier probes did NOT do: run the REAL classify and judge calls (real system prompts,
real JSON schema, judge with its own options) between the main calls. This tool does exactly that, per variant:

  normal        classify + judge + main      (what a real turn does)
  main_only     main                          (control)
  classify_only classify + main
  judge_only    judge + main

3 rounds per variant on a temp COPY of the user's data (nothing is written to your data); main uses num_predict=8 (only the prompt
evaluation matters). Compare the main call's speed in rounds 2-3:   >4000 tok/s = cache hit.

    python tools/cache_probe_turns.py --user george [--host http://localhost:11434] [--rounds 3]
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

CACHE_LIKE = 4000
VARIANTS = [("main_only", False, False), ("normal", True, True), ("classify_only", True, False), ("judge_only", False, True)]


def speed(r):
    return (r["tokens"] / r["sec"]) if r.get("sec") and r.get("tokens") else 0.0


def interpret_turns(res: dict[str, list[dict]]) -> list[str]:
    """res: variant -> list of main-call results per round ({'tokens','sec'}). Judges rounds 2.. only. Pure (unit-tested)."""
    def share(v):
        rs = res.get(v, [])[1:] or res.get(v, [])
        return sum(1 for r in rs if speed(r) >= CACHE_LIKE) / max(len(rs), 1)
    out = ["main-call hit share in rounds 2+: " + ", ".join(f"{v}: {share(v):.0%}" for v in res)]
    base, normal = share("main_only"), share("normal")
    if base < 0.5:
        out.append("even the main-only control MISSES: the prompt itself (or the server) defeats reuse in this flow; compare with "
                   "cache_probe_prod and read the Ollama debug log.")
        return out
    if normal >= 0.5:
        out.append("the REAL flow (classify + judge + main) HITS: side calls do not evict. The misses in real use come from outside this "
                   "process (other clients of the same Ollama, the gateway's environment, another model loaded). Check "
                   "`systemctl cat ollama tutor-gateway`, OLLAMA_NUM_PARALLEL, and `journalctl -u ollama`.")
        return out
    culprits = [n for n, v in (("classify", "classify_only"), ("judge", "judge_only")) if share(v) < 0.5]
    out.append("the REAL flow MISSES while main-only hits: the side calls evict the cache"
               + (f" (culprit: {' + '.join(culprits)})" if culprits else " (only the combination)")
               + ". Options: OLLAMA_NUM_PARALLEL=2 (+VRAM for a second KV slot), or run classify/judge on a small separate model.")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", required=True)
    ap.add_argument("--host", default=None)
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--rounds", type=int, default=3)
    a = ap.parse_args(argv)
    import requests
    import cache_probe_prod as CP
    import llm
    import core
    if a.host:
        llm.OLLAMA_HOST = a.host
        llm.MODEL_URL = a.host.rstrip("/") + "/api/chat"
    T, state, policy0, hist = CP.build_real_messages(a.user, Path(a.data_dir))
    import policy as P
    base_prompt = T.load_system_prompt()
    ctl = {"limit": False}
    real_post = requests.post

    def post(*args, **kw):
        j = kw.get("json")
        if j is not None and ctl["limit"]:
            j = json.loads(json.dumps(j))
            j.setdefault("options", {})["num_predict"] = 8
            kw["json"] = j
        return real_post(*args, **kw)

    requests.post = post
    res = {}
    print(f"{'variant':14s} round  main tokens  main eval s   tok/s")
    try:
        for name, do_cls, do_judge in VARIANTS:
            res[name] = []
            for rnd in range(1, a.rounds + 1):
                st = copy.deepcopy(state)
                act = st.setdefault("active_session", {})
                conv = copy.deepcopy(hist)
                conv[-1]["content"] = f"antwoord {name} {rnd}"
                text = conv[-1]["content"]
                act["turn_intent"], act["turn_correctness"] = "answer", "partial"
                ctl["limit"] = False
                if do_cls:
                    core._classify_turn(st, conv, text)
                    act["turn_intent"], act["turn_correctness"] = act.get("turn_intent") or "answer", act.get("turn_correctness") or "partial"
                if do_judge:
                    act["turn_intent"], act["turn_correctness"] = "answer", "partial"
                    core._judge_turn(st, conv, text)
                pol = P.suggest_next_activity(st)
                msgs = T.build_messages(base_prompt, st, conv, policy=pol)
                ctl["limit"] = True
                _, s = llm.ask_model(msgs, stream=True)
                ctl["limit"] = False
                r = {"tokens": int(s.get("prompt_eval_count") or 0), "sec": s.get("prompt_eval_s")}
                res[name].append(r)
                print(f"{name:14s} {rnd:5d} {r['tokens']:12d} {(r['sec'] or 0):12.2f} {speed(r):8.0f}")
    finally:
        requests.post = real_post
    print()
    for line in interpret_turns(res):
        print("-", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
