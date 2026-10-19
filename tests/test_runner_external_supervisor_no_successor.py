"""Under an external supervisor, a stale runner hands back to its supervisor; it never forks one.

THE BUG (adversarial review, verified): `bifrost_daemon.py --external-supervisor` only disabled the
DAEMON's own stale-code self-restart. The runner it manages still ran self_restart at its loop top:
when the tree moved ahead it launched a DETACHED successor (respawn_self) and exited 0. That
successor belongs to nobody -- outside the systemd unit's supervision, outside the daemon's
ManagedChild (which reads exit 0 as "stay down" and then idles on the foreign lock holder).

THE CONTRACT, pinned here:
  P1  self_restart.maybe_self_restart(..., respawn=False) decides exactly as before but launches
      nothing, and still returns the reason so the caller stands down.
  P2  the daemon hands the signal to its runner EXPLICITLY: managed_runner_env sets
      AKASHIC_EXTERNAL_SUPERVISOR=1 only under --external-supervisor (and strips an inherited one
      otherwise), and main() passes its flag at every runner spawn.
  P3  the real DeepSeek runner, given that signal and proven stale, launches NO successor and
      exits ManagedChild.HANDOVER_EXIT (7, "respawn me now"), with its lock released.
  P4  without the signal nothing changes: the runner still launches its successor and exits 0.
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from core.comm import self_restart as SR

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "scripts" / "bifrost_runner_deepseek.py"
DAEMON = REPO / "scripts" / "bifrost_daemon.py"
# The wire contract between daemon and runner is this exact variable name, spelled out here so a
# rename on one side cannot pass silently.
ENV_KEY = "AKASHIC_EXTERNAL_SUPERVISOR"


# ------------------------------------------------------------------------------------- P1
def _force_stale(monkeypatch):
    monkeypatch.setattr(
        SR, "gather", lambda agent: {"stamped_sha": "a" * 12, "head_sha": "b" * 12, "commits_behind": 7}
    )
    monkeypatch.setattr(SR, "_min_behind", lambda: 1)
    monkeypatch.setattr(SR, "_min_uptime_s", lambda: 0.0)
    monkeypatch.setattr(SR, "uptime_s", lambda: 10_000.0)
    monkeypatch.delenv("AKASHIC_SELF_RESTART", raising=False)
    import core.comm.liveness as _liv

    monkeypatch.setattr(_liv, "worklive", lambda agent: type("_FakeLive", (), {"set": lambda *a, **k: None})())


def test_p1_supervised_self_restart_decides_but_launches_nothing(monkeypatch):
    launched = []
    monkeypatch.setattr(SR, "respawn_self", lambda argv=None: launched.append(argv) or True)
    _force_stale(monkeypatch)

    reason = SR.maybe_self_restart("supervised-seat", in_flight=False, respawn=False)

    assert launched == [], "an externally supervised runner must NEVER launch a detached successor"
    assert reason, "the decision still fires, so the caller stands down for its supervisor"
    assert "stale-code" in reason
    assert SR.maybe_self_restart("supervised-seat", in_flight=True, respawn=False) is None, (
        "in-flight work still vetoes the restart"
    )


def test_p1b_unsupervised_default_is_unchanged(monkeypatch):
    launched = []
    monkeypatch.setattr(SR, "respawn_self", lambda argv=None: launched.append(argv) or True)
    _force_stale(monkeypatch)

    assert SR.maybe_self_restart("free-seat", in_flight=False)
    assert launched == [None], "the default (no supervisor) still launches the successor, exactly as before"


def test_p1c_the_signal_is_explicit_never_inferred():
    env = SR.EXTERNAL_SUPERVISOR_ENV
    assert env == ENV_KEY
    assert SR.externally_supervised({env: "1"}) is True
    for value in ("", "0", "true", "yes", " "):
        assert SR.externally_supervised({env: value}) is False, f"only the daemon's exact '1' counts, not {value!r}"
    assert SR.externally_supervised({}) is False


# ------------------------------------------------------------------------------------- P2
def test_p2_daemon_env_carries_the_flag_only_when_it_is_externally_supervised():
    from scripts import bifrost_daemon as daemon

    key = SR.EXTERNAL_SUPERVISOR_ENV
    on = daemon.managed_runner_env("deepseek", base={"KEEP": "yes"}, external_supervisor=True)
    assert on[key] == "1"
    assert on["KEEP"] == "yes"

    off = daemon.managed_runner_env("deepseek", base={"KEEP": "yes", key: "1"}, external_supervisor=False)
    assert key not in off, "an unsupervised daemon must not pass on a signal it merely inherited"
    assert key not in daemon.managed_runner_env("deepseek", base={}), "default stays unsupervised"


def test_p2b_daemon_main_passes_its_flag_at_every_runner_spawn():
    tree = ast.parse(DAEMON.read_text(encoding="utf-8"))
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [
        n
        for n in ast.walk(main)
        if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == "managed_runner_env"
    ]
    assert len(calls) >= 2, "main() builds the runner env at boot AND at the W102 reclaim -- re-derive this pin"
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        val = kw.get("external_supervisor")
        msg = (
            f"managed_runner_env call at line {call.lineno} does not pass external_supervisor=args.external_supervisor"
        )
        assert isinstance(val, ast.Attribute), msg
        assert val.attr == "external_supervisor", msg


# ------------------------------------------------------------------------------------- P3 / P4
# A real DeepSeek runner (drill-echo, offline) with ONLY the staleness facts and the spawn stubbed:
# gather() says "9 commits behind", uptime clears the floor, and respawn_self records instead of
# forking. Everything else -- the loop, the decision, the exit path -- is the production code.
_STALE_WRAPPER = textwrap.dedent(
    """
    import runpy, sys
    repo, runner, marker = sys.argv[1], sys.argv[2], sys.argv[3]
    sys.path.insert(0, repo)
    from core.comm import self_restart as SR

    SR.gather = lambda agent: {"stamped_sha": "a" * 12, "head_sha": "b" * 12, "commits_behind": 9}
    SR.uptime_s = lambda: 10_000.0

    def _respawn(argv=None):
        with open(marker, "w", encoding="utf-8") as f:
            f.write("successor launched")
        return True

    SR.respawn_self = _respawn
    sys.argv = [runner, *sys.argv[4:]]
    runpy.run_path(runner, run_name="__main__")
    """
)


def _run_stale_runner(tmp_path, *, supervised: bool):
    from core.comm.bus import Bus

    agent = f"extsup-{uuid.uuid4().hex[:8]}"
    b = Bus(agent)
    if not b.online:
        pytest.skip("redis not available")
    wrapper = tmp_path / "stale_runner.py"
    wrapper.write_text(_STALE_WRAPPER, encoding="utf-8")
    marker = tmp_path / "successor.txt"
    summary = tmp_path / "summary.json"
    env = dict(os.environ, AKASHIC_DRILL_ECHO="1", DEEPSEEK_API_KEY="drill-offline-placeholder")
    for k in ("AKASHIC_KILLPOINT", "AKASHIC_SELF_RESTART", ENV_KEY):
        env.pop(k, None)
    if supervised:
        env[ENV_KEY] = "1"
    try:
        p = subprocess.run(
            [
                sys.executable,
                str(wrapper),
                str(REPO),
                str(RUNNER),
                str(marker),
                "--agent",
                agent,
                "--summary-file",
                str(summary),
            ],
            env=env,
            cwd=str(REPO),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        from core.comm import runner_lock

        holder = runner_lock.holder(agent)
    finally:
        with contextlib.suppress(Exception):
            b._client.delete(
                b._cursor_key(), f"{b.ns}:generation:{agent}", f"{b.ns}:runner:{agent}", b._inbox_key(agent)
            )
    s = json.loads(summary.read_text(encoding="utf-8")) if summary.exists() else {}
    return p, marker.exists(), holder, s


def test_p3_supervised_runner_refuses_to_fork_and_hands_back_with_exit_7(tmp_path):
    from scripts.bifrost_child import ManagedChild

    p, forked, holder, summary = _run_stale_runner(tmp_path, supervised=True)
    out = (p.stdout or "")[-1500:]
    assert not forked, f"an externally supervised runner launched a detached successor anyway:\n{out}"
    assert p.returncode == ManagedChild.HANDOVER_EXIT, (
        f"the supervised stand-down must exit {ManagedChild.HANDOVER_EXIT} (ManagedChild: respawn me now); "
        f"got {p.returncode}:\n{out}"
    )
    assert holder is None, f"the stand-down must release the runner lock for its replacement: {holder}"
    assert summary.get("exit_code") == ManagedChild.HANDOVER_EXIT, summary


def test_p4_unsupervised_runner_still_launches_its_successor(tmp_path):
    p, forked, _holder, _summary = _run_stale_runner(tmp_path, supervised=False)
    out = (p.stdout or "")[-1500:]
    assert forked, f"without a supervisor the stale runner must still launch its own successor:\n{out}"
    assert p.returncode == 0, f"and stand down with exit 0, exactly as before; got {p.returncode}:\n{out}"
