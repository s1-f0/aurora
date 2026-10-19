"""PIN: recall-at's path query describes the FILE, not where the checkout happens to live.

MEASURED 2026-10-19 on a Linux box whose checkout is /mnt/lnx-storage/projects/akashic-labs/aurora
(worktrees under .claude/worktrees/<name>): _query_from tokenised the whole absolute path, so every
edit queried 'storage projects akashic labs aurora claude worktrees ...' before the file's own
words. Those location tokens matched verb help texts (`mailbox` on 'akashic storage', `wish` on
'ergonomics') and lesson prose, so the same off-topic hints rode along with every Edit and Write.
On the Windows box the prefix is E:/AI-Setup, which contributes almost nothing -- which is why the
house never saw it.

The rule: inside a checkout (main tree or worktree) the query is built from the repo-relative path;
outside any checkout only the file's parent folder and stem count, because the rest is machine
layout (a home directory, a scratchpad's encoded session path).

Run: uv run pytest tests/test_recall_query_ignores_checkout_location.py -q -p no:cacheprovider
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.recall.at_action import _query_from  # noqa: E402


def _checkout(base) -> str:
    (base / "core").mkdir(parents=True)
    (base / "agent_cli.py").write_text("", encoding="utf-8")
    (base / "tests").mkdir()
    return str(base)


def test_inside_a_checkout_the_location_does_not_count(tmp_path):
    deep = tmp_path / "mnt" / "lnx-storage" / "projects" / "akashic-labs" / "aurora"
    repo = _checkout(deep)
    absolute = os.path.join(repo, "tests", "test_seat_heartbeat_wiring.py")
    assert _query_from(absolute, None) == _query_from("tests/test_seat_heartbeat_wiring.py", None)
    for noise in ("storage", "projects", "akashic", "labs", "aurora"):
        assert noise not in _query_from(absolute, None).split()


def test_a_worktree_is_a_checkout_too(tmp_path):
    main = _checkout(tmp_path / "aurora")
    wt = _checkout(tmp_path / "aurora" / ".claude" / "worktrees" / "ergonomics-hooks")
    absolute = os.path.join(wt, "core", "recall", "at_action.py")
    q = _query_from(absolute, None).split()
    for noise in ("worktrees", "ergonomics", "claude"):
        assert noise not in q  # the enclosing main checkout must not win over the nearer worktree
    assert q == ["recall", "action"]
    assert main


def test_outside_any_checkout_only_the_parent_and_stem_count(tmp_path):
    p = tmp_path / "claude-1000" / "-mnt-lnx-storage-projects-akashic-labs-aurora" / "abc" / "scratchpad"
    p.mkdir(parents=True)
    q = _query_from(str(p / "record_debt.py"), None).split()
    assert q == ["scratchpad", "record", "debt"], q


def test_relative_paths_and_commands_are_unchanged():
    assert _query_from("core/recall/at_action.py", None) == "recall action"
    assert _query_from(None, "uv run pytest -q tests/test_x.py") == "pytest"
