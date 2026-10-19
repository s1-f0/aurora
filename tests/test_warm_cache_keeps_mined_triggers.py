"""PIN: the boot-time warm cache carries the same lesson items as the TTL rebuild.

recall-at reads one disk cache. Two paths write it: _cached_items() rebuilds it when the TTL lapses,
as usefulness(mined_triggers(projected items)); warm_cache() pre-warms it at SessionStart and boot,
as usefulness(projected items) -- without the trigger terms mined from credited FAIL->SUCCESS flips.
MEASURED 2026-10-19 after importing 219 flips: the miner gave 156 lessons trigger terms, the
freshly warmed cache held 0, and every session's first two minutes (the default TTL) matched without
them. One cache, one composition.

Run: uv run pytest tests/test_warm_cache_keeps_mined_triggers.py -q -p no:cacheprovider
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class _Store:
    def load_all_learnings_from_store(self):
        return [
            {
                "experiment_name": "a",
                "recommendation": "Use when a hook fires twice, before trusting counts: dedupe by payload.",
                "success": "yes",
                "agent_id": "pin",
            }
        ]


def test_warm_cache_applies_the_mined_triggers(tmp_path, monkeypatch):
    import core.recall.at_action as aa

    monkeypatch.setattr(aa, "_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(aa, "_CACHE_FILE", str(tmp_path / "lesson_items.json"))

    def _mine(items):
        for it in items:
            it["trigger_terms"] = ["mined"]
        return items

    monkeypatch.setattr(aa, "_with_mined_triggers", _mine)
    monkeypatch.setattr(aa, "_with_usefulness", lambda items: items)
    assert aa.warm_cache(learning_store=_Store()) == 1
    with open(aa._CACHE_FILE, encoding="utf-8") as f:
        items = json.load(f)
    assert items[0].get("trigger_terms") == ["mined"], (
        "warm_cache wrote the cache without the mined trigger terms that _cached_items() applies"
    )
