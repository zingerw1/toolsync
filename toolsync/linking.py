"""Command-name resolution and everything that writes into config.BIN_DIR."""
import os
import re
import shutil
from pathlib import Path

from . import config
from .config import (WRAPPER_MARKER)
from .verify import verify_runs


def default_command_name(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "", path.name)


def command_name_for(path: Path) -> str:
    if path.name in config._ALIASES:
        old_name = default_command_name(path)
        new_name = config._ALIASES[path.name]
        if old_name != new_name:
            old_link = config.BIN_DIR / old_name
            if existing_link_is_ours(old_link):
                old_link.unlink(missing_ok=True)
        return new_name
    return default_command_name(path)


def existing_link_is_ours(link: Path) -> bool:
    if link.is_symlink() or not link.is_file():
        try:
            target = link.resolve()
        except OSError:
            return False
        return config.TOOLS_DIR in target.parents or target.parent == config.TOOLS_DIR
    # A plain (non-symlink) file in config.BIN_DIR is one of ours only if it's a
    # wrapper script toolsync itself wrote (see write_wrapper_script) -
    # otherwise a real external file here is exactly the kind of conflict
    # this function exists to detect.
    try:
        return WRAPPER_MARKER in link.read_text(errors="ignore")[:200]
    except OSError:
        return False


def symlink_into_bin(entrypoint: Path, name: str, report):
    link = config.BIN_DIR / name
    if link.exists() or link.is_symlink():
        if not existing_link_is_ours(link):
            report.append(("CONFLICT", name, f"~/.local/bin/{name} already points outside ~/tools - not touching it"))
            return False
        try:
            if link.resolve() == entrypoint.resolve():
                return True
        except OSError:
            pass
        # It's "ours" but points at a different entrypoint than this call -
        # fine if that's the *same* tool directory (e.g. its venv/binary
        # path legitimately changed), but if it's a *different* tools/
        # subdirectory entirely, that's two separate repos both producing
        # the command name 'dnstool'. Without this check that's a silent,
        # order-dependent overwrite (whichever directory syncs later wins,
        # with no report at all) - flag it instead and leave the
        # pre-existing link alone.
        if link.is_symlink():
            try:
                existing_target = link.resolve()
                same_tool = (
                    config.TOOLS_DIR in existing_target.parents
                    and existing_target.relative_to(config.TOOLS_DIR).parts[0] == entrypoint.resolve().relative_to(config.TOOLS_DIR).parts[0]
                )
            except (OSError, ValueError):
                same_tool = True  # can't prove otherwise - don't block on a shaky comparison
            if not same_tool:
                report.append(("CONFLICT", name, f"'{name}' is already linked to a different tool ({existing_target}) - not overwriting; rename one or set an alias in .toolsync/aliases"))
                return False
    other_path = shutil.which(name)
    if other_path and Path(other_path) != link and not existing_link_is_ours(Path(other_path)):
        report.append(("CONFLICT", name, f"'{name}' already resolves to {other_path} on PATH ahead of ~/.local/bin - not linking"))
        return False
    config.BIN_DIR.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    link.symlink_to(entrypoint)
    return True


def _external_command_outside_tools(name: str):
    """Return the path of an executable named `name` found on PATH outside
    config.BIN_DIR and outside ~/tools, or None. Unlike shutil.which it ignores
    config.BIN_DIR, so it reports the *system* command even when config.BIN_DIR is earlier
    on PATH and already holds a toolsync link of the same name."""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        dpath = Path(d)
        if dpath == config.BIN_DIR:
            continue
        cand = dpath / name
        try:
            if cand.is_file() and os.access(cand, os.X_OK) and not existing_link_is_ours(cand):
                return cand
        except OSError:
            continue
    return None


def link_case_aliases(names, report):
    """For each command already placed in config.BIN_DIR under `names`, also create a
    lowercase-named alias pointing at it, so a tool is callable regardless of
    the case its repo/dir/file happened to use (PetitPotam / petitPotam.py ->
    petitpotam, petitpotam.py). Filesystems here are case-sensitive, so
    'petitpotam' and 'PetitPotam' are genuinely different names and both have
    to exist.

    Best-effort and non-destructive: an alias is created only when the
    lowercase name is free (or is already exactly this alias) - it never
    overwrites another command, ours or external - and a wrapper command is
    aliased with a symlink to the wrapper, so it works for plain-symlink and
    exec-wrapper commands alike. Silent by design: a lowercase name that's
    taken is simply left alone, no CONFLICT noise."""
    for name in list(names):
        lower = name.lower()
        if lower == name:
            continue
        target = config.BIN_DIR / name
        link = config.BIN_DIR / lower
        if not (target.exists() or target.is_symlink()):
            continue
        # Don't shadow a real command of that name that lives outside ~/tools
        # (e.g. an apt-installed 'responder') - same rule the primary links
        # follow, so an alias never silently hijacks a system tool. Checked by
        # scanning PATH *excluding* config.BIN_DIR, because config.BIN_DIR may sit ahead of
        # /usr/local/bin and would otherwise just find our own alias. Also undo
        # such an alias if an earlier run created one before the conflict.
        if _external_command_outside_tools(lower):
            if link.is_symlink() and existing_link_is_ours(link):
                link.unlink(missing_ok=True)
            continue
        if link.exists() or link.is_symlink():
            try:
                if link.resolve() == target.resolve():
                    continue  # already the right alias
            except OSError:
                pass
            continue  # name taken by something else - don't clobber it
        try:
            link.symlink_to(name)  # relative alias within config.BIN_DIR
        except OSError:
            pass


