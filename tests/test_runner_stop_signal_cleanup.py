"""A stopped runner cleans up after itself: SIGTERM runs the exit path, it does not skip it.

THE BUG (adversarial review, verified on the Ubuntu host's systemd units): the DeepSeek runner
installed no SIGTERM handler. systemd (KillMode=control-group) and ManagedChild.terminate() both
stop it with SIGTERM, the default action killed it at once, and its `finally` never ran: no
retire_seat, no runner_lock.release, no exit summary. Every restart therefore left

  * a dead holder on the runner lock -- the next daemon idled ~20 s on "a foreign runner pid=...
    holds the runner lock" until the key's TTL lapsed, and
  * a 'running' roster card for a process that no longer existed -- the doctor paged HARD WEDGE.

THE FIX, pinned here:
  P1  the real one-shot handler (core.comm.runner_lib.install_stop_signals) turns the first
      SIGTERM into a KeyboardInterrupt, so the runner's existing cleanup runs, and a second
      SIGTERM cannot interrupt that cleanup;
  P2  the real DeepSeek runner, stopped with SIGTERM, releases its lock, retracts its roster card
      and writes its exit summary;
  P3  the handler goes in right after the lock is won, and the try/finally that releases it
      covers onboarding too (a SIGTERM mid-boot used to leak the lock the same way);
  P4  ManagedChild.terminate() waits longer than retire_seat's heartbeat join before SIGKILL,
      or the daemon would cut the very cleanup this file exists to protect.

POSIX only: Windows stops a child with TerminateProcess, which no handler can catch, and its
behaviour is deliberately unchanged.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import json
import os
import queue
import signal
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "scripts" / "bifrost_runner_deepseek.py"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="SIGTERM delivery is a POSIX contract")


def _reader(proc: subprocess.Popen) -> queue.Queue:
    """Pump the child's stdout into a queue so the test can wait on a line without blocking."""
    lines: queue.Queue = queue.Queue()
    stream = proc.stdout
    assert stream is not None, "spawn the child with stdout=PIPE"

    def _pump():
        for line in stream:
            lines.put(line.rstrip("\n"))
        lines.put(None)

    threading.Thread(target=_pump, daemon=True).start()
    return lines


