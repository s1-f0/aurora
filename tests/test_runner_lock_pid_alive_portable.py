"""Pin: the consumer-seat liveness probe works off Windows.

runner_lock._pid_alive_default used to shell out to `tasklist` on every OS. Off Windows that
call always raises, and the probe fails toward alive, so a crashed session's dead wake listener
read as alive and free_if_dead() left its seat to the 30-minute TTL instead of freeing it. Off
Windows it now asks the kernel (signal 0); Windows keeps tasklist exactly as it was.

The shape mirrors tests/test_stop_pid_alive_portable.py (the same fix in the stop hook).
"""

import os
import subprocess
import sys
import time
import uuid

import pytest

from core.comm import runner_lock, wake_seat

posix_only = pytest.mark.skipif(os.name == "nt", reason="the signal-0 probe is the non-Windows branch")


@pytest.fixture
def no_tasklist(monkeypatch):
    """Record any shell-out. The old probe swallowed every exception, so a raising stub alone
    would be caught and read as alive; the recorded calls are what the tests assert on."""
    calls = []

    def refuse(*a, **kw):
        calls.append(a)
        raise AssertionError("the POSIX probe must not shell out")

    monkeypatch.setattr(subprocess, "run", refuse)
    return calls


def _reaped_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()  # exited and reaped: the pid names no process now
    return p.pid


@posix_only
def test_a_live_process_reads_alive(no_tasklist):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert runner_lock._pid_alive_default(p.pid) is True
    finally:
        p.kill()
        p.wait()
    assert no_tasklist == []


@posix_only
def test_a_dead_process_reads_dead(no_tasklist):
    assert runner_lock._pid_alive_default(_reaped_pid()) is False
    assert no_tasklist == []


@posix_only
def test_another_users_process_reads_alive(no_tasklist, monkeypatch):
    def denied(_pid, _sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(runner_lock.os, "kill", denied)
    assert runner_lock._pid_alive_default(4242) is True
    assert no_tasklist == []


@posix_only
@pytest.mark.parametrize("pid", [0, -1])
def test_a_process_group_pid_is_never_a_listener(no_tasklist, pid):
    assert runner_lock._pid_alive_default(pid) is False
    assert no_tasklist == []


@posix_only
def test_an_unexpected_probe_error_fails_toward_alive(no_tasklist, monkeypatch):
    def odd(_pid, _sig):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(runner_lock.os, "kill", odd)
    assert runner_lock._pid_alive_default(4242) is True  # a probe error never justifies a free
    assert no_tasklist == []


class _Out:
    def __init__(self, stdout):
        self.stdout = stdout


@pytest.mark.parametrize(
    ("stdout", "raises", "expected"),
    [
        ("python.exe                    4242 Console    1     10,000 K\n", False, True),
        ("INFO: No tasks are running which match the specified criteria.\n", False, False),
        (None, True, True),
    ],
)
def test_windows_keeps_the_tasklist_probe_unchanged(monkeypatch, stdout, raises, expected):
    """The Windows branch is byte-identical: same argv, same kwargs, same verdicts."""
    seen = []

    def fake_run(argv, **kw):
        seen.append((argv, kw))
        if raises:
            raise subprocess.TimeoutExpired(argv, 5)
        return _Out(stdout)

    with monkeypatch.context() as m:
        m.setattr(subprocess, "run", fake_run)
        m.setattr(runner_lock.os, "name", "nt")
        got = runner_lock._pid_alive_default(4242)
    assert got is expected
    assert seen == [(["tasklist", "/FI", "PID eq 4242", "/NH"], {"capture_output": True, "text": True, "timeout": 5})]


# ---------------------------------------------------------------- end to end, default probe


try:
    from core.comm.bus import Bus

    _ONLINE = bool(Bus("pidalive-probe").online)
except Exception:
    _ONLINE = False


@pytest.fixture
def agent():
    aid = f"pidalive-{uuid.uuid4().hex[:8]}"
    yield aid
    c = runner_lock._client()
    if c is not None:
        try:
            c.delete(runner_lock._key(aid))
            c.delete(runner_lock._gen_prefix() + aid)
        except Exception:
            pass


def _claim_aged(agent, sid):
    ok, _gen, _ = runner_lock.claim_consumer(agent, f"session:{sid}")
    assert ok
    rec = runner_lock.holder(agent)
    assert rec is not None
    return runner_lock._ts_epoch(rec["ts"], default=time.time()) + 400  # past the 300s grace


@posix_only
@pytest.mark.skipif(not _ONLINE, reason="live-Redis pin; bus offline")
def test_free_if_dead_frees_a_seat_whose_listener_died(agent, tmp_path):
    """The behaviour the probe exists for: no injected probe, a listener pid that is gone."""
    now = _claim_aged(agent, "deadlistener")
    with open(wake_seat.seat_path(agent, "deadlistener", str(tmp_path)), "w") as fh:
        fh.write(str(_reaped_pid()))
    v = runner_lock.free_if_dead(agent, now=now, tmp=str(tmp_path))
    assert v["freed"], v
    assert "listener-pid-dead" in v["reason"]
    assert runner_lock.holder(agent) is None


@posix_only
@pytest.mark.skipif(not _ONLINE, reason="live-Redis pin; bus offline")
def test_free_if_dead_keeps_a_seat_whose_listener_lives(agent, tmp_path):
    now = _claim_aged(agent, "livelistener")
    with open(wake_seat.seat_path(agent, "livelistener", str(tmp_path)), "w") as fh:
        fh.write(str(os.getpid()))
    v = runner_lock.free_if_dead(agent, now=now, tmp=str(tmp_path))
    assert not v["freed"], v
    assert v["reason"].startswith("listener-alive")
