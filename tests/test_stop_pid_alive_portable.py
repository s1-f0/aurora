"""Pin: the stop hook's listener-liveness probe works off Windows.

claude_stop._pid_alive used to shell out to `tasklist` everywhere. On Linux that call always
raises, and the probe fails open, so a dead wake listener read as alive and the session as
armed. Off Windows it now asks the kernel (signal 0); Windows keeps tasklist.
"""

import os
import subprocess
import sys

import pytest

from agent.harness.hooks import claude_stop

posix_only = pytest.mark.skipif(os.name == "nt", reason="the signal-0 probe is the non-Windows branch")


@pytest.fixture
def no_tasklist(monkeypatch):
    def refuse(*_a, **_kw):
        raise AssertionError("the POSIX probe must not shell out")

    monkeypatch.setattr(claude_stop.subprocess, "run", refuse)


@posix_only
def test_a_live_process_reads_alive(no_tasklist):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert claude_stop._pid_alive(p.pid) is True
    finally:
        p.kill()
        p.wait()


@posix_only
def test_a_dead_process_reads_dead(no_tasklist):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()  # exited and reaped: the pid names no process now
    assert claude_stop._pid_alive(p.pid) is False


@posix_only
def test_another_users_process_reads_alive(no_tasklist, monkeypatch):
    def denied(_pid, _sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(claude_stop.os, "kill", denied)
    assert claude_stop._pid_alive(4242) is True


@posix_only
@pytest.mark.parametrize("pid", [0, -1])
def test_a_process_group_pid_is_never_a_listener(no_tasklist, pid):
    assert claude_stop._pid_alive(pid) is False
