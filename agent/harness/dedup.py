"""first_fire: one hook event, one unit of work, however many surfaces registered the hook.

A hook can be registered twice for one session: the user-level hooks the `aurora` launcher installs
(~/.claude/settings.json) and the project hooks this repo commits (.claude/settings.json). Claude
Code runs both, each with the identical payload, so without a guard every tool call is traced twice,
every outcome is credited twice and every turn end is judged twice. claude_pretooluse has carried its
own K0/C8-3 guard since the funnel's `surfaced` count ran ~2x hot; this is the same marker made
shareable, with the event name in the key so PostToolUse and PostToolUseFailure, or two different
hooks seeing one payload, never suppress each other.

An ATOMIC marker (O_CREAT|O_EXCL, no load-then-mark race) in the temp dir, keyed by the event and
the whole payload. The whole payload matters: tool events carry a per-call tool_use_id, so two
genuine identical calls (the same Read twice) hash differently and both run, while the two
registrations of ONE call share it and collapse (Heimdall's review, 2026-10-19). Fails OPEN: on any error the caller does its work, because a double count is a
smaller harm than a swallowed fire.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time

#: Two registrations of one event start within milliseconds of each other; a retry of the same
#: payload seconds later is rarer than a real repeat, so the window stays short.
WINDOW_S = 3.0
_SWEEP_AFTER_S = 60.0


def _marker_dir() -> str:
    """AKASHIC_HOOK_DEDUP_DIR overrides it: a test that runs one payload through two hook copies on
    purpose gives each run its own directory, because there each run IS a separate event."""
    return (os.environ.get("AKASHIC_HOOK_DEDUP_DIR") or "").strip() or os.path.join(
        tempfile.gettempdir(), "akashic-hook-dedup"
    )


def first_fire(event: str, payload: object) -> bool:
    """True for the first process to see (event, payload) within WINDOW_S; False for the rest."""
    try:
        key = hashlib.sha1(
            json.dumps([event, payload], sort_keys=True, default=str).encode("utf-8", "replace")
        ).hexdigest()[:24]
        d = _marker_dir()
        os.makedirs(d, exist_ok=True)
        now = time.time()
        try:  # lazy sweep keeps the directory tiny
            for name in os.listdir(d):
                p = os.path.join(d, name)
                if now - os.path.getmtime(p) > _SWEEP_AFTER_S:
                    os.remove(p)
        except Exception:
            pass
        path = os.path.join(d, f"{event}-{key}")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            if now - os.path.getmtime(path) < WINDOW_S:
                return False
            os.utime(path, (now, now))  # an old marker: this is a new fire, take it over
            return True
    except Exception:
        return True
