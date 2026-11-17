"""Focus + path boosts — domain-agnostic (no fixed eco/boost tables)."""
from knowledge.focus import detect_focus_mode, path_boost_tokens, focus_policy_line, update_session_focus
from knowledge.local_docs import search_local


def test_detect_phrase_after_over():
    m = detect_focus_mode("ik wil verder leren over EV Boost onder EMS")
    assert m is not None
    assert "boost" in m["id"]
    assert "Boost" in m["label"] or "boost" in m["label"].lower()


def test_detect_phrase_after_naar():
    m = detect_focus_mode("terug naar de eco wet")
    assert m is not None
    assert "eco" in m["id"]
    assert "eco" in m["label"].lower()


def test_detect_no_phrase():
    assert detect_focus_mode("ja dat klopt") is None
    assert detect_focus_mode("") is None


def test_focus_policy_line():
    st = {"active_session": {"focus_mode": {"id": "ev_boost", "label": "EV Boost"}}}
    line = focus_policy_line(st)
    assert "EV Boost" in line and "FOCUS MODE" in line


def test_update_session_focus():
    st = {"active_session": {}}
    update_session_focus(st, "nu over ev boost")
    assert "boost" in st["active_session"]["focus_mode"]["id"]


def test_path_boost_tokens():
    st = {"active_session": {"subject_id": "ems", "focus_mode": {"id": "ev_boost", "label": "EV Boost"}}}
    toks = path_boost_tokens(st, "boost packs")
    assert any("ems" in t for t in toks)
    assert any("boost" in t for t in toks)


def test_search_skips_nothing_smoke():
    search_local("ems eco 6A", path_boosts=["ems"])
