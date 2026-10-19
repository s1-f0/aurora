"""PIN: the vault door opens a window on a Linux desktop that has no tkinter.

MEASURED 2026-10-19 on Ubuntu 24.04: the interpreter is the system Python 3.12 without python3-tk, so
`secret <target>` failed with NO_WINDOW and hinted `Get-Clipboard | ...`, a PowerShell command.
GNOME ships zenity (KDE ships kdialog), whose password dialog returns the paste on stdout to this
process only -- the same clipboard -> window -> file path the tkinter window gives, never a
transcript. When no dialog tool exists either, the hint must be one this OS can run.

Every case uses a throwaway vault (AKASHIC_SECRETS_DIR); no real window is opened.

Run: uv run pytest tests/test_secret_window_without_tkinter.py -q -p no:cacheprovider
"""

import argparse
import subprocess
import sys

import pytest

import agent_cli

VALUE = "pin-value-not-a-key-0000"


@pytest.fixture
def no_tk(tmp_path, monkeypatch):
    monkeypatch.setenv("AKASHIC_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("AURORA_OUTPUT", "agent")
    monkeypatch.setitem(sys.modules, "tkinter", None)  # `import tkinter` now raises ImportError
    return tmp_path


def _secret(capsys, target):
    code = agent_cli.cmd_secret(argparse.Namespace(target=target, stdin=False, fields=None))
    cap = capsys.readouterr()
    return code, cap.out + cap.err


def _dialog(monkeypatch, tool, returncode, stdout):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}" if name == tool else None)
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr("subprocess.run", run)
    return calls


@pytest.mark.parametrize("tool", ["zenity", "kdialog"])
def test_a_desktop_dialog_captures_the_paste(no_tk, capsys, monkeypatch, tool):
    calls = _dialog(monkeypatch, tool, 0, VALUE + "\n")
    code, text = _secret(capsys, "deepseek.key")
    assert code == 0, text
    assert calls
    assert calls[0][0].endswith(tool)
    assert (no_tk / "deepseek.key").read_text(encoding="utf-8") == VALUE
    assert "saved: deepseek.key" in text
    assert VALUE not in text  # the receipt counts bytes, never shows them
    assert all(VALUE not in " ".join(c) for c in calls)  # nor does the dialog's command line


def test_closing_the_dialog_writes_nothing(no_tk, capsys, monkeypatch):
    _dialog(monkeypatch, "zenity", 1, "")
    code, text = _secret(capsys, "deepseek.key")
    assert "code: CANCELLED" in text
    assert not (no_tk / "deepseek.key").exists()
    assert code != 0 or "CANCELLED" in text


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX hint is what is under test")
def test_no_window_at_all_hints_a_command_this_os_has(no_tk, capsys, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    code, text = _secret(capsys, "deepseek.key")
    assert code == 2
    assert "code: NO_WINDOW" in text
    assert "Get-Clipboard" not in text
    assert "--stdin" in text
