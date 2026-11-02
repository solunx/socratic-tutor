#!/usr/bin/env python3
"""
Production-SHAPED cache probe: why do the tutor's MAIN calls still miss the Ollama prompt cache?

Known (gpu01, 2026-10-26): keep_alive is fixed and cache_probe.py shows the cache works (16k tokens in 0.2-0.8 s) -- yet the
tutor's real main calls still take ttft ~7.1-7.8 s (7.5k tokens at ~1,070 tok/s = evaluated from scratch every turn), while the
REPAIR calls (same payload + one extra message) hit (0.7 s). So the real request SHAPE defeats the reuse. The simple probe differs
from production in: stream, format=json, temperature, think, the chat history, and an interleaved classify call.

This tool builds the REAL system prompt for a real user (read-only: it works on a temp COPY of the state), then for each
variant of the payload sends request A and then request B (same prefix, different last user message) and reports how fast B was.

    python tools/cache_probe_prod.py --user george [--host http://localhost:11434] [--data-dir data]

Takes ~1-2 minutes (a miss costs ~7 s). Writes nothing in your data. Output: per variant tokens/s of B and a verdict
naming the factor that breaks reuse (if any).
"""
from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CACHE_LIKE_TOK_PER_S = 4000

# variant name -> payload mutation (applied to the JSON sent to /api/chat) ; "stream" and "history" are handled separately
VARIANTS = [
    ("production", lambda j: None, True, "full"),
    ("no_stream", lambda j: None, False, "full"),
    ("no_format", lambda j: j.pop("format", None), True, "full"),
    ("temp_0", lambda j: j["options"].__setitem__("temperature", 0), True, "full"),
    ("think_omitted", lambda j: j.pop("think", None), True, "full"),
    ("no_history", lambda j: None, True, "no_history"),
    ("with_classify_between", lambda j: None, True, "classify_between"),
]


def speed(tokens: int, seconds: float | None) -> float:
    return (tokens / seconds) if seconds and seconds > 0 and tokens else 0.0


def hit(r: dict) -> bool:
    return speed(r.get("tokens", 0), r.get("sec")) >= CACHE_LIKE_TOK_PER_S


def interpret_prod(res: dict[str, dict]) -> list[str]:
    """res: variant -> {'tokens','sec'} for request B. Pure function (unit-tested)."""
    out = ["tok/s of request B per variant: " + ", ".join(f"{k}:{speed(v['tokens'], v['sec']):.0f}" for k, v in res.items())
           + f"   [>{CACHE_LIKE_TOK_PER_S} = cache hit]"]
    base = res.get("production")
    if base is None:
        return out + ["no production variant measured"]
    cb = res.get("with_classify_between")
    if hit(base):
        out.append("production shape HITS the cache here: the misses in real use come from something this probe does not "
                   "reproduce (idle gaps, other users' requests, the metrics/tokenize calls). Check OLLAMA_NUM_PARALLEL and "
                   "other clients of the same Ollama.")
        if cb is not None:
            out.append("an interleaved classify call " + ("does NOT break reuse." if hit(cb) else
                       "BREAKS reuse -> run classify on a small separate model, or OLLAMA_NUM_PARALLEL=2."))
        return out
    out.append("production shape MISSES (like the real tutor).")
    culprits = {"no_stream": "stream=True", "no_format": "format=json", "temp_0": "temperature 0.4 vs 0",
                "think_omitted": "the think parameter", "no_history": "the chat HISTORY after the system message"}
    found = [culprits[k] for k, v in res.items() if k in culprits and hit(v)]
    if found:
        out.append("variants that HIT: " + ", ".join(found) + "  -> that factor is what defeats reuse; change it in llm.py.")
    else:
        out.append("no single factor fixes it. Look at the interleaved call and at whether the system message prefix really is "
                   "byte-identical between turns (tools: compare two consecutive turns' system messages).")
    if cb is not None:
        out.append("the interleaved-classify variant is not informative while the production shape already misses; "
                   "re-run it after fixing the factor above.")
    return out


def build_real_messages(user: str, data_dir: Path):
    """Real system prompt + real recent history for `user`, built on a temp COPY of the data (read-only for the real data)."""
    import auth
    import policy as P
    import tutor as T

    tmp = Path(tempfile.mkdtemp(prefix="cacheprobe_"))
    src = data_dir / "users" / user
    dst = tmp / "users" / user
    dst.mkdir(parents=True)
    for name in ("learner_state.json", "web_conversation.json"):
        if (src / name).exists():
            shutil.copy(src / name, dst / name)
    auth.DATA_DIR = tmp
    state = T.load_state(user_id=user)
    hist = []
    conv = dst / "web_conversation.json"
    if conv.exists():
        d = json.loads(conv.read_text(encoding="utf-8"))
        th = (d.get("threads") or {}).get(d.get("active_thread") or "") or []
        hist = [{"role": m["role"], "content": m["content"]} for m in th if m.get("role") in ("user", "assistant")][-6:]
    if not hist:
        hist = [{"role": "assistant", "content": "Welke keten wint dan, en waarom?"}, {"role": "user", "content": "geen idee"},
                {"role": "assistant", "content": "Denk aan de ladingen. Wat gebeurt er met de afstoting?"}, {"role": "user", "content": "die wordt groter"}]
    if hist[-1]["role"] != "user":
        hist.append({"role": "user", "content": "mijn antwoord"})
    policy = P.suggest_next_activity(state)
    return T, state, policy, hist


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", required=True)
    ap.add_argument("--host", default=None)
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    a = ap.parse_args(argv)
    import llm
    import requests
    if a.host:
        llm.OLLAMA_HOST = a.host
        llm.MODEL_URL = a.host.rstrip("/") + "/api/chat"
    T, state, policy, hist = build_real_messages(a.user, Path(a.data_dir))
    base_prompt = T.load_system_prompt()

    def msgs(last_user: str, history="full"):
        h = copy.deepcopy(hist)
        h[-1]["content"] = last_user
        if history == "no_history":
            h = h[-1:]
        return T.build_messages(base_prompt, state, h, policy=policy)

    real_post = requests.post
    res = {}
    print(f"{'variant':24s} {'A tokens':>9s} {'B tokens':>9s} {'B eval s':>9s} {'B tok/s':>9s}")
    try:
        for name, mut, stream, history in VARIANTS:
            def post(*args, _mut=mut, **kw):
                j = kw.get("json")
                if j is not None:
                    j = json.loads(json.dumps(j))
                    j.setdefault("options", {})["num_predict"] = 8       # speed: the cache question is about the PROMPT
                    _mut(j)
                    kw["json"] = j
                return real_post(*args, **kw)
            requests.post = post
            ca, sa = llm.ask_model(msgs("antwoord A", history), stream=stream)
            if history == "classify_between":
                llm.ask_model([{"role": "system", "content": "You classify ONE learner message. JSON only. " + "x " * 150},
                               {"role": "user", "content": "hallo"}], stream=False)
            cb, sb = llm.ask_model(msgs("antwoord B (andere tekst)", history if history != "classify_between" else "full"), stream=stream)
            tb, secb = int(sb.get("prompt_eval_count") or 0), sb.get("prompt_eval_s")
            res[name] = {"tokens": tb, "sec": secb}
            print(f"{name:24s} {int(sa.get('prompt_eval_count') or 0):>9d} {tb:>9d} {(secb or 0):>9.2f} {speed(tb, secb):>9.0f}")
    finally:
        requests.post = real_post
    print()
    for line in interpret_prod(res):
        print("-", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
