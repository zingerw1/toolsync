"""Filesystem helpers: change-signature, chmod, shebang healing, name norm."""
import hashlib
import os
import re
import stat
from pathlib import Path


def dir_signature(path: Path) -> str:
    """Cheap change-detector: names+mtimes of top two levels. Good enough to
    notice 'entrypoint added' / 'requirements.txt changed' without hashing contents."""
    skip_dirnames = {"venv", ".git", "build", "__pycache__", "target", "node_modules"}
    parts = []
    try:
        for p in sorted(path.rglob("*")):
            if set(p.parts) & skip_dirnames or any(part.endswith((".egg-info", ".dist-info")) for part in p.parts):
                continue
            if p.is_file():
                parts.append(f"{p.relative_to(path)}:{p.stat().st_mtime_ns}")
            if len(parts) > 2000:
                break
    except OSError:
        pass
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def chmod_x(path: Path):
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def fix_shebang(path: Path, interpreter: str) -> bool:
    """Rewrite an existing python shebang to point at `interpreter`, OR -
    for a .py file picked as an entrypoint that has no shebang line at all
    (just a top comment, like peas.py) - add one. Without this, such a file
    is only runnable as `python3 file.py`; chmod +x and a PATH symlink
    alone can't make the kernel execute it (ENOEXEC: 'Exec format error'),
    so it gets silently treated as a dead link even though the tool is
    perfectly fine.

    It also heals a CRLF (Windows) shebang: a trailing '\r' left in the
    shebang line makes the kernel try to exec an interpreter whose name ends
    in '\r' ("env: '\r': No such file or directory" - e.g. ntlm_theft.py), so
    such a line is effectively broken and gets rewritten even though its bytes
    technically start with '#!'. A .py whose shebang has decayed to a bare
    `#!/usr/bin/env` with no interpreter after it is healed the same way.

    SAFETY: this is also called on arbitrary executable loose files
    (process_loose_file runs it unconditionally on every top-level
    executable, every run) - a compiled ELF/PE binary has no shebang
    either, and blindly prepending one to "fix" that would corrupt the
    binary beyond repair. The no-shebang branch therefore requires BOTH a
    .py suffix and a null-byte-free sample as independent proof this is
    actually text source, never just "doesn't start with #!". The healing
    branches below are likewise limited to python-referencing shebangs and
    (for the bare-env case) .py files, so a non-python script is never
    force-converted."""
    try:
        with open(path, "rb") as f:
            first = f.readline()
    except OSError:
        return False
    new_first = f"#!{interpreter}\n".encode()
    if first.startswith(b"#!"):
        # Shebang line without its end-of-line and without a trailing CR, so a
        # CRLF line ending can't hide what the interpreter really is.
        line = first.split(b"\n", 1)[0].rstrip(b"\r")
        body = line[2:].strip()
        is_python = b"python" in line
        # A .py whose shebang is a bare/broken env (no real interpreter after
        # it - typically CRLF residue) clearly wants python; heal it.
        broken_env = path.suffix == ".py" and body in (b"/usr/bin/env", b"env", b"")
        if not (is_python or broken_env):
            return False
        if first == new_first:
            return False
        rest = path.read_bytes()[len(first):]
        path.write_bytes(new_first + rest)
        return True
    if path.suffix != ".py":
        return False
    rest = first + path.read_bytes()[len(first):]
    if b"\x00" in rest[:4096]:
        return False
    path.write_bytes(new_first + rest)
    return True


def has_php_shebang(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            first = f.readline()
    except OSError:
        return False
    return first.startswith(b"#!") and b"php" in first


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())
