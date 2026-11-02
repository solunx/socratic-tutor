"""GREEN. Pure parts of tools/live_judge_check.py (the live run itself needs the real model)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import live_judge_check as L  # noqa: E402


def test_grading():
    assert L.grade("correct", "correct") == "OK"
    assert L.grade("correct", "uncertain") == "WARN"
    assert L.grade("correct", "partial") == "FAIL"        # exactly the production mistake
    assert L.grade("incorrect", "correct") == "FAIL"
    assert L.grade("correct", None) == "NOJSON"


def test_cases_include_the_human_checked_density_case_and_controls():
    cases = {c["id"]: c for c in L.load_cases()}
    assert cases["density"]["expect"] == "correct" and len(cases["density"]["premises"]) == 3
    assert {"arith-right", "arith-wrong", "arith-partial", "challenge-valid", "challenge-invalid"} <= set(cases)
    assert all(c["expect"] in ("correct", "partial", "incorrect") for c in cases.values())


def test_style_selection():
    assert L.styles_from_arg("both", "cot_json") == ["cot_json", "think"]
    assert L.styles_from_arg("default", "think") == ["think"]
    assert L.styles_from_arg("cot_json", "think") == ["cot_json"]
    import pytest
    with pytest.raises(SystemExit):
        L.styles_from_arg("nope", "cot_json")
