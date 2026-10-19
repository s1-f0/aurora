"""A console message to an offline seat must not spawn a second, unsupervised copy of that seat.

THE BUG (2026-10-19, adversarial review of the Ubuntu services). The Bifrost console auto-launches
the target of a message when the target is not in bus presence: ``get_launcher().launch(to)``. For
deepseek that spawns a BARE runner as a child of the UI process -- no daemon, the shift loop on,
the UI's environment, inside the UI's cgroup. On this machine the deepseek seat is owned by a
systemd user unit (aurora-heimdall), and every restart of that unit leaves deepseek briefly absent
from presence. A message sent in that window made a duplicate seat nobody supervises.

THE CONTRACT these pins hold, in order of preference:
  (a) SUPERVISED: ``AKASHIC_SUPERVISED_SEATS="deepseek=aurora-heimdall"`` declares that a systemd
      user unit owns the seat. The launcher then asks systemd for THAT unit
      (``systemctl --user start --no-block aurora-heimdall``; revive restarts it) and never spawns a
      child. The mapping is declared, never guessed from unit names, and a malformed entry refuses
      rather than falling back to a spawn.
  (b) OFF: ``AKASHIC_UI_AUTOLAUNCH=0`` makes the console launch nothing on send; it queues the
      message and says the seat is offline. It wins over (a) when both are set: it is the explicit
      kill switch.
  (c) NEITHER SET: exactly the old behaviour (Windows and other machines rely on the child spawn).

The console's feedback must be true in each mode: 'started aurora-heimdall' / 'queued; deepseek
offline' / 'auto-launched'.

No Redis writes, no real processes: subprocess.run (systemctl) and subprocess.Popen (the spawn) are
stubbed, the singleton gate and the monitor thread are neutralised, and the session file goes to a
temp dir.

Run: uv run pytest tests/test_supervised_seat_autolaunch.py -q
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import bifrost_ui as B  # noqa: E402  # scripts/ on sys.path first

from core.comm import launcher as L  # noqa: E402
from core.comm import nudge, runner_lock  # noqa: E402

SEATS_ENV = "AKASHIC_SUPERVISED_SEATS"
AUTOLAUNCH_ENV = "AKASHIC_UI_AUTOLAUNCH"

_REAL_RUN = subprocess.run
_REAL_POPEN = subprocess.Popen


class _FakeHandle:
    """What the launcher keeps for a spawned child: a live pid and no pipes to drain."""

    pid = 424242
    returncode = None
    stdin = None
    stdout = None
    stderr = None

    def poll(self):
        return None


class _Rig:
    """Records every systemctl call and every agent spawn; answers systemctl from a script."""

    def __init__(self):
        self.systemctl: list[list[str]] = []
        self.spawns: list[list[str]] = []
        self.state = "inactive"  # what `systemctl --user is-active` prints
        self.verb_rc = 0  # exit code for start / restart
        self.verb_err = ""
        self.launcher: L.Launcher | None = None  # set by the fixture

    def run(self, args, *a, **k):
        argv = list(args) if isinstance(args, (list, tuple)) else [args]
        if argv and argv[0] == "systemctl":
            self.systemctl.append(argv)
            if "is-active" in argv:
                rc = 0 if self.state == "active" else 3
                return subprocess.CompletedProcess(argv, rc, stdout=self.state + "\n", stderr="")
            return subprocess.CompletedProcess(argv, self.verb_rc, stdout="", stderr=self.verb_err)
        return _REAL_RUN(args, *a, **k)

    def popen(self, args, *a, **k):
        argv = [str(x) for x in args] if isinstance(args, (list, tuple)) else [str(args)]
        if any("bifrost_runner" in x for x in argv):
            self.spawns.append(argv)
            return _FakeHandle()
        return _REAL_POPEN(args, *a, **k)

    def verbs(self) -> list[str]:
        """The state-changing systemctl verbs issued (is-active probes left out)."""
        return [v for argv in self.systemctl for v in argv if v in ("start", "restart", "stop")]


@pytest.fixture
def rig(monkeypatch, tmp_path):
    monkeypatch.delenv(SEATS_ENV, raising=False)
    monkeypatch.delenv(AUTOLAUNCH_ENV, raising=False)
    monkeypatch.setattr(L, "SESSION_FILE", tmp_path / "bifrost-session.json")
    monkeypatch.setattr(L, "REGISTRY_PATH", tmp_path / "launcher.json")  # built-in specs only
    monkeypatch.setattr(L.Launcher, "_ensure_monitor", lambda self: None)
    monkeypatch.setattr(runner_lock, "holder", lambda aid: None)  # no live runner holds the seat
    r = _Rig()
    monkeypatch.setattr(subprocess, "run", r.run)
    monkeypatch.setattr(subprocess, "Popen", r.popen)
    launcher = r.launcher = L.Launcher()
    monkeypatch.setattr(B, "get_launcher", lambda: launcher)
    return r


# ---- the launcher ----------------------------------------------------------------------------


def test_neither_set_spawns_a_child_exactly_as_before(rig):
    res = rig.launcher.launch("deepseek")
    assert res["ok"] is True
    assert res["pid"] == _FakeHandle.pid
    assert len(rig.spawns) == 1
    assert any("bifrost_runner_deepseek.py" in x for x in rig.spawns[0])
    assert rig.systemctl == [], "with nothing declared the launcher must never talk to systemd"
    assert "supervised" not in res


def test_a_supervised_seat_starts_its_unit_and_never_spawns(rig, monkeypatch):
    monkeypatch.setenv(SEATS_ENV, "deepseek=aurora-heimdall")
    res = rig.launcher.launch("deepseek")
    assert rig.spawns == [], f"spawned a bare runner for a seat systemd owns: {rig.spawns}"
    assert ["systemctl", "--user", "start", "--no-block", "aurora-heimdall"] in rig.systemctl
    assert res["ok"] is True
    assert res["supervised"] is True
    assert res["unit"] == "aurora-heimdall"
    assert res["action"] == "started"


def test_every_tag_of_the_seat_routes_to_the_same_unit(rig, monkeypatch):
    """deepseek-build is the same seat (agent_id deepseek) with other flags: still a duplicate."""
    monkeypatch.setenv(SEATS_ENV, "deepseek=aurora-heimdall")
    res = rig.launcher.launch("deepseek-build")
    assert rig.spawns == []
    assert res["ok"] is True
    assert res["unit"] == "aurora-heimdall"
    assert "own command line" in res.get("note", ""), "the variant's flags were not applied; say so"


def test_a_unit_that_is_already_coming_up_is_not_poked(rig, monkeypatch):
    """Mid-restart the unit reads 'activating': systemd is already bringing the seat back."""
    monkeypatch.setenv(SEATS_ENV, "deepseek=aurora-heimdall")
    rig.state = "activating"
    res = rig.launcher.launch("deepseek")
    assert rig.spawns == []
    assert rig.verbs() == []
    assert res["ok"] is True
    assert res["action"] == "already_up"
    assert res["state_before"] == "activating"


def test_a_failed_start_is_reported_and_never_falls_back_to_a_spawn(rig, monkeypatch):
    monkeypatch.setenv(SEATS_ENV, "deepseek=aurora-heimdall")
    rig.verb_rc, rig.verb_err = 5, "Failed to start aurora-heimdall.service: Unit not found."
    res = rig.launcher.launch("deepseek")
    assert rig.spawns == []
    assert res["ok"] is False
    assert res["supervised"] is True
    assert "Unit not found" in res["error"]


@pytest.mark.parametrize("decl", ["deepseek=", "deepseek", "deepseek=--now", "deepseek=a b"])
def test_a_malformed_declaration_refuses_rather_than_spawning(rig, monkeypatch, decl):
    """A typo in the declaration must not quietly reopen the duplicate it exists to stop."""
    monkeypatch.setenv(SEATS_ENV, decl)
    res = rig.launcher.launch("deepseek")
    assert rig.spawns == []
    assert rig.systemctl == []
    assert res["ok"] is False
    assert SEATS_ENV in res["error"]


def test_an_undeclared_seat_still_spawns(rig, monkeypatch):
    monkeypatch.setenv(SEATS_ENV, "deepseek=aurora-heimdall")
    res = rig.launcher.launch("gemini")
    assert res["ok"] is True
    assert len(rig.spawns) == 1
    assert rig.systemctl == []


def test_several_seats_parse_with_spaces_and_case(rig, monkeypatch):
    monkeypatch.setenv(SEATS_ENV, " Gemini = aurora-gemini ; deepseek=aurora-heimdall.service ")
    assert rig.launcher.launch("gemini")["unit"] == "aurora-gemini"
    assert rig.launcher.launch("deepseek")["unit"] == "aurora-heimdall.service"
    assert rig.spawns == []


def test_revive_restarts_the_unit(rig, monkeypatch):
    monkeypatch.setenv(SEATS_ENV, "deepseek=aurora-heimdall")
    rig.state = "active"
    res = rig.launcher.revive("deepseek")
    assert rig.spawns == []
    assert ["systemctl", "--user", "restart", "--no-block", "aurora-heimdall"] in rig.systemctl
    assert res["ok"] is True
    assert res["action"] == "restarted"
    assert res["revived"] is True


# ---- the console -----------------------------------------------------------------------------


class _FakeBus:
    def __init__(self, online=()):
        self.online = list(online)
        self.sent: list[tuple] = []

    def presence(self):
        return [{"agent": a} for a in self.online]

    def send(self, to, kind, text, meta=None):
        self.sent.append((to, kind, text))
        return "1-0"

    def broadcast(self, kind, text, meta=None):
        self.sent.append(("*", kind, text))
        return "1-0"


def _console_send(monkeypatch, *, to="deepseek", fidelity="chat", online=()):
    """Drive Handler._send without a socket: a fake bus, nudges recorded, the JSON captured."""
    bus = _FakeBus(online)
    steered: list[tuple] = []
    monkeypatch.setattr(nudge, "steer_push", lambda agent, frm, text: steered.append((agent, text)) or True)
    monkeypatch.setattr(nudge, "nudge", lambda agent, by="user", reason="": True)
    h = B.Handler.__new__(B.Handler)
    out: list[dict] = []
    h._send_bus = lambda ns: bus
    h._json = lambda obj, code=200: out.append(obj) or obj
    h._send({"text": "are you there?", "to": to, "fidelity": fidelity})
    return out[0], bus


def test_console_default_mode_still_auto_launches(rig, monkeypatch):
    res, bus = _console_send(monkeypatch)
    assert len(rig.spawns) == 1
    assert rig.systemctl == []
    assert res["launched"] == ["deepseek"]
    assert set(res) == {"ok", "id", "intent", "to", "paused", "launched"}, "default reply shape unchanged"
    assert bus.sent == [("deepseek", "chat", "are you there?")]


def test_console_supervised_mode_starts_the_unit_and_says_so(rig, monkeypatch):
    monkeypatch.setenv(SEATS_ENV, "deepseek=aurora-heimdall")
    res, bus = _console_send(monkeypatch)
    assert rig.spawns == [], "the console spawned a duplicate deepseek inside its own cgroup"
    assert rig.verbs() == ["start"]
    assert res["seat"] == "started aurora-heimdall"
    assert res["started"] == ["aurora-heimdall"]
    assert "launched" not in res, "nothing was auto-launched; the toast must not claim it"
    assert bus.sent == [("deepseek", "chat", "are you there?")]


def test_console_supervised_mode_unit_already_up_says_queued(rig, monkeypatch):
    monkeypatch.setenv(SEATS_ENV, "deepseek=aurora-heimdall")
    rig.state = "activating"
    res, _ = _console_send(monkeypatch)
    assert rig.spawns == []
    assert rig.verbs() == []
    assert res["seat"] == "queued; deepseek offline (aurora-heimdall activating)"
    assert res["queued_offline"] == ["deepseek"]
    assert "started" not in res


def test_console_supervised_mode_failed_start_says_queued_and_why(rig, monkeypatch):
    monkeypatch.setenv(SEATS_ENV, "deepseek=aurora-heimdall")
    rig.verb_rc, rig.verb_err = 1, "Failed to connect to bus"
    res, _ = _console_send(monkeypatch)
    assert rig.spawns == []
    assert res["seat"].startswith("queued; deepseek offline (could not start aurora-heimdall:")
    assert "Failed to connect to bus" in res["seat"]
    assert res["queued_offline"] == ["deepseek"]


def test_console_off_switch_queues_and_launches_nothing(rig, monkeypatch):
    monkeypatch.setenv(AUTOLAUNCH_ENV, "0")
    calls: list[str] = []
    monkeypatch.setattr(rig.launcher, "launch", lambda tag, **k: calls.append(tag) or {"ok": True})
    res, bus = _console_send(monkeypatch)
    assert calls == [], "AKASHIC_UI_AUTOLAUNCH=0 but the console still called the launcher"
    assert rig.spawns == []
    assert rig.systemctl == []
    assert res["seat"] == "queued; deepseek offline"
    assert res["queued_offline"] == ["deepseek"]
    assert "launched" not in res
    assert bus.sent == [("deepseek", "chat", "are you there?")], "the message itself is still queued"


def test_console_off_switch_wins_over_a_supervised_declaration(rig, monkeypatch):
    monkeypatch.setenv(AUTOLAUNCH_ENV, "off")
    monkeypatch.setenv(SEATS_ENV, "deepseek=aurora-heimdall")
    res, _ = _console_send(monkeypatch)
    assert rig.spawns == []
    assert rig.systemctl == []
    assert res["seat"] == "queued; deepseek offline"


@pytest.mark.parametrize("env", [{}, {SEATS_ENV: "deepseek=aurora-heimdall"}, {AUTOLAUNCH_ENV: "0"}])
def test_console_never_launches_an_online_target(rig, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    res, _ = _console_send(monkeypatch, online=["deepseek"])
    assert rig.spawns == []
    assert rig.systemctl == []
    assert "seat" not in res
    assert "launched" not in res


def test_console_steer_feedback_is_true_in_each_mode(rig, monkeypatch):
    monkeypatch.setenv(SEATS_ENV, "deepseek=aurora-heimdall")
    res, _ = _console_send(monkeypatch, fidelity="steer")
    assert res["msg"] == "started aurora-heimdall — deepseek folds the steer in when it starts"
    monkeypatch.setenv(AUTOLAUNCH_ENV, "0")
    res, _ = _console_send(monkeypatch, fidelity="steer")
    assert res["msg"] == "queued; deepseek offline — deepseek folds the steer in when it starts"
    assert rig.spawns == []


def test_the_console_toast_shows_the_seat_note():
    """The server's truthful note is useless if the page drops it: send() must print j.seat."""
    with open(os.path.join(ROOT, "scripts", "bifrost_ui.py"), encoding="utf-8") as fh:
        src = fh.read()
    body = src[src.index("async function send(){") :]
    body = body[: body.index("\n}\n")]
    assert "j.seat" in body, "send() never reads the seat note the server returns"


@pytest.mark.parametrize("fn", ["async function launchAgent(tag){", "async function reviveAgent(tag){"])
def test_the_launch_and_revive_buttons_name_the_unit_not_a_pid(fn):
    """A supervised launch has no pid of ours: 'started (pid undefined)' would be a false toast."""
    with open(os.path.join(ROOT, "scripts", "bifrost_ui.py"), encoding="utf-8") as fh:
        src = fh.read()
    body = src[src.index(fn) :]
    body = body[: body.index("\n}\n")]
    assert "j.supervised" in body, f"{fn} ignores a supervised reply and toasts a pid"
    assert "j.unit" in body
    assert "j.note" in body, f"{fn} hides what the unit did not apply (deepseek-build's flags, a prompt)"


def test_both_switches_are_on_the_physics_sheet():
    """An operator finds a kill switch by reading docs/PHYSICS.md; a name read through a constant
    is invisible to the scanner that writes it."""
    from scripts.generators.gen_physics_sheet import scan

    flags, _ = scan([os.path.join(ROOT, "core", "comm", "launcher.py"), os.path.join(ROOT, "scripts", "bifrost_ui.py")])
    assert SEATS_ENV in flags
    assert AUTOLAUNCH_ENV in flags
