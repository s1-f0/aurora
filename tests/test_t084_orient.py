"""T084 VR lineage RED pins: ``orient`` is a scene, not concatenated prose.

The 2026-07-28 VR build order specified a deterministic GPS view model with an
epistemic floor, bounded sharpness, landmarks, and a return tether.  The
2026-07-30 inhabitant spec added equippable verbs.  These pins join that older
contract to the native sweep/ground/capture substrate without inventing a
second renderer-specific control plane.
"""

from __future__ import annotations

import argparse
import asyncio
import json

import pytest

from core.coord.observations import Observation, Snapshot

SUBJECT = "synthetic-sol-t084-orient"
AXES = {"authority", "claim_kind", "currency", "identity_state", "risk"}


def _snapshot(subject: str) -> Snapshot:
    assert subject == SUBJECT
    return Snapshot(
        kind="awareness",
        subject=subject,
        observations=tuple(
            Observation(
                name=name,
                subject=subject,
                status="OK",
                summary=f"{name} visible",
                source=(f"fixture:{name}",),
                details={"rank": rank},
                drill=f"drill {name}",
            )
            for rank, name in enumerate(("bus", "bench", "route", "moved"), 1)
        ),
    )


def _ground(target: str, *, subject: str, continuity: bool = False):
    assert subject == SUBJECT
    return {
        "schema": "ground.result.v1",
        "target": {"kind": target.split(":", 1)[0], "name": target.split(":", 1)[1]},
        "subject": subject,
        "observed_at": "2026-08-28T00:00:00Z",
        "rungs": [
            {
                "name": "reachable",
                "state": "observed",
                "claim": "fixture endpoint is present",
                "source": "fixture:door-census",
                "drill": "fixture ground drill",
                "details": {},
            }
        ],
        "bounds": {"sources_total": 1, "sources_failed": 0},
        "blind": ["fresh runtime proof is outside this fixture"],
        "effects": [],
        "mode": "continuity" if continuity else "ground",
    }


def _capture(subject: str, thread: str, *, per_stream: int = 1000):
    assert subject == SUBJECT
    return {
        "schema": "capture.thread.v1",
        "subject": subject,
        "thread_ref": thread,
        "found": True,
        "messages": [{"id": "1000-0", "content": "verbatim fixture"}],
        "bounds": {"per_stream": per_stream, "truncated": False},
        "blind": [],
        "effects": [],
    }


def _providers():
    return {"snapshot": _snapshot, "ground": _ground, "capture": _capture}


def test_orient_composes_a_subject_bound_scene_with_truth_floor_and_return_tether():
    from core.coord.orient import build_orientation

    scene = build_orientation(
        SUBJECT,
        "verb:capture",
        density="compact",
        depth="surface",
        providers=_providers(),
    )

    assert scene["schema"] == "orient.scene.v1"
    assert scene["subject"] == SUBJECT
    assert scene["target"] == {
        "kind": "verb",
        "name": "capture",
        "address": "verb:capture",
        "interpretation": "typed",
    }
    assert scene["position"]["schema_version"] == "observation.snapshot.v1"
    assert scene["position"]["landmarks_total"] == 4
    assert "observations" not in scene["position"]
    assert scene["effects"] == []

    # Compact density moves landmarks to the periphery; it never silently drops them.
    assert len(scene["nearby"]) == 1
    assert len(scene["periphery"]) == 3
    assert scene["nearby"][0]["folded"]["present"] is True
    assert "details" not in scene["nearby"][0]
    assert scene["bounds"]["landmarks_total"] == 4
    assert scene["bounds"]["landmarks_shown_nearby"] == 1
    assert scene["bounds"]["landmarks_contoured"] == 3

    # Surface depth folds evidence with a drillable contour, while the five-axis
    # epistemic floor remains present at minimum sharpness.
    assert set(scene["focus"]["epistemic"]) == AXES
    assert scene["focus"]["epistemic_scope"] == "observation envelope, not nested claims"
    assert scene["focus"]["folded"]["present"] is True
    assert scene["focus"]["folded"]["fields"] > 0
    assert scene["focus"]["folded"]["drill"]
    assert "evidence" not in scene["focus"]

    route_names = [route["name"] for route in scene["routes"]]
    assert route_names == ["focus", "return"]
    assert scene["routes"][0]["steps"][0]["verb"] == "ground"
    assert scene["routes"][1]["steps"][0] == {"verb": "sweep", "args": {"agent": SUBJECT}}
    for route in scene["routes"]:
        assert route["effects"] == []
        assert route["risk"] == "ordinary"
        assert route["commit_required"] is False


