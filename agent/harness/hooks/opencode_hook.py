#!/usr/bin/env python3
"""OpenCode adapter boundary -- one translator between an OpenCode event and the shared core.

OpenCode has no Claude-shaped hooks.json. Its plugin API (agent/harness/plugins/opencode/
index.ts) emits the payload defined here and applies whatever context comes back. This module
is the explicit OpenCode boundary, in the same spirit as ``codex_common.py``: it maps OpenCode
tool names and outcome shapes onto the core seams the Claude and Codex adapters already use, and
it never re-implements recall, scope or identity.

    python agent/harness/hooks/opencode_hook.py --event SessionStart        < payload.json
    python agent/harness/hooks/opencode_hook.py --event UserPromptSubmit    < payload.json
    python agent/harness/hooks/opencode_hook.py --event PreToolUse          < payload.json
    python agent/harness/hooks/opencode_hook.py --event PostToolUse         < payload.json
    python agent/harness/hooks/opencode_hook.py --event SessionEnd          < payload.json

Payload (all fields optional except ``event``)::

    {
        "session_id": str,
        "cwd": str,
        "event": str,
        "prompt": str,
        "tool_name": str,
        "tool_input": object,
        "tool_status": "completed" | "error",
        "tool_output": str,
        "agent": str,
    }

Output is always JSON on stdout, and always exit 0::

    {"context": str, "deny": str, "targets": [str]}

``context`` is "" when the house has nothing to say. A non-empty ``deny`` is the only blocking
path (a PreToolUse veto such as the git-staging guard); the plugin treats it as a refusal.

FAIL-OPEN is the hook contract: every seam is wrapped, and an exception degrades to silence
rather than to a broken session.
"""

from __future__ import annotations

import json
import os
import sys

# Run as a bare script from an OpenCode plugin, so the repo root is not on sys.path yet.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

#: OpenCode's built-in tool ids that touch a file or a shell.
FILE_TOOLS = {"read", "write", "edit", "patch", "multiedit", "apply_patch"}
SHELL_TOOLS = {"shell", "bash", "sh"}
PATH_KEYS = ("path", "filePath", "file_path")
COMMAND_KEYS = ("command", "cmd")
PATCH_KEYS = ("patchText", "patch", "input")


# --------------------------------------------------------------------------- payload helpers
def _ti(data: dict) -> dict:
    """tool_input as a dict, never a crash. A truthy non-dict is the bug the Claude adapter
    already paid for once (claude_pretooluse.py::_in_scope)."""
    ti = data.get("tool_input")
    return ti if isinstance(ti, dict) else {}


def tool_name(data: dict) -> str:
    return str(data.get("tool_name") or "").strip().lower()


def tool_path(data: dict) -> str:
    ti = _ti(data)
    for key in PATH_KEYS:
        value = ti.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def tool_command(data: dict) -> str:
    ti = _ti(data)
    for key in COMMAND_KEYS:
        value = ti.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def patch_paths(data: dict) -> list[str]:
    """Paths named by a unified patch body, bounded and best-effort."""
    ti = _ti(data)
    body = ""
    for key in PATCH_KEYS:
        value = ti.get(key)
        if isinstance(value, str) and value.strip():
            body = value
            break
    if not body:
        return []
    cwd = str(data.get("cwd") or os.getcwd())
    out: list[str] = []
    for line in body.splitlines():
        for prefix in ("*** Add File:", "*** Update File:", "*** Delete File:", "*** Move to:"):
            if line.startswith(prefix):
                raw = line[len(prefix) :].strip().strip('"')
                if raw:
                    path = raw if os.path.isabs(raw) else os.path.join(cwd, raw)
                    path = os.path.normpath(path)
                    if path not in out:
                        out.append(path)
    return out


