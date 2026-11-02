# Round 2 handoff — wat overnemen + commands

Status in deze sandbox (2026-10-20):
- `reference_patch_2.diff` is toegepast op `core.py`, `policy.py`, `tutor.py`
- Abbreviation-fix in `guards.py` (t.o.v. / bijv. / e.g. knippen de final question niet meer)
- `tools/cache_probe.py` toegevoegd
- `docs/NEXT_DESIGN.md` + `docs/README_FOR_GROK_v2.md` toegevoegd
- `tests/test_round2_reframe_rotation.py` toegevoegd (3 tests, groen)
- Suite: **78 passed, 1 xfailed**

## Bestanden die jij moet overnemen naar `~/dev/tutor`

Kopieer **alleen** deze (overschrijf de bestaande):

```
core.py
policy.py
tutor.py
guards.py
tools/cache_probe.py
docs/NEXT_DESIGN.md
docs/README_FOR_GROK_v2.md
docs/R2_HANDOFF.md
tests/test_round2_reframe_rotation.py
```

**Niet** overnemen: `data/`, `learner_state*`, `events*`, `web_conversation*`, `*.bak*`.

## Commands op gpu01 (in volgorde)

```bash
cd ~/dev/tutor
source .venv/bin/activate

# 1. Backup van wat je gaat overschrijven
cp core.py core.py.bak-pre-r2
cp policy.py policy.py.bak-pre-r2
cp tutor.py tutor.py.bak-pre-r2
cp guards.py guards.py.bak-pre-r2

# 2. Plak/kopieer de bestanden hierboven vanuit de sandbox/download
#    (via scp, VS Code remote, of handmatig plakken)

# 3. Compile-check
python -m py_compile core.py policy.py tutor.py guards.py tools/cache_probe.py

# 4. Unit tests
python -m pytest -q
# verwacht: ~78 passed, 1 xfailed (documented paraphrased-leak miss)

# 5. FSRS-opschoning george (dry-run eerst)
python tools/fix_user_state.py --user george --reset-fsrs --all-concepts --dry-run
# als de output klopt:
python tools/fix_user_state.py --user george --reset-fsrs --all-concepts

# 6. Gateway herstarten
sudo systemctl restart tutor-gateway
sudo systemctl status tutor-gateway --no-pager | head -15

# 7. Optioneel: prefix-cache diagnose (1–2 min, op de GPU-box)
python tools/cache_probe.py | tee metrics/cache_probe_r2.txt
```

## Wat je op de webpagina moet testen (15 beurten, één onderwerp)

1. Start of hervat één onderwerp (bv. zon of agents).
2. Antwoord een paar keer correct → na **4 clean corrects** moet de tutor **roteren** naar een ander concept (niet opnieuw dezelfde vergelijkingsvraag).
3. Zeg iets als: *“je blijft in rondjes draaien”* / *“waarom dezelfde vraag op beginner niveau?”*  
   → verwacht **reframe**: korte erkenning + wat je al toonde + keuze (dieper / ander concept / afronden), **geen** nieuwe quizvraag.
4. Zeg *“geen idee”* op een open gap → scaffold **dezelfde** gap, geen nieuw onderwerp.
5. Check logs:
   ```bash
   python tools/tail_events.py george --last 15
   ```
   Zoek `reframe=policy` of `reframe=loop`, en `gate_repair=`.  
   Er mag **geen** `gate_repair_failed` gevolgd door een letterlijk identieke reply staan.

## Gedrag dat nu in code zit

| Trigger | Actie |
|--------|--------|
| `turn_intent=frustration` | policy `reframe=True` → kleine side-call, geen main-prompt |
| Gate block + repair faalt | opnieuw `_reframe`, draft alleen als last resort + `reframe_failed` |
| `correct_streak >= 4` | rotate naar zwakste sibling/ander concept, `rotation_pending` beschermt tegen assessment-overwrite |
| Afkortingen in tekst | `guards.split_sentences` beschermt `t.o.v.` / `bijv.` / `e.g.` e.d. |

## Nog niet gedaan (zie NEXT_DESIGN.md)

- Latency: slim schema, response-first, Ollama prefix-cache (eerst `cache_probe` bekijken)
- Skill ledger + semantic dedupe (“zelfde vraag, andere getallen”)
- Move-based prompts, overige classifier-intents, feedback bij `correctness=unknown`
