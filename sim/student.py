"""Deterministic student wording from a truth sheet — realistic enough to probe the tutor.

Design goals (not a second Socratic tutor):
- Use truth_sheet knowledge instead of pure repetition
- Anti-repeat: never emit the same learner line twice in a session
- After hints / stuck: escalate toward half_knows then knows
- React lightly to the last tutor question (keyword echo), not full LLM student
"""
from __future__ import annotations

import re
from typing import Any


def _pick(sheet: dict, key: str, default: str = "") -> str:
    v = sheet.get(key)
    if isinstance(v, list) and v:
        return str(v[0])
    if v is None:
        return default
    return str(v)


def _list(sheet: dict, key: str) -> list[str]:
    v = sheet.get(key) or []
    out = []
    for x in v:
        if isinstance(x, dict):
            out.append(str(x.get("statement") or x.get("id") or x))
        else:
            out.append(str(x))
    return out


def _typo(s: str, style: str) -> str:
    if style in ("typo", "short_typo") and len(s) > 24:
        s = s.replace(" de ", " d ").replace("een ", "en ").replace(" het ", " ht ")
        return s[:220]
    return s


def _keywords(tutor_last: str, n: int = 4) -> list[str]:
    stop = {
        "de", "het", "een", "van", "en", "is", "dat", "die", "wat", "voor", "met", "niet",
        "je", "jouw", "volgens", "denk", "waarom", "hoe", "als", "dan", "of", "the", "a",
        "to", "in", "op", "bij", "aan", "zijn", "was", "er", "dit", "zo", "ook",
        "klopt", "goed", "juist", "fout", "nee", "ja", "prima", "andere", "kleine", "stub",
    }
    words = re.findall(r"[A-Za-zÀ-ÿ0-9]{3,}", (tutor_last or "").lower())
    out = []
    for w in words:
        if w not in stop and w not in out:
            out.append(w)
        if len(out) >= n:
            break
    return out


class StudentMemory:
    """Per-session state so the agent does not loop the same line forever."""

    def __init__(self) -> None:
        self.said: set[str] = set()
        self.hint_asks = 0
        self.stuck_asks = 0
        self.partial_asks = 0
        self.turns = 0
        self.knows_i = 0
        self.half_i = 0
        self.wrong_i = 0

    def mark(self, text: str) -> str:
        self.said.add(text.strip().lower()[:160])
        self.turns += 1
        return text

    def already(self, text: str) -> bool:
        return text.strip().lower()[:160] in self.said


def student_utterance(
    *,
    intent: str,
    sheet: dict[str, Any],
    tutor_last: str,
    persona: dict[str, Any],
    memory: StudentMemory | None = None,
) -> str:
    mem = memory or StudentMemory()
    style = (persona.get("style") or "plain").lower()
    known = _list(sheet, "knows")
    half = _list(sheet, "half_knows")
    wrong = _list(sheet, "misconceptions")
    topic = sheet.get("topic") or "dit onderwerp"
    kws = _keywords(tutor_last)

    def finish(s: str) -> str:
        s = _typo(s.strip(), style)
        # if duplicate, try to vary once
        if mem.already(s):
            s = s + (" (nog eens kort)" if "nog eens" not in s.lower() else " — andere insteek")
        return mem.mark(s)

    def from_knows() -> str:
        if not known:
            return f"Ik denk dat het met {topic} te maken heeft, maar ik ben onzeker over de precieze regel."
        i = mem.knows_i % len(known)
        mem.knows_i += 1
        line = known[i]
        if kws:
            return line if not kws else f"{line}"
        return line

    def from_half() -> str:
        if half:
            i = mem.half_i % len(half)
            mem.half_i += 1
            return half[i]
        return from_knows()

    def from_wrong() -> str:
        if wrong:
            i = mem.wrong_i % len(wrong)
            mem.wrong_i += 1
            return wrong[i]
        return f"Volgens mij werkt {topic} altijd via de batterij, niet via de zon."

    # --- intent handlers with escalation ---
    if intent == "hint_request":
        mem.hint_asks += 1
        if mem.hint_asks == 1:
            return finish("Ik snap het nog niet helemaal — mag ik een kleine hint, zonder het hele antwoord?")
        if mem.hint_asks == 2:
            # use half knowledge after one hint already given by tutor
            return finish(from_half())
        return finish(from_knows())

    if intent == "stuck":
        mem.stuck_asks += 1
        if mem.stuck_asks == 1:
            return finish("Geen idee eerlijk gezegd. Duw me één stap in de goede richting?")
        if mem.stuck_asks == 2:
            return finish(from_half())
        return finish(from_knows())

    if intent == "reveal_request":
        if mem.turns < 2:
            return finish("Zeg het maar gewoon, wat is het antwoord?")
        return finish(from_half())

    if intent == "already_said":
        if known and mem.knows_i > 0:
            return finish("Dat heb ik net ongeveer gezegd — wil je dat ik het toepas op jouw voorbeeld?")
        return finish("Dat heb ik net gezegd. Stel een andere vraag of geef een concreet getallenvoorbeeld.")

    if intent == "off_topic":
        return finish("Even tussendoor: moet ik dit onthouden voor later, of is dit alleen een zijsprong?")

    if intent == "rename_meta":
        return finish(_pick(sheet, "rename_line", "Het onderwerp mag van mij gewoon EMS heten."))

    if intent == "challenge":
        claim = _pick(sheet, "challenge_line") or (
            f"Maar dat botst met wat ik dacht: {known[0]}" if known else
            "Je lijkt van mode te wisselen; dat was niet wat ik vroeg."
        )
        if kws and "mode" not in claim.lower():
            claim = f"{claim} Je vroeg net over {kws[0]}."
        return finish(claim)

    if intent == "wrong_confident":
        return finish(from_wrong())

    if intent == "partial":
        mem.partial_asks += 1
        if mem.partial_asks == 1 and half:
            base = from_half()
        elif mem.partial_asks >= 2 and known:
            base = from_knows()
        else:
            base = from_half()
        if kws:
            return finish(base if not kws else f"{base} — daar ben ik nog onzeker over.")
        return finish(base)

    # answer (default)
    if known:
        return finish(from_knows())
    if half:
        return finish(from_half())
    return finish(f"Ik wil {topic} beter begrijpen; stel een concrete situatie voor.")