def action_paths(data: dict) -> list[str]:
    name = tool_name(data)
    if name in SHELL_TOOLS:
        return []
    if name in ("patch", "apply_patch"):
        return patch_paths(data)
    if name in FILE_TOOLS:
        single = tool_path(data)
        return [single] if single else []
    return []


def is_success(data: dict) -> bool:
    """OpenCode's execute.after status is the direct signal -- no transcript synthesis."""
    status = str(data.get("tool_status") or "").strip().lower()
    if status:
        return status in ("completed", "success", "ok")
    response = data.get("tool_response")
    if isinstance(response, dict):
        if response.get("is_error") is True or response.get("isError") is True:
            return False
        for key in ("exit_code", "exitCode", "returncode"):
            if key in response:
                try:
                    return int(response[key]) == 0
                except (TypeError, ValueError):
                    return False
    return True


def seat(data: dict) -> str:
    try:
        from core.comm.seat_identity import resolve

        return str(resolve(str(data.get("session_id") or "")) or "opencode")
    except Exception:
        return (os.getenv("AKASHIC_AGENT_ID") or "").strip() or "opencode"


def declare_seat(data: dict) -> str:
    """Write the session binding at first breath so later resolve() calls are session-scoped."""
    sid = str(data.get("session_id") or "")
    agent = (os.getenv("AKASHIC_AGENT_ID") or "").strip() or "opencode"
    try:
        from core.comm.seat_identity import declare

        if sid:
            declare(agent, sid)
    except Exception:
        pass
    return seat(data)


def in_scope(data: dict) -> bool:
    try:
        from agent.harness.scope import file_in_scope, session_in_scope, shell_in_scope

        name = tool_name(data)
        if name in SHELL_TOOLS:
            return shell_in_scope(str(data.get("cwd") or os.getcwd()), tool_command(data))
        paths = action_paths(data)
        if paths:
            return any(file_in_scope(path) for path in paths)
        return session_in_scope(str(data.get("cwd") or os.getcwd()))
    except Exception:
        return False


def touch_working(data: dict) -> None:
    """Liveness belongs above adapter filters: a silent recall is still a live seat."""
    sid = str(data.get("session_id") or "")
    agent = seat(data)
    if not sid or agent in ("", "unknown"):
        return
    try:
        from core.comm import roster, wake_seat
        from core.comm.bus import NS

        roster.heartbeat(NS, agent, sid, phase="working")
        wake_seat.touch_activity(agent, sid)
    except Exception:
        pass


def capture_payload(data: dict) -> None:
    try:
        from agent.harness.capture import capture

        label = f"opencode-{str(data.get('event') or 'event').lower()}-{tool_name(data) or 'lifecycle'}"
        capture(data, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".capture"), label=label)
    except Exception:
        pass


# --------------------------------------------------------------------------- event handlers
def on_session_start(data: dict) -> dict:
    sid = str(data.get("session_id") or "")
    agent = declare_seat(data)
    try:
        from core.recall.at_action import prune_state, warm_cache

        warm_cache()
        prune_state()
    except Exception:
        pass
    try:
        from core.fleet import provenance

        provenance.ensure(data, session_id=sid, agent_id=agent)
    except Exception:
        pass
    try:
        from core.comm import incarnation, wake_seat

        if sid:
            wake_seat.clear_tombstone(sid)
            wake_seat.touch_activity(agent, sid)
            incarnation.publish_card(agent, sid)
    except Exception:
        pass
    context = ""
    try:
        from agent.harness.context import build_autoboot_context

        context = build_autoboot_context(str(data.get("cwd") or os.getcwd()), agent, session_id=sid) or ""
    except Exception:
        context = ""
    return {"context": context}


