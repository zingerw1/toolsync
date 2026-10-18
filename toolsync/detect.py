"""Entry-point detection: python / native-binary / prebuilt / foreign artifact."""
import os
import re
from pathlib import Path

from .config import (COMMON_BUILD_OUTPUT_DIRS, FOREIGN_DIR_PREFERENCE,
                     FOREIGN_TYPE_PRIORITY, SKIP_NAMES, SKIP_SUFFIXES)
from .fsutil import _norm
from .runners import FOREIGN_RUNNERS


def find_python_entrypoints(dirpath: Path):
    """Entrypoint candidates: <dirname>.py, main.py, <dirname>/<dirname>.py,
    or (if there's exactly one top-level .py file) that file."""
    candidates = []
    name_variants = {dirpath.name, dirpath.name.lower(), dirpath.name.replace("-", "_").replace(" ", "_")}
    for depth_dir in (dirpath, dirpath / dirpath.name):
        if not depth_dir.is_dir():
            continue
        for variant in name_variants:
            for fname in (f"{variant}.py", "main.py"):
                cand = depth_dir / fname
                if cand.is_file():
                    candidates.append(cand)
    if not candidates:
        top_py = [
            p for p in dirpath.glob("*.py")
            if p.is_file() and p.name not in {"setup.py", "conftest.py", "__init__.py"}
        ]
        if len(top_py) == 1:
            candidates.append(top_py[0])
    seen = set()
    uniq = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _candidate_top_files(dirpath: Path):
    return [
        p for p in dirpath.iterdir()
        if p.is_file() and not p.name.startswith(".")
        and p.suffix not in SKIP_SUFFIXES and p.name not in SKIP_NAMES
    ]


def find_binary_entrypoint(dirpath: Path):
    """A single executable file, checked at the top level, one level deep in
    a <dirname>/<dirname>/ nesting (e.g. `netexecspray/netexecspray/
    netexecspray.py`, `smbmap/smbmap/smbmap`), and one level deep in a
    <dirname>-<version>/ nesting (e.g. a release zip extracted to
    `hashcat/hashcat-7.1.2/`)."""
    name_variants = {dirpath.name, dirpath.name.lower()}
    search_dirs = [dirpath]
    inner = dirpath / dirpath.name
    if inner.is_dir():
        search_dirs.append(inner)
    dir_norm = _norm(dirpath.name)
    for sub in dirpath.iterdir():
        if sub.is_dir() and sub != inner and _norm(sub.name).startswith(dir_norm) and dir_norm:
            search_dirs.append(sub)

    for search_dir in search_dirs:
        for variant in name_variants:
            for fname in (variant, f"{variant}.sh", f"{variant}.py", f"{variant}.bin"):
                cand = search_dir / fname
                if cand.is_file() and not cand.is_symlink():
                    return cand

    top_files = _candidate_top_files(dirpath)
    executables = [p for p in top_files if os.access(p, os.X_OK)]
    if len(executables) == 1:
        return executables[0]

    for search_dir in search_dirs[1:]:
        inner_files = _candidate_top_files(search_dir)
        inner_executables = [p for p in inner_files if os.access(p, os.X_OK)]
        if len(inner_executables) == 1:
            return inner_executables[0]

    return find_prebuilt_binary_deep(dirpath)



def find_prebuilt_binary_deep(dirpath: Path):
    """Last-resort fallback: a tool can ship a prebuilt binary under a
    conventional output dir whose name doesn't relate to the repo name at
    all (e.g. john-jumbo's binary is actually called `john`, sitting in
    `run/`). Look in those conventional dirs for exactly one no-extension,
    reasonably-sized executable - scripts (which always keep a .py/.pl/.sh
    extension in this tree) are excluded by the no-suffix check, so a
    directory full of helper scripts next to the real binary doesn't
    confuse this."""
    candidates = []
    for rel in COMMON_BUILD_OUTPUT_DIRS:
        d = dirpath / rel
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if (
                p.is_file() and not p.is_symlink() and p.suffix == ""
                and os.access(p, os.X_OK) and p.stat().st_size > 10_000
            ):
                candidates.append(p)
    if not candidates:
        return None
    # Prefer a candidate whose name is literally one of the dir name's
    # word-tokens (e.g. john-jumbo -> {"john", "jumbo"} -> run/john) over
    # the bare "exactly one candidate total" rule - this is what lets a
    # repo named after a project ("john-jumbo") correctly resolve to a
    # binary named after the underlying tool ("john") without an alias.
    tokens = {t.lower() for t in re.split(r"[-_ ]+", dirpath.name) if t}
    token_matches = [p for p in candidates if p.name.lower() in tokens]
    if len(token_matches) == 1:
        return token_matches[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _foreign_search_dirs(dirpath: Path):
    """All the places a foreign artifact might sit, ordered by preference:
    the top level, a <dirname>/<dirname> nesting, a <dirname>-version nesting,
    and conventional output dirs (x64/Release/bin/...). Earlier = preferred,
    so x64/ wins over Win32/ when a tool ships both."""
    dirs = [dirpath]
    inner = dirpath / dirpath.name
    if inner.is_dir():
        dirs.append(inner)
    dir_norm = _norm(dirpath.name)
    for sub in sorted(dirpath.iterdir()):
        if sub.is_dir() and sub != inner and dir_norm and _norm(sub.name).startswith(dir_norm):
            dirs.append(sub)
    # conventional output dirs, in preference order
    present = {p.name.lower(): p for p in dirpath.iterdir() if p.is_dir()}
    for pref in FOREIGN_DIR_PREFERENCE:
        if pref in present and present[pref] not in dirs:
            dirs.append(present[pref])
    return dirs


def find_foreign_entrypoint(dirpath: Path):
    """Find a single foreign artifact (.exe/.jar/.ps1/...) to treat as the
    tool's entrypoint, by the same name-match-then-single-candidate logic used
    for native binaries. Prefers a file whose name matches the directory name;
    falls back to the sole foreign artifact of a given type if there's exactly
    one. Returns (path, suffix) or None. Keyed purely on file type and name, so
    it's not specific to any particular tool."""
    name_variants = {_norm(dirpath.name)}
    search_dirs = _foreign_search_dirs(dirpath)

    # 1) a foreign file whose (normalized) name matches the directory name,
    #    taking search-dir preference order into account (x64 before Win32).
    for search_dir in search_dirs:
        try:
            entries = sorted(search_dir.iterdir())
        except OSError:
            continue
        for p in entries:
            if not p.is_file() or p.is_symlink():
                continue
            suf = p.suffix.lower()
            if suf in FOREIGN_RUNNERS and _norm(p.stem) in name_variants:
                return p, suf

    # 2) no name match: look across the top level and primary nested dir.
    for search_dir in search_dirs[:2]:
        try:
            foreign = [
                p for p in sorted(search_dir.iterdir())
                if p.is_file() and not p.is_symlink() and p.suffix.lower() in FOREIGN_RUNNERS
            ]
        except OSError:
            continue
        if not foreign:
            continue
        # a) exactly one foreign artifact - unambiguous.
        if len(foreign) == 1:
            return foreign[0], foreign[0].suffix.lower()
        # b) several, but all the same logical tool shipped in multiple forms
        #    (e.g. SharpHound.exe + SharpHound.ps1 - one normalized stem):
        #    pick by type priority, runnable payload before plain script.
        stems = {_norm(p.stem) for p in foreign}
        if len(stems) == 1:
            for suf in FOREIGN_TYPE_PRIORITY:
                match = [p for p in foreign if p.suffix.lower() == suf]
                if match:
                    return match[0], suf
    return None
