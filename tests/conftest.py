"""Shared test helpers (restored). Run from the repo root:  python -m pytest -q"""
import json
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("METRICS_ENABLED", "0")
os.environ.setdefault("TUTOR_EVENT_LOG", "0")
FIX = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def fx():
    return json.loads((FIX / "vriend1_kernfusie.json").read_text(encoding="utf-8"))


@pytest.fixture
def turns(fx):
    return {t["idx"]: t for t in fx["turns"]}


def history_upto(fx, idx):
    return [{"role": t["role"], "text": t["text"]} for t in fx["turns"] if t["idx"] < idx]


@pytest.fixture
def hist(fx):
    return lambda idx: history_upto(fx, idx)


def conversation_upto(fx, idx):
    return [{"role": t["role"], "content": t["text"]} for t in fx["turns"] if t["idx"] < idx]


def make_state(**active_overrides):
    import tutor as T
    st = T.default_state()
    st["learner"]["id"] = "vriend1"
    T.ensure_subject(st, "kernfusie", "kernfusie")
    T.ensure_concept(st, "kernfusie", "kernfusie")
    c = st["subjects"]["kernfusie"]["concepts"]["kernfusie"]
    c.update({"knowledge": 0.6, "retrieval": 0.5, "application": 0.3, "explanation": 0.6, "transfer": 0.2})
    a = st["active_session"]
    a.update({
        "active": True, "subject_id": "kernfusie", "current_concept_id": "kernfusie",
        "current_activity": "EXPLANATION", "prev_activity": "EXPLANATION",
        "just_resumed": False, "question_number": 9,
        "open_threads": [{
            "id": "t1", "q": "Wat verandert er aan de afstand waarop de kernen elkaar raken?",
            "status": "hinted", "tries": 2, "activity": "EXPLANATION",
        }],
    })
    a.update(active_overrides)
    return st


@pytest.fixture
def state_factory():
    return make_state


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    import tutor as T

    def boom(*a, **k):
        raise AssertionError("a test tried to call the real LLM; script it via ScriptedLLM / llm_call")

    for name in ("ask_model", "ask_model_with_json_retry"):
        if hasattr(T, name):
            monkeypatch.setattr(T, name, boom)
    try:
        import llm
        for name in ("ask_model", "ask_model_with_json_retry"):
            if hasattr(llm, name):
                monkeypatch.setattr(llm, name, boom)
    except ImportError:
        pass


class ScriptedLLM:
    """core.llm_call(role, messages, **opts) -> SimpleNamespace(raw, parsed, stats); records .calls."""

    def __init__(self, script, main_draft_text=None):
        self.script = {k: (list(v) if isinstance(v, list) else v) for k, v in script.items()}
        self.calls = []
        self.main_draft_text = main_draft_text

    def roles(self):
        return [c[0] for c in self.calls]

    def __call__(self, role, messages, **opts):
        self.calls.append((role, messages, opts))
        spec = self.script.get(role)
        if spec is None:
            raise AssertionError(f"unscripted llm_call role: {role!r}")
        parsed = spec.pop(0) if isinstance(spec, list) else spec
        if isinstance(parsed, dict) and parsed.get("__truncated__"):      # token budget exhausted before any JSON
            return types.SimpleNamespace(raw="", parsed=None, stats={"done_reason": "length"})
        cb = opts.get("on_response_delta")
        if role == "main" and cb and self.main_draft_text:
            text, n = self.main_draft_text, max(1, len(self.main_draft_text) // 3)
            for i in range(0, len(text), n):
                cb(text[: i + n], text[i: i + n])
        import json as _json
        return types.SimpleNamespace(raw=_json.dumps(parsed, ensure_ascii=False), parsed=parsed, stats={"streamed": False})


@pytest.fixture
def scripted_llm(monkeypatch):
    import core

    def install(script, main_draft_text=None):
        llm = ScriptedLLM(script, main_draft_text)
        monkeypatch.setattr(core, "llm_call", llm, raising=False)
        return llm

    return install


def main_result(response_text, *, verdict="correct", signal="correct", **extra):
    import guards
    res = {
        "routing": {"action": "continue"},
        "response": response_text,
        "assessment": {"subject": "kernfusie", "concept": "kernfusie", "activity": "EXPLANATION",
                       "verdict": verdict, "learner_signal": signal},
        "session": {"last_question": guards.final_question(response_text) or ""},
    }
    for k, v in extra.items():
        if k in ("assessment", "session"):
            res[k].update(v)
        else:
            res[k] = v
    return res
