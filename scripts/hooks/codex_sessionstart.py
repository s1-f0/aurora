"""Stable entry point: runs agent/harness/hooks/codex_sessionstart.py, the one canonical copy.

Kept because existing user-level Codex registrations (e.g. `py E:/AI-Setup/scripts/hooks/codex_sessionstart.py`
in ~/.codex/hooks.json) point here. Behaviour lives ONLY in the canonical file; this shim must stay
policy-free (scripts/checkers/check_wiring.py enforces the shape). New installs:
`agent_cli.py hooks install --harness codex`.
"""

import os
import runpy

runpy.run_path(
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "agent",
        "harness",
        "hooks",
        "codex_sessionstart.py",
    ),
    run_name="__main__",
)
