"""B1 — query building, inject, footer (no live network)."""
from knowledge.turn_evidence import (
    query_for_turn,
    sources_payload,
    format_sources_footer,
    inject_evidence_into_messages,
    gather_turn_evidence,
)


def test_query_includes_learner_and_concept():
    st = {"active_session": {
        "subject_id": "zon",
        "current_concept_id": "pp_keten",
        "open_threads": [{
            "q": "Welke keten domineert?",
            "status": "open",
            "gap_card": {"key": "dichtheid ratio", "target": "pp vs cno"},
            "premises": ["zelfde T"],
        }],
    }}
    q = query_for_turn(st, "de verhouding blijft gelijk")
    assert "verhouding" in q.lower() or "gelijk" in q.lower()
    assert "pp" in q.lower() or "keten" in q.lower() or "domineert" in q.lower()


def test_footer_and_payload():
    pack = {
        "status": "found",
        "snippets": [{
            "kind": "local", "title": "PP", "source": "local:pp_keten.md",
            "text": "Stap 1 p+p.", "url": None,
        }],
    }
    src = sources_payload(pack)
    assert src[0]["source"] == "local:pp_keten.md"
    foot = format_sources_footer(pack)
    assert "Bronnen:" in foot and ("pp_keten" in foot or "local:pp" in foot)


def test_inject_appends_to_system():
    msgs = [{"role": "system", "content": "BASE"}, {"role": "user", "content": "hi"}]
    pack = {
        "status": "found",
        "wiki_ok": False,
        "local_ok": True,
        "query": "pp",
        "snippets": [{"kind": "local", "title": "PP", "source": "local:x", "text": "hello evidence", "url": None}],
    }
    inject_evidence_into_messages(msgs, pack)
    assert "SOURCE EVIDENCE" in msgs[0]["content"]
    assert "hello evidence" in msgs[0]["content"]


def test_inject_skips_not_found():
    msgs = [{"role": "system", "content": "BASE"}]
    inject_evidence_into_messages(msgs, {"status": "not_found", "snippets": []})
    assert msgs[0]["content"] == "BASE"


def test_gather_respects_disable(monkeypatch):
    monkeypatch.setenv("TUTOR_EVIDENCE", "0")
    pack = gather_turn_evidence(
        {"active_session": {"current_concept_id": "pp_keten"}},
        "deuterium stap",
    )
    assert pack.get("status") == "not_found"
    assert pack.get("enabled") is False


def test_sources_payload_dedupes_same_local_doc():
    from knowledge.turn_evidence import sources_payload, format_sources_footer
    pack = {
        "status": "found",
        "snippets": [
            {"kind": "local", "source": "local:ems.md", "title": "A", "text": "chunk one about eco"},
            {"kind": "local", "source": "local:ems.md", "title": "A", "text": "chunk two about boost"},
            {"kind": "local", "source": "local:other.md", "title": "B", "text": "other doc"},
        ],
    }
    src = sources_payload(pack)
    paths = [s.get("source") for s in src]
    assert paths.count("local:ems.md") == 1
    assert "local:other.md" in paths
    foot = format_sources_footer(pack)
    assert foot.count("[local] ems.md") == 1
