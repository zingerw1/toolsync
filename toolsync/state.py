"""InstallResult plus the JSON state file and the ignore/aliases lists."""
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .config import DEFAULT_ALIASES, DEFAULT_IGNORE


@dataclass
class InstallResult:
    ok: bool
    entrypoint: Path | None = None
    name_hint: str | None = None  # override command name derivation if set
    note: str = ""
    missing_apt_pkgs: set = field(default_factory=set)


def load_ignore():
    if not config.IGNORE_FILE.exists():
        config.IGNORE_FILE.write_text("\n".join(DEFAULT_IGNORE) + "\n")
    return {
        line.strip()
        for line in config.IGNORE_FILE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def load_aliases():
    if not config.ALIASES_FILE.exists():
        config.ALIASES_FILE.write_text("\n".join(DEFAULT_ALIASES) + "\n")
    result = {}
    for line in config.ALIASES_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        dirname, _, cmdname = line.partition("=")
        result[dirname.strip()] = cmdname.strip()
    return result


def load_state():
    if config.STATE_FILE.exists():
        return json.loads(config.STATE_FILE.read_text())
    return {}


def save_state(state):
    config.STATE_DIR.mkdir(exist_ok=True)
    config.STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))
