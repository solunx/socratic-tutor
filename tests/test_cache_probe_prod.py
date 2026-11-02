"""GREEN. Interpretation logic of tools/cache_probe_prod.py (the live run needs the real Ollama)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import cache_probe_prod as C  # noqa: E402


def R(tok, sec): return {"tokens": tok, "sec": sec}


HIT, MISS = R(7500, 0.4), R(7500, 7.0)


def test_production_miss_with_format_as_culprit():
    res = {"production": MISS, "no_stream": MISS, "no_format": HIT, "temp_0": MISS, "think_omitted": MISS,
           "no_history": MISS, "with_classify_between": MISS}
    msg = " ".join(C.interpret_prod(res))
    assert "MISSES" in msg and "format=json" in msg and "stream=True" not in msg
    assert "BREAKS reuse" not in msg and "not informative" in msg      # no false accusation while production itself misses


def test_production_miss_with_history_as_culprit():
    res = {"production": MISS, "no_history": HIT, "with_classify_between": HIT}
    msg = " ".join(C.interpret_prod(res))
    assert "HISTORY" in msg and "not informative" in msg


def test_production_hit_points_elsewhere():
    msg = " ".join(C.interpret_prod({"production": HIT}))
    assert "HITS the cache" in msg and "OLLAMA_NUM_PARALLEL" in msg


def test_no_single_factor():
    res = {k: MISS for k in ("production", "no_stream", "no_format", "temp_0", "think_omitted", "no_history")}
    assert "no single factor" in " ".join(C.interpret_prod(res))


def test_classify_verdict_only_when_production_hits():
    ok = " ".join(C.interpret_prod({"production": HIT, "with_classify_between": HIT}))
    bad = " ".join(C.interpret_prod({"production": HIT, "with_classify_between": MISS}))
    assert "does NOT break" in ok and "BREAKS reuse" in bad