def _wait_for(lines: queue.Queue, needle: str, seen: list[str], timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            line = lines.get(timeout=max(0.05, deadline - time.time()))
        except queue.Empty:
            return False
        if line is None:
            return False
        seen.append(line)
        if needle in line:
            return True
    return False


def _drain(lines: queue.Queue, seen: list[str]) -> None:
    while True:
        try:
            line = lines.get(timeout=2)
        except queue.Empty:
            return
        if line is None:
            return
        seen.append(line)


# ------------------------------------------------------------------------------------- P1
_MINIMAL_RUNNER = textwrap.dedent(
    """
    import sys, threading, time
    sys.path.insert(0, sys.argv[1])
    marker = sys.argv[2]
    from core.comm.runner_lib import ignore_stop_signals, install_stop_signals, stop_exit_code

    def log(text):
        with open(marker, "a", encoding="utf-8") as f:
            f.write(text + "\\n")

    assert install_stop_signals() is True
    stop_hb = threading.Event()
    beat = threading.Thread(target=lambda: stop_hb.wait(60), daemon=True)
    beat.start()
    try:
        print("ready", flush=True)
        while True:
            time.sleep(0.05)  # the runner's loop: a blocking wait the signal must break
    except (KeyboardInterrupt, EOFError):
        log("interrupted")
    finally:
        ignore_stop_signals()
        log("cleanup-start")
        print("cleanup-start", flush=True)
        time.sleep(1.0)  # a slow cleanup: the second SIGTERM lands in here
        stop_hb.set()
        beat.join(5)
        log("cleanup-done")
    sys.exit(stop_exit_code(0))
    """
)


def test_p1_the_real_handler_turns_sigterm_into_cleanup_a_second_cannot_interrupt(tmp_path):
    script = tmp_path / "minimal_runner.py"
    script.write_text(_MINIMAL_RUNNER, encoding="utf-8")
    marker = tmp_path / "cleanup.log"
    proc = subprocess.Popen(
        [sys.executable, str(script), str(REPO), str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(tmp_path),
    )
    seen: list[str] = []
    try:
        lines = _reader(proc)
        assert _wait_for(lines, "ready", seen, timeout=30), f"minimal runner never got ready: {seen}"
        proc.send_signal(signal.SIGTERM)
        assert _wait_for(lines, "cleanup-start", seen, timeout=10), f"SIGTERM did not reach the cleanup: {seen}"
        proc.send_signal(signal.SIGTERM)  # the second stop: systemd plus ManagedChild, or an impatient human
        proc.wait(timeout=15)
        _drain(lines, seen)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    log = marker.read_text(encoding="utf-8").split() if marker.exists() else []
    assert log == ["interrupted", "cleanup-start", "cleanup-done"], (
        f"SIGTERM must run the cleanup to completion and the second SIGTERM must not cut it short; "
        f"cleanup log={log}, returncode={proc.returncode}, output={seen}"
    )
    assert proc.returncode == 128 + signal.SIGTERM, (
        f"a SIGTERM stop exits 128+SIGTERM, so a supervisor still reads it as killed, not as a "
        f"deliberate exit 0 (ManagedChild never respawns an exit 0); got {proc.returncode}"
    )


def test_p1b_sigint_keeps_the_old_clean_exit_code():
    """Ctrl-C was always a clean exit 0 (KeyboardInterrupt -> finally -> return 0). The handler
    must not turn it into a crash code."""
    from core.comm import runner_lib

    saved = dict(runner_lib._STOP_STATE)
    try:
        runner_lib._STOP_STATE["signum"] = int(signal.SIGINT)
        assert runner_lib.stop_exit_code(0) == 0
        runner_lib._STOP_STATE["signum"] = int(signal.SIGTERM)
        assert runner_lib.stop_exit_code(0) == 128 + signal.SIGTERM
        runner_lib._STOP_STATE["signum"] = None
        assert runner_lib.stop_exit_code(4) == 4
    finally:
        runner_lib._STOP_STATE.clear()
        runner_lib._STOP_STATE.update(saved)


# ------------------------------------------------------------------------------------- P2
def _bus_or_skip(agent):
    from core.comm.bus import Bus

    b = Bus(agent)
    if not b.online:
        pytest.skip("redis not available")
    return b


def _cleanup_keys(b, agent, sid8):
    with contextlib.suppress(Exception):
        b._client.delete(
            b._cursor_key(),
            f"{b.ns}:generation:{agent}",
            f"{b.ns}:runner:{agent}",
            f"{b.ns}:worklive:{agent}#{sid8}",
            f"{b.ns}:seatseen:{agent}#{sid8}",
            b._inbox_key(agent),
        )


def test_p2_the_real_deepseek_runner_cleans_up_on_sigterm(tmp_path):
    from core.comm import runner_lock
    from core.comm.bus import sid8 as _sid8

    agent = f"sigterm-{uuid.uuid4().hex[:8]}"
    session = f"sig{uuid.uuid4().hex}"
    sid8 = _sid8(session)
    b = _bus_or_skip(agent)
    summary = tmp_path / "summary.json"
    env = dict(os.environ, AKASHIC_DRILL_ECHO="1", DEEPSEEK_API_KEY="drill-offline-placeholder")
    for k in ("AKASHIC_KILLPOINT", "AKASHIC_EXTERNAL_SUPERVISOR", "BIFROST_INCARNATION"):
        env.pop(k, None)
    proc = subprocess.Popen(
        [sys.executable, str(RUNNER), "--agent", agent, "--session", session, "--summary-file", str(summary)],
        env=env,
        cwd=str(REPO),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    seen: list[str] = []
    card_key = f"{b.ns}:worklive:{agent}#{sid8}"
    seen_key = f"{b.ns}:seatseen:{agent}#{sid8}"
    try:
        lines = _reader(proc)
        assert _wait_for(lines, "Waiting for messages", seen, timeout=90), f"runner never came online: {seen[-20:]}"
        # Wait for the first roster beat, so there IS a 'running' card for the exit to retract.
        deadline = time.time() + 20
        while time.time() < deadline and not b._client.get(card_key):
            time.sleep(0.2)
        assert b._client.get(card_key), "the runner never beat its roster card"
        assert runner_lock.holder(agent), "the runner never took its lock"

        proc.send_signal(signal.SIGTERM)  # what systemd and ManagedChild.terminate() send
        proc.wait(timeout=30)
        _drain(lines, seen)

        holder = runner_lock.holder(agent)
        card = b._client.get(card_key)
        witness = json.loads(b._client.get(seen_key) or "{}")
        assert holder is None, (
            f"SIGTERM left a dead holder on the runner lock ({holder}); the next daemon idles on "
            f"'a foreign runner holds the runner lock' until the TTL lapses. rc={proc.returncode}"
        )
        assert card is None, f"SIGTERM left the 'running' roster card behind (doctor pages HARD WEDGE): {card}"
        assert witness.get("phase") == "offline", f"the seat must be declared offline on a stop: {witness}"
        assert summary.exists(), f"SIGTERM skipped the exit summary; output tail={seen[-10:]}"
        assert json.loads(summary.read_text(encoding="utf-8")).get("exit_code") == 128 + signal.SIGTERM
        assert any("[deepseek-runner] stopped." in line for line in seen), seen[-10:]
        assert proc.returncode == 128 + signal.SIGTERM, proc.returncode
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        _cleanup_keys(b, agent, sid8)


# ------------------------------------------------------------------------------------- P3
def _main_fn(tree: ast.Module) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    raise AssertionError("bifrost_runner_deepseek.py has no main()")


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            fname = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if fname == name:
                out.append(n)
    return out


def test_p3_handler_follows_the_lock_and_the_finally_covers_onboarding():
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    main = _main_fn(tree)
    acquire = _calls(main, "acquire_waiting")
    install = _calls(main, "install_stop_signals")
    onboarding = _calls(main, "onboarding_context")
    assert acquire, "the runner no longer acquires its lock with acquire_waiting -- re-derive this pin"
    assert install, "the DeepSeek runner never installs the one-shot stop handler (SIGTERM skips its cleanup)"
    assert onboarding, "onboarding_context is gone -- re-derive this pin"

    releasing = [
        t for t in ast.walk(main) if isinstance(t, ast.Try) and any(_calls(stmt, "release") for stmt in t.finalbody)
    ]
    assert len(releasing) == 1, f"expected ONE try whose finally releases the runner lock, found {len(releasing)}"
    tenure = releasing[0]

    acquire_line = acquire[0].lineno
    install_line = install[0].lineno
    assert acquire_line < install_line < tenure.lineno, (
        f"the handler must be installed AFTER the lock is won (line {acquire_line}) and BEFORE the "
        f"tenure's try opens (line {tenure.lineno}); it is at line {install_line}"
    )
    # Nothing that can block or fail may sit between the handler and the try: only bindings.
    body = main.body
    i_install = next(i for i, s in enumerate(body) if install_line in range(s.lineno, (s.end_lineno or 0) + 1))
    i_try = body.index(tenure)
    between = body[i_install + 1 : i_try]
    assert all(isinstance(s, (ast.Assign, ast.AnnAssign)) for s in between), (
        f"only plain bindings may sit between install_stop_signals() and the try; found "
        f"{[type(s).__name__ for s in between]} -- a SIGTERM there would skip the cleanup"
    )
    first, last = tenure.body[0].lineno, tenure.body[-1].end_lineno or 0
    for call in onboarding:
        assert first <= call.lineno <= last, (
            f"onboarding (line {call.lineno}) runs OUTSIDE the try that releases the lock "
            f"(lines {first}-{last}); a SIGTERM during onboarding leaks the lock"
        )
    assert any(_calls(stmt, "ignore_stop_signals") for stmt in tenure.finalbody[:1]), (
        "the cleanup must disarm the handler first, so a stop signal arriving DURING the cleanup cannot raise into it"
    )


# ------------------------------------------------------------------------------------- P4
_SLOW_STOPPER = textwrap.dedent(
    """
    import signal, sys, time
    marker, cleanup_s = sys.argv[1], float(sys.argv[2])

    def _stop(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(cleanup_s)  # retire_seat joining a heartbeat thread for its full budget
        with open(marker, "w", encoding="utf-8") as f:
            f.write("cleanup-done")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _stop)
    print("ready", flush=True)
    while True:
        time.sleep(0.1)
    """
)


def test_p4_managed_child_terminate_outwaits_retire_seats_heartbeat_join(tmp_path):
    from core.comm.runner_lib import retire_seat
    from scripts.bifrost_child import ManagedChild

    join_s = float(inspect.signature(retire_seat).parameters["hb_join_s"].default)
    script = tmp_path / "slow_stopper.py"
    script.write_text(_SLOW_STOPPER, encoding="utf-8")
    marker = tmp_path / "done.txt"
    mc = ManagedChild([sys.executable, str(script), str(marker), str(join_s + 0.5)], cwd=str(tmp_path))
    assert mc.spawn() is not None
    deadline = time.time() + 30
    while time.time() < deadline and "ready" not in list(mc._ring):
        time.sleep(0.05)
    assert "ready" in list(mc._ring), f"slow stopper never got ready: {list(mc._ring)}"
    started = time.time()
    mc.terminate()
    took = time.time() - started
    assert marker.exists(), (
        f"ManagedChild.terminate() SIGKILLed the child after {took:.1f}s, before a cleanup that "
        f"joins the heartbeat for retire_seat's {join_s}s budget could finish"
    )