def test_evidence_depth_expands_the_same_focus_without_changing_its_truth_state():
    from core.coord.orient import build_orientation

    surface = build_orientation(SUBJECT, "verb:capture", depth="surface", providers=_providers())
    evidence = build_orientation(SUBJECT, "verb:capture", depth="evidence", providers=_providers())

    assert evidence["focus"]["evidence"]["schema"] == "ground.result.v1"
    assert evidence["focus"]["epistemic"] == surface["focus"]["epistemic"]
    assert evidence["focus"]["blind"] == surface["focus"]["blind"]
    assert "details" in evidence["nearby"][0]
    assert "details" not in surface["nearby"][0]
    assert evidence["effects"] == surface["effects"] == []


def test_destination_is_typed_and_seat_continuity_cannot_borrow_a_peer():
    from core.coord.orient import build_orientation

    with pytest.raises(ValueError, match="typed"):
        build_orientation(SUBJECT, "capture", providers=_providers())
    with pytest.raises(ValueError, match="bound subject"):
        build_orientation(SUBJECT, "seat:someone-else", providers=_providers())

    own = build_orientation(SUBJECT, f"seat:{SUBJECT}", depth="evidence", providers=_providers())
    assert own["focus"]["evidence"]["mode"] == "continuity"
    assert own["routes"][0]["steps"][0]["args"]["continuity"] is True


def test_thread_focus_uses_capture_read_mode_and_never_mints():
    from core.coord.orient import build_orientation

    scene = build_orientation(
        SUBJECT,
        "thread:thread-7",
        depth="evidence",
        per_stream=37,
        providers=_providers(),
    )

    assert scene["focus"]["evidence"]["schema"] == "capture.thread.v1"
    assert scene["focus"]["evidence"]["bounds"]["per_stream"] == 37
    assert scene["routes"][0]["steps"][0] == {
        "verb": "capture",
        "args": {"thread": "thread-7", "as_doc": False, "per_stream": 37},
    }
    assert scene["effects"] == []


def test_aperture_values_refuse_unknown_physics_instead_of_guessing():
    from core.coord.orient import build_orientation

    with pytest.raises(ValueError, match="density"):
        build_orientation(SUBJECT, density="cinematic", providers=_providers())
    with pytest.raises(ValueError, match="depth"):
        build_orientation(SUBJECT, depth="omniscient", providers=_providers())


def test_compact_renderer_names_focus_contours_truth_floor_and_effects(monkeypatch, capsys):
    """The CLI's compact projection of the scene names the subject, the focus, every contoured landmark, the
    five-axis truth floor and the effects, and folds the evidence. The projection is agent_cli's agent view
    (_orient_doc); core.coord.orient's own text renderer had no caller left and was removed (e82255f96)."""
    import agent_cli
    from core.coord import orient as orient_module

    real_build = orient_module.build_orientation
    monkeypatch.setattr(
        orient_module,
        "build_orientation",
        lambda subject, target, **kw: real_build(subject, target, **kw, providers=_providers()),
    )
    monkeypatch.setenv("AURORA_OUTPUT", "agent")
    ns = argparse.Namespace(
        agent=SUBJECT, target="verb:capture", density="compact", depth="surface", per_stream=1000, json=False
    )
    assert agent_cli.cmd_orient(ns) == 0
    text = capsys.readouterr().out

    assert f"subject: {SUBJECT}" in text
    assert 'target: "verb:capture"' in text
    assert "landmarks_total: 4" in text
    assert text.count(",contoured,") == 3, "compact density contours landmarks; it never drops them"
    for axis in sorted(AXES):
        assert f"\n  {axis}: " in text, f"the truth floor is missing {axis}"
    assert "effects: none" in text
    # Surface depth folds the evidence: the focus reports every field of the ground result as folded (depth=evidence
    # reports 0), and neither the ground rung's claim nor the landmark details reach the render. There is no line cap:
    # the old renderer's "<= 12 lines" went with it, as this view prints one line per truth value and per landmark.
    assert f"\n  folded_fields: {len(_ground('verb:capture', subject=SUBJECT))}\n" in text
    assert "fixture endpoint is present" not in text
    assert "rank" not in text