def link_entrypoint_variants(entrypoint: Path, primary_name: str, report) -> bool:
    """Link the entrypoint under its clean command name (e.g. 'windapsearch')
    AND, when it differs, under the entrypoint file's own raw name (e.g.
    'windapsearch.py') - so a tool is callable either way without having to
    remember which form toolsync picked. The raw-name link is best-effort:
    a conflict there (CONFLICT/already-linked-elsewhere) is reported but
    never blocks the primary link from succeeding. Lowercase aliases of both
    names are added too, so case never matters when typing the command."""
    primary_ok = symlink_into_bin(entrypoint, primary_name, report)
    raw_name = entrypoint.name
    if raw_name != primary_name:
        symlink_into_bin(entrypoint, raw_name, report)
    link_case_aliases([primary_name, raw_name], report)
    return primary_ok


def write_wrapper_script(entrypoint: Path, link: Path) -> bool:
    """Replace a symlink with a tiny exec wrapper. Some compiled tools
    (e.g. john the ripper) refuse to locate their own resource files
    (john.conf etc.) unless argv[0] contains a '/' - a bare PATH-resolved
    symlink name never does, but a wrapper that execs the entrypoint by its
    absolute path always does."""
    try:
        link.unlink(missing_ok=True)
        link.write_text(f'#!/bin/sh\n{WRAPPER_MARKER}\nexec "{entrypoint}" "$@"\n')
        link.chmod(0o755)
        return True
    except OSError:
        return False


def write_runner_wrapper(entrypoint: Path, link: Path, runner: list) -> bool:
    """Write a wrapper that execs `entrypoint` through an interpreter, e.g.
    `exec java -jar /abs/tool.jar "$@"` or `exec mono /abs/tool.exe "$@"`.
    This is how a .jar/.exe/.ps1 becomes a plain command on PATH."""
    quoted = " ".join(f'"{a}"' for a in runner)
    try:
        link.unlink(missing_ok=True)
        link.write_text(f'#!/bin/sh\n{WRAPPER_MARKER}\nexec {quoted} "{entrypoint}" "$@"\n')
        link.chmod(0o755)
        return True
    except OSError:
        return False


def link_and_verify(entrypoint: Path, primary_name: str, report, label: str) -> bool:
    """Link both name variants, verify the primary one actually runs, and
    self-heal by upgrading to an exec wrapper (see write_wrapper_script) if
    a plain symlink doesn't. Appends the final OK/WARN line and returns
    whether it's genuinely usable."""
    if not link_entrypoint_variants(entrypoint, primary_name, report):
        return False
    link = config.BIN_DIR / primary_name
    # Verify the way a user actually invokes it - by bare command name
    # through PATH, not by the symlink's absolute path. Those are NOT
    # equivalent: subprocess given an absolute path always hands the
    # program an argv[0] containing '/', but a shell resolving a bare
    # command name through PATH does not - so an absolute-path check can
    # never catch a tool that breaks specifically on that (e.g. john the
    # ripper's "Cannot find John home" when invoked as a bare PATH name).
    ok, tail = verify_runs(primary_name, extra_env={"PATH": f"{config.BIN_DIR}:{os.environ.get('PATH', '')}"})
    reported = False
    if not ok and write_wrapper_script(entrypoint, link):
        ok, tail = verify_runs(primary_name, extra_env={"PATH": f"{config.BIN_DIR}:{os.environ.get('PATH', '')}"})
        if ok:
            report.append(("OK", label, f"linked '{primary_name}' via exec wrapper (binary needs argv[0] with a path) -> {entrypoint}"))
            reported = True
    if not reported:
        if ok:
            report.append(("OK", label, f"linked '{primary_name}' -> {entrypoint}"))
        else:
            report.append(("WARN", label, f"linked '{primary_name}' but it fails to run: {tail}"))
    return ok
