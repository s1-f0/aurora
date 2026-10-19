"""Stable entry point: runs agent/harness/hooks/snippet_autofile.py, the one canonical copy.

Version 1 ships un-wired (see the canonical docstring); this is the path its design notes name for
the live registration. Behaviour lives ONLY in the canonical file; this shim must stay policy-free
(scripts/checkers/check_wiring.py enforces the shape).
"""

import os
import runpy

runpy.run_path(
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "agent",
        "harness",
        "hooks",
        "snippet_autofile.py",
    ),
    run_name="__main__",
)
