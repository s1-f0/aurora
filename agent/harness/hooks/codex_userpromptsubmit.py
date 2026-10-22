#!/usr/bin/env python3
"""Codex UserPromptSubmit: subject-labelled plan recall and non-consuming mail cue."""

from __future__ import annotations

import json
import os
import sys

# Run as a bare script (by .codex/hooks.json or the scripts/hooks/ shim), so the repo root is
# not on sys.path until we put it there.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from agent.harness.hooks.codex_common import (
    capture_payload,
    dedup_should_skip,
    event_seat,
    subject_label,
)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception as exc:
        print(f"[codex-plan-recall] stdin unparseable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
    try:
        if dedup_should_skip(data):
            return 0
        capture_payload(data)
        from agent.harness.scope import session_in_scope

        if not session_in_scope(str(data.get("cwd") or os.getcwd())):
            return 0
        seat = event_seat(data)
        sid = str(data.get("session_id") or "")
        from agent.harness.hooks.claude_userpromptsubmit import (
            build_bus_line,
            build_page_lines,
            build_plan_recall,
            build_verb_line,
        )

        pieces = [
            build_plan_recall(str(data.get("prompt") or ""), sid, seat),
            build_verb_line(str(data.get("prompt") or ""), sid, seat),
            build_bus_line(seat),
            *build_page_lines(),
        ]
        pieces = [piece for piece in pieces if piece]
        if pieces:
            context = subject_label(data, seat) + "\n" + "\n".join(pieces)
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "UserPromptSubmit",
                            "additionalContext": context,
                        }
                    }
                )
            )
    except Exception as exc:
        print(f"[codex-plan-recall] suppressed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
