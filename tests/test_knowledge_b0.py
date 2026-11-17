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


def test_split_into_chunks_finds_later_section():
    """Late content must become its own chunk (not only document prefix)."""
    text = (
        "# Intro\n\n"
        + ("Filler about solar panels and grid import. " * 40)
        + "\n\n## EV Boost rules\n\n"
        "EV Boost zonder import ontlaadt de packs tot de ingestelde rest. "
        "Er is geen 6A startdrempel op zon in deze mode.\n"
    )
    chunks = LD.split_into_chunks(text)
    assert len(chunks) >= 2
    joined = " ".join(c["text"] for c in chunks)
    assert "EV Boost" in joined
    assert any("EV Boost" in (c.get("text") or "") for c in chunks)


def test_search_local_returns_late_chunk_not_only_prefix(tmp_path, monkeypatch):
    """Query about a late section must not return only the document head."""
    long_doc = tmp_path / "long_manual.md"
    head = "# Manual\n\n" + ("Alpha topic about batteries and solar eco mode. " * 50)
    tail = (
        "\n\n## Night discharge mode\n\n"
        "Night discharge mode empties packs to the configured remainder without grid import. "
        "Minimum pack floor is twenty percent before discharge stops.\n"
    )
    long_doc.write_text(head + tail, encoding="utf-8")

    def fake_load():
        return [{
            "id": "long_manual.md",
            "path": str(long_doc),
            "title": "Manual",
            "text": long_doc.read_text(encoding="utf-8"),
            "source": "local:long_manual.md",
        }]

    monkeypatch.setattr(LD, "load_all", fake_load)
    hits = LD.search_local("night discharge packs remainder without grid import", limit=3)
    assert hits, "expected a hit on the late section"
    blob = " ".join(h["text"] for h in hits).lower()
    assert "night discharge" in blob or "remainder" in blob
    # Must not be only the alpha/eco prefix
    assert "twenty percent" in blob or "grid import" in blob


def test_search_local_short_doc_still_works():
    hits = LD.search_local("deuterium helium-3 zwakke wisselwerking", limit=2)
    assert hits
    assert any("pp" in (h.get("source") or "").lower() for h in hits)


def test_api_retrieve_alias_matches_evidence_for():
    from knowledge.api import retrieve, evidence_for, retrieve_local
    a = retrieve("pp-keten deuterium", max_snippets=2, use_wiki=False)
    b = evidence_for("pp-keten deuterium", max_snippets=2, use_wiki=False)
    assert a["status"] == b["status"]
    assert len(a.get("snippets") or []) == len(b.get("snippets") or [])
    loc = retrieve_local("pp-keten", limit=2)
    assert loc.get("wiki_ok") is False or loc.get("status") in ("found", "weak", "not_found")


def test_excerpt_centers_on_query_not_prefix():
    from knowledge.chunking import excerpt_for_query
    # Decisive phrase near the end; prefix is noise
    body = ("Eco intro " * 40) + "EV Boost zonder import ontlaadt de packs tot de ingestelde rest. " + ("tail " * 20)
    out = excerpt_for_query(body, "EV Boost zonder import packs", max_chars=120)
    assert "EV Boost" in out or "Boost" in out
    assert "ontlaadt" in out or "packs" in out


def test_split_respects_headings():
    from knowledge.chunking import split_into_chunks
    md = "# Title\n\nIntro para.\n\n## Boost-staat\n\nEV Boost zonder import ontlaadt de packs.\n\n## Eco\n\nGewone eco op zon.\n"
    chunks = split_into_chunks(md, max_chars=200)
    texts = " ".join(c["text"] for c in chunks)
    assert "EV Boost" in texts
    assert any("Boost" in (c.get("heading") or "") or "Boost" in c["text"] for c in chunks)
