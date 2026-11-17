"""Run simulated learner sessions against core.handle_turn.

Critical: keep learner `state` in memory across turns. With save=False the product
must not write disk; the harness therefore MUST pass the same state dict every turn.
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim.intent import choose_intent
from sim.student import student_utterance, StudentMemory
from sim.curriculum import curriculum_steps, message_for_step
from sim.metrics import summarize_metrics

SIM_DIR = Path(__file__).resolve().parent
RUNS_DIR = SIM_DIR / "runs"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_persona(name: str) -> dict[str, Any]:
    p = SIM_DIR / "personas" / f"{name}.json"
    if not p.exists():
        raise FileNotFoundError(p)
    data = load_json(p)
    data["_id"] = name
    return data


def load_scenario(name: str) -> dict[str, Any]:
    p = SIM_DIR / "scenarios" / f"{name}.json"
    if not p.exists():
        raise FileNotFoundError(p)
    return load_json(p)


def apply_knowledge_condition(condition: str | None) -> None:
    c = (condition or "local").lower().strip()
    if c in ("model_only", "none", "off"):
        os.environ["TUTOR_EVIDENCE"] = "0"
        os.environ["TUTOR_EVIDENCE_WIKI"] = "0"
    elif c in ("wiki", "wiki_only"):
        os.environ["TUTOR_EVIDENCE"] = "1"
        os.environ["TUTOR_EVIDENCE_WIKI"] = "1"
    elif c in ("local", "local_only"):
        os.environ["TUTOR_EVIDENCE"] = "1"
        os.environ["TUTOR_EVIDENCE_WIKI"] = "0"
    else:
        os.environ["TUTOR_EVIDENCE"] = "1"
        os.environ["TUTOR_EVIDENCE_WIKI"] = "1"


def format_transcript(session: dict[str, Any]) -> str:
    lines = [
        f"session_id={session.get('session_id')}",
        f"persona={session.get('persona')}  scenario={session.get('scenario')}  seed={session.get('seed')}",
        f"knowledge={session.get('knowledge_condition')}  mode={(session.get('sim_health') or {}).get('mode')}",
        f"sim_health={session.get('sim_health')}",
        f"setup={session.get('setup')}",
        "",
    ]
    for t in session.get("turns") or []:
        r = t.get("tutor_result") or {}
        lines.append(f"L ({t.get('intent')}): {t.get('learner')}")
        lines.append(
            f"T ({r.get('kind')} subj={r.get('subject_id')} active={r.get('session_active')}): "
            f"{r.get('response') or ''}"
        )
        st = r.get("stats") or {}
        j = st.get("judge") if isinstance(st.get("judge"), dict) else {}
        flags = []
        for k in (
            "judge_called", "judge_failed", "judge_truncated", "judge_evidence",
            "gate_repair", "gate_skipped", "echo_skipped", "echo_flagged",
            "off_target_streak", "concede_repair_failed", "empty_fallback", "reframe",
        ):
            if st.get(k) is not None and st.get(k) is not False:
                flags.append(f"{k}={st.get(k)}")
        if j.get("verdict") or flags:
            parts = []
            if j.get("verdict"):
                c, rp = j.get("claim"), j.get("responsive")
                extra = f" claim={c} responsive={rp}" if c or rp else ""
                parts.append(f"judge={j.get('verdict')}{extra}: {j.get('reason')}")
            if flags:
                parts.append(" | ".join(flags))
            lines.append("  " + " || ".join(parts))
        lines.append("")
    return "\n".join(lines)


def write_session_artifacts(out_dir: Path, session: dict[str, Any]) -> None:
    sid = session.get("session_id") or "session"
    sdir = out_dir / "sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "transcript.txt").write_text(format_transcript(session), encoding="utf-8")
    (sdir / "session.json").write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")


def _fresh_state(user_id: str) -> dict[str, Any]:
    """In-memory learner state; never relies on disk when save=False."""
    import tutor as T
    fn = getattr(T, "default_state", None)
    if callable(fn):
        try:
            st = fn(user_id)
        except TypeError:
            st = fn()
        if isinstance(st, dict):
            st.setdefault("learner", {})["id"] = user_id
            return st
    return {
        "learner": {"id": user_id, "name": user_id},
        "subjects": {},
        "active_session": {"active": False},
        "version": 1,
    }


def _is_active(state: dict | None) -> bool:
    if not isinstance(state, dict):
        return False
    act = state.get("active_session") or {}
    return bool(act.get("active"))


def _call(
    handle_turn,
    message: str,
    *,
    state: dict,
    conversation: list,
    user_id: str,
) -> dict[str, Any]:
    """Always pass the same in-memory state; never load from disk mid-session."""
    res = handle_turn(
        message,
        state=state,
        conversation=conversation,
        save=False,
        user_id=user_id,
        stream=False,
    )
    return res if isinstance(res, dict) else {"ok": False, "response": str(res), "kind": "error", "stats": {}}


def _setup_subject(
    handle_turn,
    scenario: dict,
    conversation: list,
    user_id: str,
    state: dict,
) -> dict[str, Any]:
    setup = scenario.get("setup") or {}
    title = setup.get("ensure_subject_title")
    goal = setup.get("pending_goal_text") or setup.get("goal")
    meta: dict[str, Any] = {"did_setup": False, "active_after": False}
    if not title:
        return meta

    r1 = _call(handle_turn, f"/new {title}", state=state, conversation=conversation, user_id=user_id)
    meta["new_subject"] = {
        "kind": r1.get("kind"),
        "subject_id": r1.get("subject_id"),
        "response": (r1.get("response") or "")[:240],
        "active": _is_active(state),
    }
    if goal:
        r2 = _call(handle_turn, goal, state=state, conversation=conversation, user_id=user_id)
        meta["goal"] = {
            "kind": r2.get("kind"),
            "subject_id": r2.get("subject_id"),
            "response": (r2.get("response") or "")[:240],
            "active": _is_active(state),
        }
        # If goal step opened yet another subject without activating teaching, try a neutral continue
        if not _is_active(state) and r2.get("kind") == "new_subject":
            r3 = _call(
                handle_turn,
                "Laten we daarmee beginnen.",
                state=state,
                conversation=conversation,
                user_id=user_id,
            )
            meta["activate"] = {
                "kind": r3.get("kind"),
                "subject_id": r3.get("subject_id"),
                "response": (r3.get("response") or "")[:240],
                "active": _is_active(state),
            }

    meta["did_setup"] = True
    meta["active_after"] = _is_active(state)
    meta["subject_id_after"] = (state.get("active_session") or {}).get("subject_id")
    return meta


def _turn_row(i: int, intent: str, msg: str, res: dict, state: dict) -> dict[str, Any]:
    stats = dict(res.get("stats") or {})
    # Surface judge meta if core only put it on the session
    act = state.get("active_session") or {}
    meta = act.get("_judge_meta") or {}
    if meta.get("called") and "judge_called" not in stats:
        stats["judge_called"] = True
    if meta.get("failed") and "judge_failed" not in stats:
        stats["judge_failed"] = True
    if meta.get("truncated") and "judge_truncated" not in stats:
        stats["judge_truncated"] = True
    j = act.get("judge")
    if isinstance(j, dict) and j.get("verdict") and not stats.get("judge"):
        stats["judge"] = dict(j)
    return {
        "turn": i,
        "intent": intent,
        "learner": msg,
        "tutor_result": {
            "ok": res.get("ok", True),
            "kind": res.get("kind"),
            "response": res.get("response"),
            "subject_id": res.get("subject_id") or act.get("subject_id"),
            "session_active": _is_active(state),
            "stats": stats,
        },
    }


def run_session(
    *,
    persona: dict[str, Any],
    scenario: dict[str, Any],
    handle_turn: Callable[..., dict] | None = None,
    max_turns: int | None = None,
    seed: int = 0,
    user_id: str = "sim_agent",
    save: bool = False,
    do_setup: bool = True,
    use_curriculum: bool = True,
    require_active: bool = True,
) -> dict[str, Any]:
    if handle_turn is None:
        import core
        handle_turn = core.handle_turn

    apply_knowledge_condition(scenario.get("knowledge_condition"))

    rng = random.Random(seed)
    sheet = dict(scenario.get("truth_sheet") or {})
    opening = (
        scenario.get("opening")
        or sheet.get("opening")
        or f"ik wil leren over {sheet.get('topic') or 'dit onderwerp'}"
    )
    sid = scenario.get("id") or scenario.get("name") or ""
    steps = curriculum_steps(sid) if use_curriculum else None
    if scenario.get("use_curriculum") is False:
        steps = None

    if steps:
        n = min(int(max_turns or scenario.get("max_turns") or len(steps)), len(steps))
    else:
        n = int(max_turns or scenario.get("max_turns") or 8)

    conversation: list[dict[str, str]] = []
    rows: list[dict[str, Any]] = []
    tutor_last = ""
    setup_meta: dict[str, Any] = {}
    memory = StudentMemory()

    # --- in-memory state for the whole session ---
    state = _fresh_state(user_id)

    if do_setup and (scenario.get("setup") or {}).get("ensure_subject_title"):
        try:
            setup_meta = _setup_subject(handle_turn, scenario, conversation, user_id, state)
        except Exception as e:
            setup_meta = {"did_setup": False, "error": str(e), "active_after": False}

        if require_active and not setup_meta.get("active_after"):
            session_id = f"{persona.get('_id') or persona.get('name')}__{sid}__seed{seed}"
            session_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)[:120]
            return {
                "session_id": session_id,
                "user_id": user_id,
                "persona": persona.get("_id") or persona.get("name"),
                "scenario": sid,
                "knowledge_condition": scenario.get("knowledge_condition") or "local",
                "score_mode": scenario.get("score_mode") or "full",
                "seed": seed,
                "setup": setup_meta,
                "setup_failed": True,
                "error": "setup did not produce active_session.active=True; refusing to run turns (stateful harness)",
                "sim_health": {"unique_learner_lines": 0, "n_turns": 0, "unique_ratio": 0.0, "mode": "aborted"},
                "turns": [],
                "metrics": summarize_metrics([]),
            }

    for i in range(n):
        if steps:
            step = steps[i]
            intent = step.get("intent") or "answer"
            msg = message_for_step(
                step,
                opening=opening,
                sheet=sheet,
                tutor_last=tutor_last,
                persona=persona,
                memory=memory,
                student_utterance_fn=student_utterance,
            )
        else:
            if i == 0:
                intent = "answer"
                msg = opening
                memory.mark(msg)
            else:
                intent = choose_intent(persona, i, rng)
                forced = scenario.get("force_intents") or []
                if i - 1 < len(forced) and forced[i - 1]:
                    intent = forced[i - 1]
                msg = student_utterance(
                    intent=intent, sheet=sheet, tutor_last=tutor_last,
                    persona=persona, memory=memory,
                )

        t0 = time.time()
        res = _call(handle_turn, msg, state=state, conversation=conversation, user_id=user_id)
        elapsed = time.time() - t0
        tutor_last = str(res.get("response") or "")
        stats = dict(res.get("stats") or {})
        stats.setdefault("wall_s", round(elapsed, 3))
        res = dict(res)
        res["stats"] = stats
        rows.append(_turn_row(i, intent, msg, res, state))

        if res.get("kind") == "subject_choice":
            res2 = _call(handle_turn, "1", state=state, conversation=conversation, user_id=user_id)
            rows.append(_turn_row(len(rows), "answer", "1", res2, state))
            tutor_last = str(res2.get("response") or "")

    unique = len({(t["learner"] or "").strip().lower()[:120] for t in rows})
    n_judge = sum(
        1 for t in rows
        if (t.get("tutor_result") or {}).get("stats", {}).get("judge_called")
        or isinstance((t.get("tutor_result") or {}).get("stats", {}).get("judge"), dict)
    )
    n_active = sum(1 for t in rows if (t.get("tutor_result") or {}).get("session_active"))
    sim_health = {
        "unique_learner_lines": unique,
        "n_turns": len(rows),
        "unique_ratio": round(unique / max(len(rows), 1), 3),
        "mode": "curriculum" if steps else "free",
        "n_judge_turns": n_judge,
        "n_active_turns": n_active,
        "setup_active": bool(setup_meta.get("active_after")),
    }

    session_id = f"{persona.get('_id') or persona.get('name')}__{sid}__seed{seed}"
    session_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)[:120]

    return {
        "session_id": session_id,
        "user_id": user_id,
        "persona": persona.get("_id") or persona.get("name"),
        "scenario": sid,
        "knowledge_condition": scenario.get("knowledge_condition") or "local",
        "score_mode": scenario.get("score_mode") or "full",
        "seed": seed,
        "setup": setup_meta,
        "sim_health": sim_health,
        "turns": rows,
        "metrics": summarize_metrics(rows),
    }


def run_batch(
    *,
    persona_names: list[str],
    scenario_names: list[str],
    seeds: list[int] | None = None,
    handle_turn: Callable[..., dict] | None = None,
    max_turns: int | None = None,
    run_id: str | None = None,
    write_run: bool = False,
    use_curriculum: bool = True,
    require_active: bool = True,
) -> dict[str, Any]:
    seeds = seeds or [0, 1]
    sessions = []
    for pn in persona_names:
        persona = load_persona(pn)
        for sn in scenario_names:
            scenario = load_scenario(sn)
            for seed in seeds:
                uid = f"sim_{pn}_{sn}_{seed}".replace("/", "_")[:64]
                sessions.append(
                    run_session(
                        persona=persona,
                        scenario=scenario,
                        handle_turn=handle_turn,
                        max_turns=max_turns,
                        seed=seed,
                        user_id=uid,
                        save=False,
                        use_curriculum=use_curriculum,
                        require_active=require_active,
                    )
                )
    all_turns = [t for s in sessions for t in s["turns"]]
    report: dict[str, Any] = {
        "n_sessions": len(sessions),
        "n_setup_failed": sum(1 for s in sessions if s.get("setup_failed")),
        "summary": summarize_metrics(all_turns),
        "sim_health": {
            "mean_unique_ratio": round(
                sum((s.get("sim_health") or {}).get("unique_ratio") or 0 for s in sessions)
                / max(len(sessions), 1),
                3,
            ),
            "sessions_low_unique": sum(
                1 for s in sessions if ((s.get("sim_health") or {}).get("unique_ratio") or 0) < 0.5
            ),
            "sessions_with_judge": sum(
                1 for s in sessions if ((s.get("sim_health") or {}).get("n_judge_turns") or 0) > 0
            ),
            "sessions_fully_active": sum(
                1
                for s in sessions
                if (s.get("sim_health") or {}).get("n_active_turns")
                == (s.get("sim_health") or {}).get("n_turns")
                and (s.get("sim_health") or {}).get("n_turns", 0) > 0
            ),
        },
        "sessions": sessions,
    }
    if write_run:
        rid = run_id or datetime.now(timezone.utc).strftime("run_%Y%m%d_%H%M%S")
        out_dir = RUNS_DIR / rid
        out_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "run_id": rid,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "personas": persona_names,
            "scenarios": scenario_names,
            "seeds": seeds,
            "max_turns": max_turns,
            "use_curriculum": use_curriculum,
            "require_active": require_active,
            "stateful_harness": True,
            "standard": "sim/SOCRATIC_STANDARD.md",
        }
        try:
            import subprocess
            manifest["git_commit"] = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT), text=True
            ).strip()
        except Exception:
            manifest["git_commit"] = None
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        with (out_dir / "sessions.jsonl").open("w", encoding="utf-8") as f:
            for s in sessions:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
                write_session_artifacts(out_dir, s)
        all_txt = []
        for s in sessions:
            all_txt.append("=" * 72)
            all_txt.append(format_transcript(s))
        (out_dir / "all_transcripts.txt").write_text("\n".join(all_txt), encoding="utf-8")
        report["all_transcripts"] = str(out_dir / "all_transcripts.txt")
        (out_dir / "summary.json").write_text(
            json.dumps(
                {
                    "n_sessions": report["n_sessions"],
                    "n_setup_failed": report["n_setup_failed"],
                    "summary": report["summary"],
                    "sim_health": report["sim_health"],
                    "session_ids": [s.get("session_id") for s in sessions],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        report["run_dir"] = str(out_dir)
        report["run_id"] = rid
    return report
