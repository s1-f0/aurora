"""PIN: a hook registered on two surfaces does its work once per event.

MEASURED 2026-10-19 on a Linux box set up with `aurora setup`: the user-level hooks
(~/.claude/settings.json, run through the `aurora` launcher) and the committed project hooks
(<repo>/.claude/settings.json, run through `uv run`) BOTH register claude_trace, claude_posttooluse
and claude_stop, so in this repo every tool call was traced twice, every outcome credited twice
(the funnel's surfaced/helped counters run ~2x hot), and every turn end judged twice.
claude_pretooluse already carried the K0/C8-3 same-payload guard; these three did not.

The shared guard is agent/harness/dedup.py:first_fire(event, payload). Both registrations receive
the identical payload, so the first process to create the marker does the work and the second
returns quietly. The marker is keyed by the EVENT too: the same payload seen by PostToolUse and by
PostToolUseFailure, or by two different hooks, must never suppress each other.

Run: uv run pytest tests/test_hook_double_registration_dedup.py -q -p no:cacheprovider
"""

from __future__ import annotations

import ast
import os
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

HOOKS = os.path.join(ROOT, "agent", "harness", "hooks")


@pytest.fixture
def dedup(tmp_path, monkeypatch):
    from agent.harness import dedup as mod

    monkeypatch.setattr(mod, "_marker_dir", lambda: str(tmp_path))
    return mod


def _payload(**extra):
    return {"session_id": uuid.uuid4().hex, "tool_name": "Bash", "tool_input": {"command": "ls"}, **extra}


def test_second_identical_fire_is_skipped(dedup):
    p = _payload()
    assert dedup.first_fire("PostToolUse", p) is True
    assert dedup.first_fire("PostToolUse", p) is False


def test_the_event_is_part_of_the_key(dedup):
    p = _payload()
    assert dedup.first_fire("PostToolUse", p) is True
    assert dedup.first_fire("PostToolUseFailure", p) is True
    assert dedup.first_fire("Stop", p) is True


def test_two_real_identical_calls_both_fire(dedup):
    """Heimdall's review finding 1 (2026-10-19): two GENUINE identical calls -- the same Read twice
    within three seconds -- must not collapse. Claude Code stamps each tool call with its own
    tool_use_id, and the key hashes the whole payload, so they differ; both registrations of ONE
    call carry the same id and still collapse."""
    first = _payload(tool_use_id="toolu_A")
    again = dict(first, tool_use_id="toolu_B")
    assert dedup.first_fire("PostToolUse", first) is True
    assert dedup.first_fire("PostToolUse", again) is True
    assert dedup.first_fire("PostToolUse", dict(first)) is False


def test_different_payloads_both_fire(dedup):
    assert dedup.first_fire("PostToolUse", _payload()) is True
    assert dedup.first_fire("PostToolUse", _payload()) is True


def test_an_old_marker_does_not_suppress_a_new_fire(dedup, monkeypatch):
    p = _payload()
    assert dedup.first_fire("Stop", p) is True
    real = dedup.time.time
    monkeypatch.setattr(dedup.time, "time", lambda: real() + dedup.WINDOW_S + 1)
    assert dedup.first_fire("Stop", p) is True


def test_fails_open(dedup, monkeypatch):
    """A guard that can swallow the only fire is worse than a double count."""
    monkeypatch.setattr(dedup, "_marker_dir", lambda: "/proc/definitely/not/writable")
    p = _payload()
    assert dedup.first_fire("Stop", p) is True
    assert dedup.first_fire("Stop", p) is True


def _main_calls(path: str) -> set[str]:
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    out = set()
    for node in ast.walk(main):
        if isinstance(node, ast.Call):
            f = node.func
            out.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return out


@pytest.mark.parametrize("hook", ["claude_trace.py", "claude_posttooluse.py", "claude_stop.py"])
def test_double_registered_hooks_call_the_guard(hook):
    assert "first_fire" in _main_calls(os.path.join(HOOKS, hook)), (
        f"{hook} is registered on both the user and the project surface; its main() must call "
        "agent.harness.dedup.first_fire before doing any work"
    )
