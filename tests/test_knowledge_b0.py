"""Phase B0 — local docs + verify (Wikipedia mocked; offline-safe)."""
from __future__ import annotations

import knowledge.local_docs as LD
import knowledge.verify as V
import knowledge.wiki as W


def test_list_docs_finds_lesson_notes():
    paths = LD.list_docs()
    names = {p.name for p in paths}
    assert "pp_keten.md" in names
    assert "cno_cyclus.md" in names
    assert "README.md" not in names


def test_search_local_hits_pp_chain():
    hits = LD.search_local("pp-keten deuterium helium-3 stappen", limit=3)
    assert hits, "expected local hit on pp_keten.md"
    assert any("pp" in (h.get("source") or "").lower() for h in hits)


def test_evidence_for_local_only(monkeypatch):
    monkeypatch.setattr(V.wiki, "search_hits", lambda *a, **k: [])
    monkeypatch.setattr(V.wiki, "fetch_extract", lambda *a, **k: None)
    pack = V.evidence_for("pp-keten stap deuterium", use_wiki=True, use_local=True)
    assert pack["status"] in ("found", "weak")
    assert pack["local_ok"] is True
    assert all(s["kind"] == "local" for s in pack["snippets"])


def test_evidence_offline_wiki_still_returns_local(monkeypatch):
    monkeypatch.setattr(V.wiki, "search_hits", lambda *a, **k: [])
    monkeypatch.setattr(V.wiki, "fetch_extract", lambda *a, **k: None)
    pack = V.evidence_for("CNO cyclus temperatuur", use_wiki=True)
    assert pack["wiki_ok"] is False
    assert pack["local_ok"] is True
    assert pack["status"] == "found"


def test_evidence_nothing_at_all(monkeypatch):
    monkeypatch.setattr(V.wiki, "search_hits", lambda *a, **k: [])
    monkeypatch.setattr(V.wiki, "fetch_extract", lambda *a, **k: None)
    monkeypatch.setattr(V.local_docs, "search_local", lambda *a, **k: [])
    pack = V.evidence_for("xyzzy-nonexistent-topic-42")
    assert pack["status"] == "not_found"
    assert pack["snippets"] == []


def test_evidence_wiki_mocked(monkeypatch):
    def fake_hits(query, lang="nl", limit=8):
        return [{"title": "Proton–proton chain reaction", "snippet": "fusion stars", "score": 3.0, "lang": lang}]

    def fake_extract(title, lang="nl", chars=900):
        return {
            "title": title,
            "lang": lang,
            "text": "The proton–proton chain is a set of fusion reactions.",
            "url": "https://en.wikipedia.org/wiki/x",
            "source": f"wikipedia:{lang}:{title}",
        }

    monkeypatch.setattr(V.wiki, "search_hits", fake_hits)
    monkeypatch.setattr(V.wiki, "fetch_extract", fake_extract)
    pack = V.evidence_for("pp chain fusion", use_local=False, use_wiki=True)
    assert pack["wiki_ok"] is True
    assert pack["snippets"][0]["kind"] == "wikipedia"


def test_no_topic_hint_table():
    assert not hasattr(V, "_WIKI_HINTS")


def test_title_noise_filter():
    assert not W.title_looks_useful("Lijst van afkortingen in de geneeskunde")
    assert not W.title_looks_useful("Foo (disambiguation)")
    assert W.title_looks_useful("Coulomb barrier")
    assert W.title_looks_useful("Proton-protoncyclus")


def test_format_evidence_for_prompt():
    pack = {
        "query": "pp", "status": "found", "wiki_ok": False, "local_ok": True,
        "snippets": [{"kind": "local", "title": "PP", "source": "local:pp_keten.md",
                      "text": "Stap 1 is p+p.", "url": None}],
    }
    assert "SOURCE EVIDENCE" in V.format_evidence_for_prompt(pack)


def test_wiki_empty_query():
    assert W.search_titles("") == []
    assert W.fetch_extract("") is None
