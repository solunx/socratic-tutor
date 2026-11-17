"""GREEN. Interpretation logic of tools/cache_probe_turns.py (the live run needs the real Ollama)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import cache_probe_turns as C  # noqa: E402


def R(tok, sec): return {"tokens": tok, "sec": sec}


HIT, MISS = R(7500, 0.5), R(7500, 7.0)


def rounds(first, rest, n=3): return [first] + [rest] * (n - 1)


def test_judge_is_the_culprit():
    res = {"main_only": rounds(MISS, HIT), "normal": rounds(MISS, MISS), "classify_only": rounds(MISS, HIT),
           "judge_only": rounds(MISS, MISS)}
    msg = " ".join(C.interpret_turns(res))
    assert "side calls evict" in msg and "judge" in msg and "culprit: judge" in msg and "classify +" not in msg


def test_real_flow_hits_points_outside_the_process():
    res = {"main_only": rounds(MISS, HIT), "normal": rounds(MISS, HIT), "classify_only": rounds(MISS, HIT), "judge_only": rounds(MISS, HIT)}
    msg = " ".join(C.interpret_turns(res))
    assert "HITS" in msg and "systemctl cat" in msg


def test_control_misses_means_server_or_prompt():
    res = {"main_only": rounds(MISS, MISS), "normal": rounds(MISS, MISS)}
    assert "main-only control MISSES" in " ".join(C.interpret_turns(res))
