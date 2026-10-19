"""Pin: a grant written through a symlinked security/acl.json lands in the real file.

On a host where every checkout's security/acl.json is a symlink to one instance ACL
(~/.aurora/<world>/security/acl.json), grant_writer used to mkstemp beside the LINK and
os.replace onto the link path. os.replace swaps the directory entry, so the link became a
regular file holding the new grant: that one checkout forked from the instance ACL, and every
other checkout and the running services kept the old grants without a word.

The writer now resolves the real path first and does the temp write and the swap beside it,
so the link survives, the real file changes, and the swap stays a same-directory rename.
"""

import json
import os

import pytest

from core.trust import grant_writer
from core.trust.capabilities import ROLE_TEMPLATES

SAMPLE = {
    "_comment": "test acl",
    "schema_version": 1,
    "grants": [
        {
            "agent_id": "claude",
            "role": "super_admin",
            "caps": sorted(c.value for c in ROLE_TEMPLATES["super_admin"]["caps"]),
            "path_scope": ["*"],
            "granted_by": "root",
            "granted_at": "2026-08-24T00:00:00Z",
            "reason": "t",
        },
        {
            "agent_id": "old_seat",
            "role": "member",
            "caps": [],
            "path_scope": [],
            "granted_by": "claude",
            "granted_at": "2026-08-24T00:00:00Z",
            "reason": "t",
        },
    ],
}


@pytest.fixture(params=["absolute", "relative"])
def linked_acl(request, tmp_path, monkeypatch):
    """One instance ACL, two checkouts linking to it; the process reads through checkout A."""
    real = tmp_path / "instance" / "security" / "acl.json"
    real.parent.mkdir(parents=True)
    real.write_text(json.dumps(SAMPLE, indent=2), encoding="utf-8")
    links = []
    for name in ("checkout_a", "checkout_b"):
        link = tmp_path / name / "security" / "acl.json"
        link.parent.mkdir(parents=True)
        target = str(real) if request.param == "absolute" else os.path.relpath(real, link.parent)
        os.symlink(target, link)
        links.append((link, target))
    monkeypatch.setenv("AKASHIC_ACL_PATH", str(links[0][0]))
    return real, links


def _grants(path):
    return {g["agent_id"]: g for g in json.loads(path.read_text(encoding="utf-8"))["grants"]}


def _litter(*dirs):
    return [n for d in dirs for n in os.listdir(d) if n.startswith(".acl-")]


def _assert_links_intact(real, links):
    for link, target in links:
        assert os.path.islink(link), f"{link} was replaced by a regular file: the ACL forked"
        assert os.readlink(link) == target
        assert os.path.samefile(link, real)


@pytest.mark.parametrize(
    "write",
    [
        lambda: grant_writer.grant("newbie", role="member", by="claude", reason="pin", hours=1),
        lambda: grant_writer.revoke("old_seat", by="claude", reason="pin"),
        lambda: grant_writer.bootstrap(by="pin"),
    ],
    ids=["grant", "revoke", "bootstrap"],
)
def test_every_write_lands_in_the_real_file_and_keeps_the_link(linked_acl, write):
    real, links = linked_acl
    before = real.read_text(encoding="utf-8")
    write()
    _assert_links_intact(real, links)
    assert real.read_text(encoding="utf-8") != before, "the real instance ACL did not change"
    # Every checkout reads the same answer, because there is still only one file.
    assert _grants(links[0][0]) == _grants(links[1][0]) == _grants(real)
    assert not _litter(real.parent, links[0][0].parent, links[1][0].parent)


def test_the_grant_itself_reaches_every_checkout(linked_acl):
    real, links = linked_acl
    grant_writer.grant("newbie", role="member", by="claude", reason="pin", hours=1)
    assert "newbie" in _grants(real)
    assert "newbie" in _grants(links[1][0]), "checkout B never saw checkout A's grant"


def test_the_swap_is_a_rename_beside_the_real_file(linked_acl, monkeypatch):
    """Atomicity is kept: the temp file sits in the REAL file's directory, so os.replace is a
    same-directory rename (beside the link it could even cross filesystems and fail)."""
    real, _links = linked_acl
    seen = []
    orig = os.replace

    def spy(src, dst):
        seen.append((os.path.dirname(os.path.realpath(src)), os.path.realpath(dst), os.fspath(dst)))
        return orig(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    grant_writer.grant("newbie", role="member", by="claude", reason="pin", hours=1)
    real_dir = os.path.realpath(real.parent)
    assert seen == [(real_dir, os.path.realpath(real), os.path.realpath(real))]


def test_a_failed_swap_still_leaves_the_link_and_the_file_untouched(linked_acl, monkeypatch):
    real, links = linked_acl
    before = real.read_bytes()

    def boom(*_a, **_kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="No space left"):
        grant_writer.grant("newbie", role="member", by="claude", reason="pin", hours=1)
    _assert_links_intact(real, links)
    assert real.read_bytes() == before
    assert not _litter(real.parent, links[0][0].parent, links[1][0].parent)


def test_a_plain_file_is_written_exactly_as_before(tmp_path, monkeypatch):
    p = tmp_path / "acl.json"
    p.write_text(json.dumps(SAMPLE, indent=2), encoding="utf-8")
    monkeypatch.setenv("AKASHIC_ACL_PATH", str(p))
    grant_writer.grant("newbie", role="member", by="claude", reason="pin", hours=1)
    assert not os.path.islink(p)
    assert "newbie" in _grants(p)
    assert not _litter(tmp_path)
