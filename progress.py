"""
Progress markers for run-upgrade.sh's condensed display.

When UPGRADE_PROGRESS=1 is set (taskui.py sets it), these print marker lines
that taskui.py turns into the in-place "[5/9] Stage image ... 63%" display.
Run by hand, without the variable, they print nothing, so the scripts
behave exactly as before.
"""
import os

ENABLED = os.environ.get("UPGRADE_PROGRESS") == "1"


def progress(msg: str) -> None:
    """Live status for the current stage, e.g. '5/8 commands' or '63% (316/502 MB)'."""
    if ENABLED:
        print(f"@@PROGRESS {msg}", flush=True)


def note(msg: str) -> None:
    """Short summary shown after ok/changed on the stage's final line."""
    if ENABLED:
        print(f"@@NOTE {msg}", flush=True)
