"""GREEN. Pure parts of tools/live_judge_check.py (the live run itself needs the real model)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import live_judge_check as L  # noqa: E402


def test_grading():
    assert L.grade("correct", "correct") == "OK"
    assert L.grade("correct", "uncertain") == "WARN"
    assert L.grade("correct", "partial") == "FAIL"        # exactly the production mistake
    assert L.grade(("correct", "uncertain"), "uncertain") == "OK"
    assert L.grade(("correct", "uncertain"), "incorrect", ambiguous=True) == "AMBIG"   # premise-dependent: not a failure
    assert L.grade(("correct", "uncertain"), "incorrect") == "FAIL"
    assert L.grade("incorrect", "correct") == "FAIL"
    assert L.grade("correct", None) == "NOJSON"


def test_cases_include_the_density_variants_and_controls():
    cases = {c["id"]: c for c in L.load_cases()}
    assert cases["density-fraction"]["expect"] == "correct" and cases["density-absolute"]["expect"] == "incorrect"
    assert cases["density-ambiguous"]["ambiguous"] and "uncertain" in cases["density-ambiguous"]["expect"]
    # the two variants differ ONLY in the CNO premise (and the density wording), same question and learner answer
    assert cases["density-fraction"]["answer"] == cases["density-absolute"]["answer"]
    assert cases["density-fraction"]["premises"] != cases["density-absolute"]["premises"]
    assert {"arith-right", "arith-wrong", "arith-partial", "challenge-valid", "challenge-invalid"} <= set(cases)


def test_style_selection():
    assert L.styles_from_arg("both", "cot_json") == ["cot_json", "think"]
    assert L.styles_from_arg("default", "think") == ["think"]
    assert L.styles_from_arg("cot_json", "think") == ["cot_json"]
    import pytest
    with pytest.raises(SystemExit):
        L.styles_from_arg("nope", "cot_json")


def test_ems_cases_carry_verbatim_evidence_and_sensible_expectations():
    import pathlib
    import pytest
    cases = {c["id"]: c for c in L.load_cases()}
    ems = [c for k, c in cases.items() if k.startswith("ems-")]
    assert len(ems) == 5 and all(c["evidence"] for c in ems)
    assert cases["ems-misconception"]["expect"] == "incorrect" and cases["ems-offtopic-true"]["expect"] == "off_target"
    assert cases["ems-true-responsive"]["expect"] == "correct"
    assert "incorrect" not in cases["ems-true-boost"]["expect"] and "incorrect" not in cases["ems-valid-challenge"]["expect"]
    doc = pathlib.Path(__file__).resolve().parent.parent / "knowledge" / "docs" / "ems.md"
    if not doc.exists():
        pytest.skip("knowledge/docs/ems.md not in this checkout")
    text = " ".join(doc.read_text(encoding="utf-8").split())
    for c in ems:
        for line in c["evidence"]:
            assert " ".join(line.split()) in text, f"evidence line not verbatim in ems.md: {line[:50]}"
