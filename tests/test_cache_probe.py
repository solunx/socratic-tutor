"""Interpretation logic of tools/cache_probe.py v2, checked against the owner's REAL probe output (2026-10-20)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import cache_probe as CP  # noqa: E402


def R(tokens, ms): return {"tokens": tokens, "ms": ms}


# his actual numbers: the token COUNT stays 16132 even when the cache is hit; only the time changes
RUN1 = [R(16132, 15041), R(16132, 769), R(16136, 827), R(26, 184), R(16136, 180)]
RUN2 = [R(16132, 772), R(16132, 770), R(16136, 819), R(26, 124), R(16136, 183)]


def test_real_run_1_is_a_working_cache_not_a_failure():
    msg = " ".join(CP.interpret(RUN1))
    assert "WORKS" in msg and "NO PREFIX REUSE" not in msg
    assert "does NOT evict" in msg and "good" in msg


def test_real_run_2_warm_start_is_recognised():
    assert "WORKS" in " ".join(CP.interpret(RUN2))


def test_really_uncached_is_still_detected():
    rs = [R(16000, 15000), R(16000, 14800), R(16000, 14900), R(400, 300), R(16000, 14950)]
    assert "NO PREFIX REUSE" in " ".join(CP.interpret(rs))


def test_eviction_by_a_side_call_is_detected():
    rs = [R(16000, 15000), R(16000, 700), R(16000, 800), R(400, 300), R(16000, 14800)]
    assert "EVICTS" in " ".join(CP.interpret(rs))


def test_idle_gap_loss_is_detected():
    rs = [R(16000, 15000), R(16000, 700), R(16000, 800), R(400, 300), R(16000, 700), R(16000, 14900)]
    assert "GONE" in " ".join(CP.interpret(rs))
