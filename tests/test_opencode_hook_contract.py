"""Pins for the OpenCode adapter boundary.

Three things must not drift silently:
  * the registry row states the tiers this adapter actually reaches -- and says "one-beat-late"
    where OpenCode genuinely cannot inject before a tool runs;
  * OpenCode tool names map onto the same act/path vocabulary the Claude and Codex adapters use,
    because recall and outcome credit are keyed by that vocabulary;
  * the installer writes the plugin path and the MCP server, idempotently, into opencode.json.

Run: uv run pytest tests/test_opencode_hook_contract.py -q
"""

from __future__ import annotations

import json

from agent.harness import install, registry
from agent.harness.hooks import opencode_hook


def test_registry_row_is_honest_about_each_tier():
    row = registry.HARNESSES["opencode"]
    assert row["default_agent_id"] == "opencode"
    assert "plugins/opencode" in row["adapters"]
    # Reached without the agent doing it by hand.
    for tier in ("T0", "T1", "T2", "T4", "T5", "T6"):
        assert registry.supported("opencode", tier), tier
    # T3 cannot inject before the tool call: the row must say one beat late, not "yes".
    assert row["tiers"]["T3"].startswith("one-beat-late"), row["tiers"]["T3"]
    # T4 is direct because execute.after reports status -- no transcript reconstruction.
    assert row["tiers"]["T4"].startswith("yes, DIRECT")
    assert "no transcript fold" in row["tiers"]["T6"]


def test_opencode_is_an_installable_harness_with_no_hook_specs():
    assert "opencode" in install.HARNESS_NAMES
    assert registry.HOOK_SPECS["opencode"] == []
    assert install.target_file("opencode", "user").as_posix().endswith(".config/opencode/opencode.json")


def test_tool_names_map_onto_the_shared_vocabulary():
    shell = {"tool_name": "shell", "tool_input": {"command": "git status"}}
    assert opencode_hook.tool_command(shell) == "git status"
    assert opencode_hook.action_paths(shell) == []

    edit = {"tool_name": "edit", "tool_input": {"path": "core/x.py"}}
    assert opencode_hook.action_paths(edit) == ["core/x.py"]

    # V2 uses `path`; older shapes used filePath/file_path. All three are accepted.
    for key in ("path", "filePath", "file_path"):
        assert opencode_hook.action_paths({"tool_name": "write", "tool_input": {key: "a.py"}}) == ["a.py"]

    # A truthy non-dict tool_input must not crash the boundary (the Claude adapter paid for
    # this once); it degrades to "no information".
    assert opencode_hook.tool_path({"tool_name": "edit", "tool_input": "oops"}) == ""
    assert opencode_hook.tool_command({"tool_name": "shell", "tool_input": 7}) == ""


def test_patch_paths_come_out_absolute():
    data = {
        "tool_name": "patch",
        "cwd": "/tmp/work",
        "tool_input": {"patchText": "*** Add File: notes/a.md\n+x\n*** Update File: /etc/b\n+y\n"},
    }
    paths = opencode_hook.action_paths(data)
    assert paths == ["/tmp/work/notes/a.md", "/etc/b"]


def test_outcome_is_read_from_the_direct_status():
    assert opencode_hook.is_success({"tool_status": "completed"}) is True
    assert opencode_hook.is_success({"tool_status": "error"}) is False
    assert opencode_hook.is_success({"tool_response": {"is_error": True}}) is False
    assert opencode_hook.is_success({"tool_response": {"exit_code": 1}}) is False
    assert opencode_hook.is_success({"tool_response": {"exit_code": 0}}) is True


def test_main_is_total_and_always_json(monkeypatch, capsys):
    import io
    import sys

    for event in ("", "NotAnEvent"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"event": event})))
        assert opencode_hook.main([] if not event else ["--event", event]) == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert "context" in payload


def test_main_dispatches_known_events(monkeypatch, capsys):
    import io
    import sys

    monkeypatch.setitem(opencode_hook.HANDLERS, "SessionEnd", lambda data: {"context": "closed"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    assert opencode_hook.main(["--event", "SessionEnd"]) == 0
    assert json.loads(capsys.readouterr().out.strip())["context"] == "closed"


def test_a_raising_handler_still_emits_json(monkeypatch, capsys):
    import io
    import sys

    def boom(data):
        raise RuntimeError("seam exploded")

    monkeypatch.setitem(opencode_hook.HANDLERS, "PreToolUse", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert opencode_hook.main(["--event", "PreToolUse"]) == 0
    assert json.loads(capsys.readouterr().out.strip())["context"] == ""


def test_bounded_abandons_a_handler_that_overruns_its_deadline(monkeypatch):
    """The embedded Redis has accepted a connection and then never answered. A hook that waits
    on it stalls the agent, so an overrunning seam must become silence on a hard clock."""
    import time

    monkeypatch.setitem(opencode_hook.DEADLINES, "PreToolUse", 0.2)
    monkeypatch.setitem(opencode_hook.HANDLERS, "PreToolUse", lambda data: time.sleep(3) or {"context": "late"})
    started = time.time()
    result = opencode_hook.bounded("PreToolUse", {})
    elapsed = time.time() - started
    assert result == {"context": ""}
    assert elapsed < 1.5, f"deadline did not hold: {elapsed:.1f}s"


def test_local_veto_does_not_wait_on_the_bus(monkeypatch):
    """A git-staging veto is a safety decision: it must fire even when every bus seam hangs."""
    import time

    import agent.harness.guards as guards

    monkeypatch.setattr(opencode_hook, "in_scope", lambda data: True)
    monkeypatch.setattr(opencode_hook, "touch_working", lambda data: time.sleep(5))
    monkeypatch.setattr(guards, "git_veto", lambda command: "BLOCKED: blanket git staging")
    started = time.time()
    result = opencode_hook.on_pre_tool(
        {"session_id": "s1", "cwd": "/tmp", "tool_name": "shell", "tool_input": {"command": "git add -A"}}
    )
    elapsed = time.time() - started
    assert result["deny"] == "BLOCKED: blanket git staging"
    assert elapsed < 1.0, f"the veto waited on the bus: {elapsed:.1f}s"


def test_install_opencode_writes_plugin_and_mcp_idempotently(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "_launcher", lambda: "/opt/aurora/bin/aurora")
    first = install.install_opencode(scope="project", project=tmp_path)
    assert first.changed
    doc = json.loads((tmp_path / "opencode.json").read_text())
    assert doc["plugins"] == [install.opencode_plugin_path()]
    server = doc["mcp"]["servers"]["akashic-aurora"]
    assert server["command"] == ["/opt/aurora/bin/aurora", "mcp"]
    assert server["environment"]["AKASHIC_AGENT_ID"] == "opencode"

    second = install.install_opencode(scope="project", project=tmp_path)
    assert not second.changed, "a second install must be a no-op"
    assert second.added == []


def test_install_opencode_preserves_foreign_config(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "_launcher", lambda: "/opt/aurora/bin/aurora")
    (tmp_path / "opencode.json").write_text(
        json.dumps({"model": "x/y", "plugins": ["other-plugin"], "mcp": {"servers": {"keep": {"type": "remote"}}}})
    )
    install.install_opencode(scope="project", project=tmp_path)
    doc = json.loads((tmp_path / "opencode.json").read_text())
    assert doc["model"] == "x/y"
    assert doc["plugins"][0] == "other-plugin"
    assert "keep" in doc["mcp"]["servers"]
