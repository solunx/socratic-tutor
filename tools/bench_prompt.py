#!/usr/bin/env python3
"""Dry-run prompt size bench (no LLM required).

  cd ~/dev/tutor && source .venv/bin/activate
  python tools/bench_prompt.py
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tutor as T
from metrics_lib import format_breakdown, get_last_breakdown


CASES = [
    {"name": "meta_no_session", "active": False, "conv": []},
    {"name": "teach_empty_hist", "active": True, "conv": []},
    {
        "name": "teach_12_msgs",
        "active": True,
        "conv": [
            {"role": "user", "content": f"antwoord {i} " + ("x" * 80)}
            for i in range(6)
        ]
        + [
            {"role": "assistant", "content": f"vraag {i}? " + ("y" * 120)}
            for i in range(6)
        ],
    },
]


def main():
    state = T.load_state()
    base = T.load_system_prompt()
    print("Model:", T.MODEL_NAME)
    print("MAX_CONVERSATION_MESSAGES:", T.MAX_CONVERSATION_MESSAGES)
    print()
    for case in CASES:
        st = T.default_state() if not state.get("subjects") else state
        # shallow copy active flags
        st = dict(st)
        st["active_session"] = dict(st.get("active_session") or {})
        if case["active"] and st.get("subjects"):
            sid = next(iter(st["subjects"]))
            st["active_session"]["active"] = True
            st["active_session"]["subject_id"] = sid
            concepts = (st["subjects"][sid].get("concepts") or {})
            if concepts:
                st["active_session"]["current_concept_id"] = next(iter(concepts))
        else:
            st["active_session"]["active"] = False
        policy = T.suggest_next_activity(st)
        T.build_messages(base, st, case["conv"], policy=policy)
        bd = get_last_breakdown() or {}
        print(f"=== {case['name']} ===")
        print(format_breakdown(bd.get("components") or [], bd.get("total_tokens_est")))
        print()


if __name__ == "__main__":
    main()
