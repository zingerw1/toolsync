"""Paths, tunables, and shared data tables.

Every other module reads its constants from here. ``_ALIASES`` is the one value
reassigned at runtime (the CLI loads it from ``.toolsync/aliases`` and stores it
back here), so code that needs it references ``config._ALIASES`` live rather than
importing the name by value.
"""

import os
from pathlib import Path

# ── Locations ─────────────────────────────────────────────────────────────────
# These are module-level so every other module reads them live as config.<name>;
# resolve_paths() (called once by the CLI) repoints them from --root/--bin, the
# TOOLSYNC_ROOT/TOOLSYNC_BIN env vars, or ~/.config/toolsync/config. Until then
# they hold the defaults, so importing the package stays side-effect-free.
DEFAULT_ROOT = Path.home() / "tools"
DEFAULT_BIN = Path.home() / ".local" / "bin"
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "toolsync" / "config"

TOOLS_DIR = DEFAULT_ROOT
BIN_DIR = DEFAULT_BIN
STATE_DIR = TOOLS_DIR / ".toolsync"
STATE_FILE = STATE_DIR / "state.json"
IGNORE_FILE = STATE_DIR / "ignore"
ALIASES_FILE = STATE_DIR / "aliases"


def _read_config_file():
    """Parse ~/.config/toolsync/config: simple `key = value` lines (root, bin)."""
    out = {}
    try:
        text = CONFIG_FILE.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip().lower()] = val.strip()
    return out


def resolve_paths(root=None, bin_dir=None):
    """Set TOOLS_DIR / BIN_DIR / STATE_* from (in precedence order) explicit
    arguments, the TOOLSYNC_ROOT / TOOLSYNC_BIN env vars, the config file, then
    the built-in defaults. Call once at startup before any path is read."""
    global TOOLS_DIR, BIN_DIR, STATE_DIR, STATE_FILE, IGNORE_FILE, ALIASES_FILE
    cfg = _read_config_file()
    root = root or os.environ.get("TOOLSYNC_ROOT") or cfg.get("root")
    bin_dir = bin_dir or os.environ.get("TOOLSYNC_BIN") or cfg.get("bin")
    TOOLS_DIR = Path(root).expanduser() if root else DEFAULT_ROOT
    BIN_DIR = Path(bin_dir).expanduser() if bin_dir else DEFAULT_BIN
    STATE_DIR = TOOLS_DIR / ".toolsync"
    STATE_FILE = STATE_DIR / "state.json"
    IGNORE_FILE = STATE_DIR / "ignore"
    ALIASES_FILE = STATE_DIR / "aliases"
    return TOOLS_DIR, BIN_DIR

# Bump whenever the install/verify logic changes in a way that should re-apply
# to already-synced tools. A stored state entry with an older version is treated
# as stale regardless of directory signature, so fixes self-apply on the next
# run instead of requiring a manual state.json edit.
TOOLSYNC_VERSION = 20

# ── Defaults written to .toolsync/ on first run ─────────────────────────────────
DEFAULT_ALIASES = [
    "# <directory-name> = <command-name>  (one per line - overrides the",
    "# default command name toolsync would otherwise pick, for tools whose",
    "# repo/dir name isn't what you actually want to type)",
    "enum4linux-ng = enum4linux",
]

DEFAULT_IGNORE = [
    "# one directory name per line - these are skipped entirely by toolsync.",
    "# (docker/web services, installers, payload bundles with no local CLI entrypoint)",
    "bloodhound-ce",
    "metasploit",
    "Peas",
]

# Directory -> command-name overrides; loaded from ALIASES_FILE by the CLI.
_ALIASES = {}

# ── File classification ─────────────────────────────────────────────────────────
SKIP_SUFFIXES = {
    ".exe", ".dll", ".msi", ".zip", ".yaml", ".yml", ".md", ".history",
    ".version", ".docx", ".ps1", ".bak", ".log", ".json",
}
SKIP_NAMES = {"LICENSE", "README.md"}

ARCHIVE_SUFFIXES = {".zip", ".7z", ".tar.gz", ".tgz", ".tar", ".tar.bz2", ".tbz2"}

# pip/compiler stderr substring -> apt package that provides the missing header.
MISSING_HEADER_TO_APT_PKG = {
    "lber.h": "libldap2-dev",
    "ldap.h": "libldap2-dev",
    "sasl.h": "libsasl2-dev",
    "Python.h": "python3-dev",
    "ffi.h": "libffi-dev",
    "openssl/": "libssl-dev",
    "curl/curl.h": "libcurl4-openssl-dev",
    "xml2": "libxml2-dev",
    "xslt": "libxslt1-dev",
    "zlib.h": "zlib1g-dev",
}

# ── Linking / detection tables ──────────────────────────────────────────────────
# Marker written into wrapper scripts so toolsync recognises its own files in BIN_DIR.
WRAPPER_MARKER = "# toolsync-wrapper"

# Conventional build-output dirs searched for a prebuilt binary (last resort).
COMMON_BUILD_OUTPUT_DIRS = ("run", "bin", "src", "dist", "build", "release", "target/release")

# When several copies of the same foreign artifact exist (e.g. x64/ and Win32/),
# prefer a build from a dir earlier in this list. 64-bit-first, no tool names.
FOREIGN_DIR_PREFERENCE = ("x64", "x86_64", "amd64", "release", "bin", "win32", "x86")

# When one tool ships several forms sharing a name (SharpHound.exe + .ps1),
# prefer the runnable payload over the plain script.
FOREIGN_TYPE_PRIORITY = (".exe", ".jar", ".ps1")