def test_cli_mcp_and_toolbox_share_the_native_scene_seam(monkeypatch, tmp_path):
    import agent_cli
    import ai_setup_mcp
    from core.comm.toolbox import TOOLS, ToolBox
    from core.coord import orient as orient_module
    from scripts.checkers import check_door_parity

    fixture = {
        "schema": "orient.scene.v1",
        "subject": SUBJECT,
        "effects": [],
        "target": None,
        "aperture": {"density": "compact", "depth": "surface"},
        "position": {},
        "focus": None,
        "nearby": [],
        "periphery": [],
        "routes": [],
        "bounds": {},
        "blind": [],
    }
    monkeypatch.setattr(orient_module, "build_orientation", lambda *a, **k: dict(fixture))

    parser = agent_cli.build_parser()
    assert any(isinstance(a, argparse._SubParsersAction) for a in parser._actions)
    parsed = parser.parse_args(
        [
            "orient",
            "verb:capture",
            "--agent",
            SUBJECT,
            "--density",
            "compact",
            "--depth",
            "surface",
            "--json",
        ]
    )
    assert parsed.target == "verb:capture"
    assert parsed.agent == SUBJECT

    raw = asyncio.run(ai_setup_mcp.orient(agent=SUBJECT, target="verb:capture", density="compact", depth="surface"))
    assert json.loads(raw)["schema"] == "orient.scene.v1"
    assert "\n" not in raw, "model-facing MCP JSON should not spend tokens on indentation"

    advertised = {row["function"]["name"] for row in TOOLS}
    assert "orient" in advertised
    assert check_door_parity.MANIFEST["orient"] == "shared"

    tb = ToolBox(
        tmp_path,
        allow_exec=False,
        trust=False,
        allow_secrets=False,
        confirm=lambda *_: False,
        agent_id=SUBJECT,
    )
    monkeypatch.setattr(
        tb,
        "_agent_cli",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("orient must compose native structures, not shell through agent_cli")
        ),
    )
    tb_raw = tb.orient("verb:capture")
    assert json.loads(tb_raw)["schema"] == "orient.scene.v1"
    assert "\n" not in tb_raw, "model-facing ToolBox JSON should stay compact"


def test_unbound_toolbox_cannot_borrow_an_orientation_subject(tmp_path):
    from core.comm.toolbox import ToolBox

    tb = ToolBox(
        tmp_path,
        allow_exec=False,
        trust=False,
        allow_secrets=False,
        confirm=lambda *_: False,
    )
    with pytest.raises(ValueError, match="subject is required"):
        tb.orient("verb:capture")


def test_ground_knows_orient_is_an_open_read_seam_on_every_door():
    from core.coord.ground import ground

    result = ground("verb:orient", subject="sol")
    authorized = next(row for row in result["rungs"] if row["name"] == "authorized")

    assert authorized["state"] == "observed"
    assert {door: row["state"] for door, row in authorized["details"]["doors"].items()} == {
        "cli": "observed",
        "mcp": "observed",
        "toolbox": "observed",
    }
