# Round 2 — read this after README_FOR_GROK.md

Your phase 0-5 work passed everything in round 1 (verified: 75 passed on your latest files). Round 2 is about
what the REAL session of 2026-10-18 showed. Evidence + priorities: NEXT_DESIGN.md.

## Run
    cp -r guards.py tests tools pytest.ini <repo root>/          # overwrite guards.py and the tests
    python -m pytest -o addopts="" -q
Verified end-to-end on a rebuild of your repo (your latest files + round-1 tests, markers removed):
    before overlay                      : 75 passed, 1 xfailed          (identical to your own run)
    after overlay, your code            : 83 passed, 14 xfailed         (12 = todo list, 2 = documented known misses)
    + `patch -p1 < reference_patch_2.diff` (core.py, policy.py, tutor.py; applies cleanly to your latest files):
                                          12 failed = strict XPASS (the 12 todo tests now work) -> delete their markers
    + markers removed                   : 95 passed, 2 xfailed

## What changed in the kit
- guards.py: BUG FIX — "t.o.v." / "bijv." / "e.g." split the final question in half. Replace your copy.
- tests/test_guards_reference.py: + abbreviation regression, + known miss "same skill, different wording" (xfail).
- tests/test_contracts_v2.py (NEW): frustration -> reframe; failed repair -> reframe; rotation; streak.
  Negative tests carry a positive control so they cannot pass vacuously.
- tests/test_cache_probe.py + tools/cache_probe.py (NEW): finds out why Ollama re-evaluates the static prefix.
- tools/fix_user_state.py: + `--all-concepts` (george: stability 3650, review_count 96, lapses 14):
      python tools/fix_user_state.py --user george --reset-fsrs --all-concepts --dry-run     # then without --dry-run
  It keeps evidence and misconceptions, writes a .bak-fix-* backup first, and refuses on learner.id mismatch.

## Corrections to round 1 (my errors, be explicit about them)
1. The kit REQUIRED "failed repair -> deliver the draft". That is wrong: the duplicate is then shown (it happened twice).
   `test_failed_repair_falls_back_to_draft...` is replaced by `test_failed_repair_goes_to_reframe_...`;
   the draft is now only a logged last resort.
2. "Echo repair overwrites the assessment" was wrong (dead code). The real bug was `session.last_question`
   pointing at the discarded draft (fixed in round 1).
3. I said the classifier probably evicts Ollama's prompt cache. The data says the cache was never used
   (processed/prompt = 0.99 before the classifier too). Use tools/cache_probe.py before theorising.

## Integration bug the tests found
`apply_assessment` re-set `current_concept_id` from the model's assessment, silently undoing a Python rotation.
Fixed with `rotation_pending` (policy sets it, apply_assessment consumes it). Keep that if you re-implement.

## Contract additions
- role "reframe": small side call, NOT the big prompt (test asserts < 2500 chars); replaces `main` on a reframe turn.
- policy: `move` in {reframe, rotate, ...}; `intent=frustration` -> `reframe`.
- state: `correct_streak` (clean corrects; partial/incorrect reset), `parked_thread_q`, `rotation_pending`.
- stats: `reframe` = "policy" | "loop", `reframe_failed`.

## Not done (see NEXT_DESIGN.md): latency (cache, slim schema, response-first, UI), skill ledger + semantic dedupe,
## move-based prompts, the other 4 write-only intents, feedback-correctness when correctness == unknown.

## Done means
Suite green (2 documented xfails) + a 15-turn live run on ONE topic where: "geen idee" scaffolds the same gap,
frustration produces a reframe (no quiz question), a demonstrated concept rotates instead of looping, and
`tail_events` shows `reframe=` / `gate_repair=` and no `gate_repair_failed` followed by an identical reply.
