# What should work differently (evidence: george's session 2026-10-18 20:00-21:08, after the "phase 0-5" update)

## Evidence (measured on the uploaded files, not opinions)
| fact | number |
|---|---|
| tutor turns in the post-update CNO block | 39 |
| ...of which the final question is a "which of the two wins" comparison | 17 (44%) |
| explicit learner complaints (circles / same reason 5x / 10x / beginner level) | 4 |
| classifier intents with ANY policy behaviour | 4 of 9 (answer, asks_hint, asks_reveal, challenges_tutor) |
| pre-send gate interventions / repairs that worked | 3 (1 echo ok) / 2 gate attempts, **0 succeeded**; the duplicate was delivered both times |
| time of such a turn | classify 1.4 s + main ~18 s + echo repair ~12 s + gate repair ~10 s = ~40 s |
| prompt per call | ~7.3k tokens: base prompt 2,971 + static rules 1,329 + output schema 831 + state 691 + policy hint 625 + avoid 326 + **dialogue 243 (3%)** + ... |
| prompt tokens re-evaluated per call (processed / prompt size) | 0.99 median (min 0.93): the identical 24k-char static prefix is NOT served from cache, before or after the classifier existed |
| output per call | 500-800 tokens for a ~100-token reply (assessment/session/gap_card JSON) |
| FSRS on old concepts | review_count 96, stability 3650 (the cap), lapses 14 (per-turn grading before the fix) |

## Diagnosis in one paragraph
Python now enforces *safety* (isolation, saves, gates), but nothing in Python decides *where the lesson goes*.
After a concept is demonstrated the only instruction is "apply / predict in a NEW situation", so a 27B model
re-invents the same question with other numbers. Detection works (the gate fired); the remedy does not (one repair
from the same 7k-token context lands in the same attractor, and on failure the duplicate was delivered).
97% of the context is rules, 3% is the actual conversation.

## Done in reference_patch_2 (tested)
1. `frustration` -> **reframe**: a small side call (no big prompt): acknowledge, say what the learner showed,
   offer a choice (deeper / switch concept / wrap up). Replaces the main call, so it is also ~15 s faster.
2. Failed repair -> **reframe**, never the known duplicate (draft only as last resort, logged `reframe_failed`).
3. `correct_streak` + **focus rotation** after 4 clean corrects: next concept (siblings first, weakest first), the
   open gap is parked. The model's assessment can no longer silently undo it (`rotation_pending`).
4. guards: abbreviations ("t.o.v.", "bijv.", "e.g.") no longer cut the final question in half.

## Next, in priority order
P1 **Latency (users feel this most)**
 - Run `tools/cache_probe.py` on the GPU box. If the prefix cache can work, ~4-6 s per turn returns for free.
 - Slim the output schema: gap_card only when a gap opens, assessment fields only when they change, no `session`
   fields that did not change. Target <= 200 output tokens -> ~-10 s per turn.
 - Response-first: call 1 returns only the learner-facing reply (validated by the gates), call 2 (assessment/session)
   runs while the learner reads; apply it before the next turn under the existing user lock.
 - UI: a waiting indicator and per-message timestamps (the learner cannot see times; debugging needs them).
P2 **Skill ledger + semantic dedupe** (the real fix for "same question, other numbers")
 - the model labels each question with `session.skill` (<= 6 words, learner's language);
 - Python keeps `skills[label] = {asked, correct, level}` (merge near-identical labels);
 - a skill with 2 clean corrects is CLOSED: the next question must either raise the level for that skill
   (recall -> apply -> predict -> quantify -> critique -> teach-back) or use a different skill/concept;
 - the gate compares the new `skill` label (and, optionally, an embedding of the question from a small Ollama
   embedding model via /api/embed, ~50 ms) with closed skills. Lexical dedupe cannot see that "which of the two stars
   makes more via X" and "which star delivers more energy via X" are the same question (documented xfail).
P2 **Move-based prompts.** Python already knows the move (ELICIT, REVEAL, APPLY, QUANTIFY, CONFRONT, RECOGNISE,
   REFRAME, ROTATE, DIAGNOSE). Send a ~1.2k-token base (identity, style, output contract) + ONE move block
   (300-500 tokens) + a 10-12 message window + the ledger (~200 tokens). Target <= 3k tokens per call.
P2 **Use the other classifier intents** (today write-only): `insight` -> RECOGNISE and follow their thread,
   `side_question` -> answer briefly then ask whether to resume (no automatic re-ask), `asks_clarification` ->
   restate the SAME question simpler (no new content), `off_topic` -> one line, back to the thread.
P2 **Feedback correctness.** "Goed, je kiest 0,9^4" praised a wrong answer (correctness=unknown). When the classifier
   says unknown, the reply must not contain approval; simplest: a one-sentence neutral acknowledgement + the
   next step. Checking arithmetic/comparisons needs a thinking-enabled evaluator call, not the 27B at think=False.
P3 Verification layer (Wikipedia / sources) stays later; it does not fix loops.

## Do NOT
- add more FORBIDDEN lines to the prompt (97% of the context is rules already);
- retry the repair from the same big prompt (it lands in the same attractor);
- deliver a detected duplicate as a fallback;
- count "tests green" as evidence: policy/contract tests use stubs. Judge by replaying real transcripts
  (add george's session as a second fixture) and by a short live run after every change.
