"""Runtime verification + mapping build errors to apt packages."""
import os
import re
import subprocess
from pathlib import Path

from .config import MISSING_HEADER_TO_APT_PKG


def detect_missing_apt_pkgs(stderr: str) -> set:
    pkgs = set()
    if not stderr:
        return pkgs
    for header, pkg in MISSING_HEADER_TO_APT_PKG.items():
        if header in stderr:
            pkgs.add(pkg)
    return pkgs


def verify_runs(entrypoint: Path, extra_env=None):
    """Run the linked entrypoint with a harmless flag to confirm it actually
    starts (doesn't ModuleNotFoundError / ImportError / exit non-zero before
    reaching its own arg parsing). Returns (ok: bool, tail: str)."""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    for flag in ("--help", "-h"):
        try:
            r = subprocess.run(
                [str(entrypoint), flag],
                capture_output=True, text=True, timeout=10, env=env,
            )
        except subprocess.TimeoutExpired:
            return True, ""  # started fine, just didn't exit - good enough
        except OSError as e:
            return False, str(e)
        combined = (r.stdout or "") + (r.stderr or "")
        bad_markers = (
            "ModuleNotFoundError", "ImportError", "Traceback (most recent call last)",
            "SyntaxError", "IndentationError", "NameError", "AttributeError", "TypeError:",
        )
        if any(m in combined for m in bad_markers):
            tail = combined.strip().splitlines()
            return False, tail[-1] if tail else "unknown import error"
        # Some compiled tools print a short fatal error and still exit 0
        # (e.g. john the ripper's "Cannot find John home" when argv[0]
        # doesn't contain a path) - argparse/click's "exit 2 on an
        # unrecognized flag, but still print real usage text" is NOT this:
        # that case has help-shaped content (usage:/options:/etc), so only
        # flag a short, error-shaped message that has none of that.
        lower = combined.lower()
        looks_like_help = any(tok in lower for tok in ("usage:", "options:", "arguments:", "--help", "-h,"))
        looks_like_error = bool(re.search(r"\berror\b", lower)) or "cannot find" in lower
        if looks_like_error and not looks_like_help and len(combined) < 500:
            tail = combined.strip().splitlines()
            return False, tail[-1] if tail else "unrecognized error"
        # argparse/click/etc. exit 2 on unrecognized flag but still "ran" -
        # that counts as success (the program started). A hard crash (big
        # traceback with no flag recognition) was already caught above.
        return True, ""
    return True, ""