def on_user_prompt(data: dict) -> dict:
    try:
        from agent.harness.scope import session_in_scope

        if not session_in_scope(str(data.get("cwd") or os.getcwd())):
            return {"context": ""}
    except Exception:
        return {"context": ""}
    sid = str(data.get("session_id") or "")
    agent = seat(data)
    pieces: list[str] = []
    try:
        from agent.harness.actions import plan_block

        pieces.append(plan_block(str(data.get("prompt") or ""), sid, sid, agent_id=agent))
    except Exception:
        pass
    try:
        from agent.harness.verbs import verb_plan_line

        pieces.append(verb_plan_line(str(data.get("prompt") or ""), sid, agent, sid))
    except Exception:
        pass
    try:
        from agent.harness.context import _unread_count

        unread = _unread_count(agent)
        if unread:
            pieces.append(f"[akashic] mail: {unread} unread bus msg(s) -> aurora bifrost-sync {agent}")
    except Exception:
        pass
    try:
        from core.comm import pager

        pieces.extend(pager.hook_lines())
    except Exception:
        pass
    context = "\n".join(piece for piece in pieces if piece)
    return {"context": context}


def on_pre_tool(data: dict) -> dict:
    name = tool_name(data)
    if not name or name.startswith("akashic"):
        return {"context": ""}
    if not in_scope(data):
        return {"context": ""}
    sid = str(data.get("session_id") or "")
    command = tool_command(data) if name in SHELL_TOOLS else ""
    paths = action_paths(data)
    path = paths[0] if paths else ""
    # LOCAL GUARDS FIRST. A veto is a safety decision and must not wait on the shared bus: when
    # Redis accepted the connection but stopped answering, this call used to burn the whole
    # deadline before the git-staging guard was ever evaluated.
    deny = ""
    if command:
        try:
            from agent.harness.guards import git_veto

            deny = git_veto(command) or ""
        except Exception:
            deny = ""
    if deny:
        return {"context": "", "deny": deny, "targets": paths}
    touch_working(data)
    context = ""
    try:
        from agent.harness.actions import recall_block

        context = recall_block(sid, sid, path or None, command or None) or ""
    except Exception:
        context = ""
    return {"context": context, "targets": paths}


def on_post_tool(data: dict) -> dict:
    touch_working(data)
    name = tool_name(data)
    if not name or name.startswith("akashic"):
        return {"context": "", "targets": []}
    if not in_scope(data):
        return {"context": "", "targets": []}
    sid = str(data.get("session_id") or "")
    agent = seat(data)
    ok = is_success(data)
    try:
        from core.recall.at_action import normalize_target

        if name in SHELL_TOOLS:
            target = normalize_target(command=tool_command(data))
            targets = [target] if target else []
        else:
            targets = [normalize_target(path=path) for path in action_paths(data)]
    except Exception:
        targets = []
    for target in targets:
        try:
            from core.recall.at_action import resolve_action_outcome

            resolve_action_outcome(sid, target, ok, agent_id=agent)
        except Exception:
            pass
        if not ok:
            try:
                from core.events.event_log import capture_event

                capture_event(
                    "fail",
                    f"FAIL: {target}",
                    agent_id=agent,
                    detail={"target": target, "tool": name, "harness": "opencode"},
                )
            except Exception:
                pass
    return {"context": "", "targets": targets}


def on_session_end(data: dict) -> dict:
    sid = str(data.get("session_id") or "")
    agent = seat(data)
    try:
        import agent_cli
        from core.learning.agent_memory import get_agent_memory

        commits = agent_cli._recent_commits(24)
        lessons = agent_cli._recent_lessons(8)
        notes = get_agent_memory().get_decisions(days=1)
        try:
            from core.recall.at_action import recent_flips, recent_injections

            flips, injections = recent_flips(24), recent_injections(24)
        except Exception:
            flips, injections = [], []
        agent_cli.write_last_session_draft(
            agent_cli.last_session_draft_path(),
            commits,
            lessons,
            notes,
            trigger="session end (opencode)",
            flips=flips,
            injections=injections,
        )
    except Exception:
        pass
    try:
        from core.comm.session_exit import clean_death

        clean_death(agent, sid, event="SessionEnd")
    except Exception:
        pass
    return {"context": ""}


