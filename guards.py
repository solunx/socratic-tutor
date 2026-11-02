"""
Reference implementation of the deterministic pre-send guards.

Design rules (do not break):
- Language-agnostic: NO stop-word lists, NO phrase lists, NO domain words.
  Glue words are down-weighted *adaptively* from the conversation itself
  (document frequency over the tutor's own turns).
- Pure functions, no LLM calls, no I/O -> unit-testable, millisecond cost.
- A guard only DECIDES. What to do on a block (one repair call) lives in core.py.

Public API
----------
split_sentences(text) -> list[str]
final_question(text) -> str | None
setup_and_question(text) -> str            # last 2 sentences ending at the final '?'
similarity(a, b, weights) -> float          # weighted Jaccard, 0..1
build_weights(tutor_texts) -> dict          # adaptive IDF-ish weights
repeat_gate(candidate, history, ...) -> GateResult
leak_check(expected_answer, prior_tutor_texts, ...) -> LeakResult
ResponseFieldBuffer                         # hold the streamed "response" JSON field until closed
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable

_Q_MARKS = "?？؟"
_SENT_SPLIT = re.compile(r"(?<=[.!?？؟])\s+")
_TOKEN = re.compile(r"\w+", re.UNICODE)
# Protect abbreviation dots so "t.o.v." / "bijv." / "e.g." do not become sentence breaks.
# Language-agnostic: single-letter chains (t.o.v., e.g., i.e.) + short tokens before a
# lowercase/digit continuation (bijv. waterstof, vs. 15, ca. 3).
_ABBREV_DOTS = re.compile(
    r"(?<!\w)(?:[A-Za-z]\.){1,6}(?=\s*[a-z0-9])"  # t.o.v.  e.g.  i.e.  d.w.z.
    r"|(?<!\w)[A-Za-z]{1,6}\.(?=\s*[a-z0-9])",    # bijv.  etc.  vs.  nr.  ca.
    re.UNICODE,
)
_DOT_PLACEHOLDER = "\uE000"


# ----------------------------------------------------------------- text utils
def split_sentences(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    protected = _ABBREV_DOTS.sub(lambda m: m.group(0).replace(".", _DOT_PLACEHOLDER), text)
    parts = [s.strip() for s in _SENT_SPLIT.split(protected) if s.strip()]
    return [p.replace(_DOT_PLACEHOLDER, ".") for p in parts]


def final_question(text: str) -> str | None:
    """Last sentence that ends with a question mark (not the whole paragraph)."""
    for s in reversed(split_sentences(text)):
        if s and s[-1] in _Q_MARKS:
            return s
    return None


def setup_and_question(text: str) -> str:
    """Final question + the sentence right before it (carries the scenario/setup)."""
    sents = split_sentences(text)
    for i in range(len(sents) - 1, -1, -1):
        if sents[i] and sents[i][-1] in _Q_MARKS:
            return " ".join(sents[max(0, i - 1): i + 1])
    return ""


def tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall((text or "").lower()) if len(t) >= 3 and not t.isdigit()}


# ----------------------------------------------------------------- weighting
def build_weights(tutor_texts: Iterable[str]) -> dict[str, float]:
    """
    weight(token) = clamp(1 - df/N, 0.05, 1), df counted over UNIQUE tutor sentences.
    Glue words (in most sentences) -> low weight; scenario words -> high weight.
    Unique sentences, so a question repeated six times does not make its own
    words look like glue (that was the failure of the first attempt).
    Language-agnostic: learned from the session itself, no stop-word list.
    """
    sents = set()
    for t in tutor_texts:
        for s in split_sentences(t or ""):
            sents.add(re.sub(r"\s+", " ", s.lower()))
    docs = [tokens(s) for s in sents]
    n = max(len(docs), 1)
    df: dict[str, int] = {}
    for d in docs:
        for t in d:
            df[t] = df.get(t, 0) + 1
    return {t: max(0.05, min(1.0, 1.0 - c / n)) for t, c in df.items()}


def similarity(a: str, b: str, weights: dict[str, float] | None = None) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    w = weights or {}
    def wt(t): return w.get(t, 1.0)          # unseen token = fully informative
    inter = sum(wt(t) for t in ta & tb)
    union = sum(wt(t) for t in ta | tb)
    return inter / union if union else 0.0


# ----------------------------------------------------------------- repeat gate
@dataclass
class GateResult:
    blocked: bool = False
    reason: str | None = None          # "repeat" | "template_cap" | None
    score: float = 0.0
    matched_index: int | None = None   # index in `history` of the earlier tutor turn
    detail: dict = field(default_factory=dict)


def _answered_tutor_turns(history: list[dict]) -> list[tuple[int, str]]:
    """(index, text) of tutor turns that the learner then replied to."""
    out = []
    for i, m in enumerate(history):
        if m.get("role") == "assistant" and i + 1 < len(history) and history[i + 1].get("role") == "user":
            out.append((i, m.get("text") or m.get("content") or ""))
    return out


def repeat_gate(
    candidate: str,
    history: list[dict],
    *,
    repeat_threshold: float = 0.85,
    template_threshold: float = 0.8,
    template_cap: int = 2,
    lookback_turns: int = 8,
    template_lookback_turns: int = 12,
) -> GateResult:
    """
    Decide whether `candidate` (the tutor reply about to be shown) re-asks something
    the learner already answered.

    history: [{"role": "assistant"|"user", "text": str}, ...] in order, WITHOUT the candidate.
             (accepts "content" instead of "text")

    Block when
      (1) setup+question is near-identical (>= repeat_threshold) to an answered turn
          in the last `lookback_turns` tutor turns            -> reason "repeat"
      (2) the final question sentence alone is near-identical (>= template_threshold)
          to the final question of more than `template_cap` earlier tutor turns
          (same template again and again, cosmetic scenario swaps)  -> "template_cap"

    Calibrated on a real session (the session fixture in tests/fixtures):
      setup+question score: exact repeat 1.00 / 0.88; every legitimate turn <= 0.71
      final-question score: same template 0.88-1.00; legitimate follow-ups <= 0.56
    Thresholds were chosen with margin on both sides; re-calibrate per language if
    you add fixtures (tests/test_guards_reference.py shows how).
    """
    cq = final_question(candidate)
    if not cq:
        return GateResult()
    tutor_texts = [(m.get("text") or m.get("content") or "") for m in history if m.get("role") == "assistant"]
    weights = build_weights(tutor_texts + [candidate])

    answered = _answered_tutor_turns(history)[-lookback_turns:]
    cand_sq = setup_and_question(candidate)
    best = GateResult()
    for idx, text in answered:
        s = similarity(cand_sq, setup_and_question(text), weights)
        if s > best.score:
            best = GateResult(False, None, s, idx)
    if best.score >= repeat_threshold:
        best.blocked, best.reason = True, "repeat"
        return best

    # template cap: count earlier tutor turns whose final question ~ candidate's final question
    same = []
    for (idx, text) in _answered_tutor_turns(history)[-template_lookback_turns:]:
        q = final_question(text)
        if q and similarity(cq, q, weights) >= template_threshold:
            same.append(idx)
    if len(same) >= template_cap:
        return GateResult(True, "template_cap", best.score, same[-1], {"uses": len(same)})
    best.detail = {"uses": len(same)}
    return best


# ----------------------------------------------------------------- leak check
@dataclass
class LeakResult:
    leaked: bool
    coverage: float
    detail: dict = field(default_factory=dict)


def leak_check(
    expected_answer: str,
    prior_tutor_texts: list[str],
    *,
    candidate_pre_question: str = "",
    threshold: float = 0.75,
    max_prior: int = 3,
) -> LeakResult:
    """
    The model supplies `expected_answer` for the question it is about to ask
    (gap_card.expected_answer). If its content words are already (mostly) present in
    the tutor's own recent NON-question sentences, the answer was given away.

    Known limitation (documented, tested as xfail): purely lexical. Paraphrased
    leaks are missed. This is a cheap guard, not a proof.
    """
    exp = tokens(expected_answer)
    if not exp:
        return LeakResult(False, 0.0)
    stated: set[str] = set()
    for t in prior_tutor_texts[-max_prior:]:
        for s in split_sentences(t):
            if s and s[-1] not in _Q_MARKS:
                stated |= tokens(s)
    for s in split_sentences(candidate_pre_question):
        if s and s[-1] not in _Q_MARKS:
            stated |= tokens(s)
    cov = len(exp & stated) / len(exp)
    return LeakResult(cov >= threshold, round(cov, 2))


# ----------------------------------------------------- streamed-field buffering
class ResponseFieldBuffer:
    """
    Feed raw streamed JSON text chunk by chunk. Returns the fully decoded value of the
    TOP-LEVEL "response" string as soon as its closing quote arrives, else None.
    Lets the gateway hold the reply back until the guards ran, while the rest of the
    JSON (assessment/session/gap_card) keeps streaming.

    Handles: chunk boundaries anywhere (also inside \\uXXXX or an escape), nested
    objects/arrays before the field, a "response" key at depth > 1 (ignored),
    surrogate pairs.
    """

    def __init__(self, field_name: str = "response"):
        self.field = field_name
        self.value: str | None = None
        self._depth = 0
        self._in_str = False
        self._esc = False
        self._u: str | None = None        # collecting \uXXXX hex digits
        self._str_buf: list[str] = []
        self._str_is_key = False
        self._expect_key = False          # at depth 1: next string is a key
        self._pending_key: str | None = None
        self._expect_colon = False
        self._expect_value = False
        self._capturing = False
        self._hi: str | None = None       # pending high surrogate

    @property
    def closed(self) -> bool:
        return self.value is not None

    def feed(self, chunk: str) -> str | None:
        for ch in chunk:
            if self.value is not None:
                return self.value
            self._step(ch)
        return self.value

    # -- internals
    def _emit_char(self, c: str):
        self._str_buf.append(c)

    def _step(self, ch: str):
        if self._in_str:
            self._string_char(ch)
            return
        if ch.isspace():
            return
        if ch == "{" or ch == "[":
            self._depth += 1
            self._expect_key = (ch == "{" and self._depth == 1)
            self._expect_colon = self._expect_value = False
        elif ch == "}" or ch == "]":
            self._depth -= 1
            self._expect_key = False
        elif ch == ",":
            self._expect_key = (self._depth == 1)
            self._expect_colon = self._expect_value = False
        elif ch == ":":
            if self._expect_colon:
                self._expect_colon, self._expect_value = False, True
        elif ch == '"':
            self._in_str, self._str_buf = True, []
            if self._depth == 1 and self._expect_key:
                self._str_is_key, self._expect_key = True, False
            elif self._depth == 1 and self._expect_value and self._pending_key == self.field:
                self._str_is_key, self._capturing = False, True
            else:
                self._str_is_key = False
            self._expect_value = False if not self._capturing else self._expect_value
        else:
            self._expect_value = False  # number/true/false/null value

    def _string_char(self, ch: str):
        if self._u is not None:
            self._u += ch
            if len(self._u) == 4:
                code = int(self._u, 16)
                self._u = None
                self._push_code(code)
            return
        if self._esc:
            self._esc = False
            m = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", '"': '"', "\\": "\\", "/": "/"}
            if ch == "u":
                self._u = ""
            else:
                self._emit_char(m.get(ch, ch))
            return
        if ch == "\\":
            self._esc = True
            return
        if ch == '"':
            self._in_str = False
            text = "".join(self._str_buf)
            if self._str_is_key:
                self._pending_key, self._expect_colon = text, True
            elif self._capturing:
                self.value = text
            return
        self._emit_char(ch)

    def _push_code(self, code: int):
        if 0xD800 <= code <= 0xDBFF:
            self._hi = chr(code)
        elif 0xDC00 <= code <= 0xDFFF and self._hi:
            pair = (self._hi + chr(code)).encode("utf-16", "surrogatepass").decode("utf-16")
            self._hi = None
            self._emit_char(pair)
        else:
            self._emit_char(chr(code))
