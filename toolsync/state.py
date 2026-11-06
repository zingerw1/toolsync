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
        if line.strip() and not line.strip().startswith("#") and not line.strip().startswith("*.")
    }


def load_ignore_extensions():
    """Lines in the ignore file of the form `*.exe` - a whole file type to
    never wrap/link, without having to name every directory that ships one.
    New tools that drop a binary of that type are covered automatically."""
    if not config.IGNORE_FILE.exists():
        config.IGNORE_FILE.write_text("\n".join(DEFAULT_IGNORE) + "\n")
    exts = set()
    for line in config.IGNORE_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("*."):
            exts.add(line[1:])  # "*.exe" -> ".exe"
    return exts


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
        try:
            return json.loads(config.STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            # Corrupted by an interrupted save (Ctrl-C / crash / power loss
            # mid-write) - treat as "no state yet" rather than crashing every
            # future run; the next successful save_state() overwrites it.
            return {}
    return {}


def save_state(state):
    config.STATE_DIR.mkdir(exist_ok=True)
    tmp = config.STATE_FILE.with_suffix(config.STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(config.STATE_FILE)  # atomic on same filesystem - no partial-write window