HANDLERS = {
    "SessionStart": on_session_start,
    "UserPromptSubmit": on_user_prompt,
    "PreToolUse": on_pre_tool,
    "PostToolUse": on_post_tool,
    "SessionEnd": on_session_end,
}

#: Per-event wall clock. The recall seams talk to the shared bus, and the embedded Redis has
#: been observed to accept a connection and then never answer a command -- a hook that waits on
#: it stalls the agent. These budgets keep a degraded backend a short silence instead of a hang.
#: Healthy numbers (measured): PreToolUse ~0.25 s, UserPromptSubmit ~3.6 s (plan recall).
DEADLINES = {
    "SessionStart": 8.0,
    "UserPromptSubmit": 5.0,
    "PreToolUse": 1.5,
    "PostToolUse": 1.5,
    "SessionEnd": 5.0,
}
DEFAULT_DEADLINE = 2.0
DEADLINES_MAX = max(DEADLINES.values())


def handle(event: str, data: dict) -> dict:
    """Run one handler and normalise the envelope. Never raises."""
    handler = HANDLERS.get(event)
    if handler is None:
        return {"context": ""}
    try:
        result = handler(data)
    except Exception as exc:  # fail-open: silence beats a broken session
        print(f"[opencode-hook] suppressed {event}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return {"context": ""}
    if not isinstance(result, dict):
        return {"context": ""}
    result.setdefault("context", "")
    if result.get("deny"):
        result["deny"] = str(result["deny"])
    return result


def bounded(event: str, data: dict) -> dict:
    """handle() under a hard deadline. A stuck seam yields silence, never a stalled agent."""
    import threading

    box: dict = {"result": {"context": ""}}

    def work() -> None:
        box["result"] = handle(event, data)

    thread = threading.Thread(target=work, daemon=True)
    deadline = DEADLINES.get(event, DEFAULT_DEADLINE)
    thread.start()
    thread.join(deadline)
    if thread.is_alive():
        # The handler may be blocked inside a C call where no Python timeout can interrupt it.
        # It is a daemon, so the process can still print and leave immediately.
        print(f"[opencode-hook] deadline {deadline}s exceeded for {event}", file=sys.stderr)
        return {"context": ""}
    return box["result"]


def serve() -> int:
    """Long-lived mode: one JSON request per line, one JSON response per line.

    The plugin keeps this process warm so each event pays neither interpreter start nor module
    import. Requests may overlap; each is handled on its own thread and stdout is written under
    a lock so lines never interleave.
    """
    import threading

    lock = threading.Lock()
    threads: list = []

    def reply(req_id: object, result: dict) -> None:
        line = json.dumps({"id": req_id, "result": result})
        with lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def work(req: dict) -> None:
        event = str(req.get("event") or "")
        payload = req.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        payload.setdefault("event", event)
        reply(req.get("id"), bounded(event, payload))

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        if not isinstance(req, dict):
            continue
        thread = threading.Thread(target=work, args=(req,), daemon=True)
        thread.start()
        threads.append(thread)
    # stdin closed (plugin unloading): let in-flight requests answer, but never wait forever.
    # Each handler is already inside bounded(), so this join is bounded too.
    for thread in threads:
        thread.join(DEADLINES_MAX + 1)
    return 0


def main(argv: list[str]) -> int:
    if "--serve" in argv:
        return serve()
    event = ""
    for i, arg in enumerate(argv):
        if arg == "--event" and i + 1 < len(argv):
            event = argv[i + 1]
        elif arg.startswith("--event="):
            event = arg.split("=", 1)[1]
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("event", event)
    event = event or str(data.get("event") or "")
    print(json.dumps(bounded(event, data)))
    sys.stdout.flush()
    # A daemon thread stuck in a C call cannot be joined out of existence; leaving now is the
    # whole point of the deadline.
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
