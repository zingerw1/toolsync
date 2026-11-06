"""Universal install helpers layered on the directory-scan core.

Subcommands:
  add <path | git-url> ...   install a tool from anywhere (clone / copy into the
                             root, or link a lone script in place)
  self-install               put the `toolsync` command on PATH, check PATH
  doctor                     report root/bin/PATH health and offer to fix PATH
  locate <name>              find where a tool lives and how to run it, by
                             substring/fuzzy match on its directory or command
                             name, falling back to a recursive filename search
                             (e.g. `locate nc.exe`) when nothing matches at the
                             top level
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import config
from .fsutil import chmod_x, dir_signature, fix_shebang
from .handlers import run_pipeline
from .linking import command_name_for, existing_link_is_ours, link_and_verify
from .report import print_report
from .state import load_aliases, load_state, save_state


def dispatch(cmd, argv):
    return {"add": cmd_add, "self-install": cmd_self_install, "doctor": cmd_doctor,
           "locate": cmd_locate}[cmd](argv)


def _parser(sub, desc):
    p = argparse.ArgumentParser(prog=f"toolsync {sub}", description=desc)
    p.add_argument("--root", metavar="DIR", help="tools directory (default: ~/tools / $TOOLSYNC_ROOT)")
    p.add_argument("--bin", metavar="DIR", dest="bin_dir", help="PATH link directory (default: ~/.local/bin)")
    return p


# ── PATH doctor ─────────────────────────────────────────────────────────────
def _bin_on_path():
    for e in os.environ.get("PATH", "").split(os.pathsep):
        if not e:
            continue
        try:
            if Path(e).expanduser().resolve() == config.BIN_DIR.resolve():
                return True
        except OSError:
            if Path(e).expanduser() == config.BIN_DIR:
                return True
    return False


def _shell_rc():
    shell = os.environ.get("SHELL", "")
    home = Path.home()
    if "zsh" in shell:
        return home / ".zshrc"
    if "bash" in shell:
        return home / ".bashrc"
    return home / ".profile"


def ensure_path(assume_yes=False):
    """Make sure BIN_DIR is on PATH; offer to append it to the shell rc."""
    if _bin_on_path():
        print(f"  PATH  : {config.BIN_DIR} is on PATH ✓")
        return True
    rc = _shell_rc()
    line = f'export PATH="{config.BIN_DIR}:$PATH"'
    print(f"  PATH  : {config.BIN_DIR} is NOT on PATH")
    print(f"          add to {rc}:  {line}")
    if assume_yes:
        answer = "y"
    elif sys.stdin.isatty():
        try:
            answer = input(f"          append it to {rc} now? [y/N] ").strip().lower()
        except EOFError:
            answer = "n"
    else:
        answer = "n"
    if answer != "y":
        print("          skipped — add it yourself, then open a new shell.")
        return False
    existing = rc.read_text() if rc.exists() else ""
    if line in existing:
        print("          already present.")
        return True
    with open(rc, "a") as f:
        f.write(f"\n# added by toolsync\n{line}\n")
    print(f"          added — run:  source {rc}")
    return True


def cmd_doctor(argv):
    p = _parser("doctor", "Report root/bin/PATH health and offer to fix PATH.")
    p.add_argument("--yes", "-y", action="store_true", help="auto-append the PATH line to your shell rc")
    args = p.parse_args(argv)
    config.resolve_paths(args.root, args.bin_dir)

    print("toolsync doctor")
    print(f"  root  : {config.TOOLS_DIR}" + ("" if config.TOOLS_DIR.is_dir() else "   (missing!)"))
    print(f"  bin   : {config.BIN_DIR}")
    linked = 0
    if config.BIN_DIR.is_dir():
        for entry in config.BIN_DIR.iterdir():
            try:
                if entry.is_symlink() and config.TOOLS_DIR in entry.resolve().parents:
                    linked += 1
            except OSError:
                pass
    print(f"  links : {linked} command(s) point into the tools dir")
    runners = " ".join(f"{t}={'yes' if shutil.which(t) else 'no'}" for t in ("git", "wine", "mono", "java", "pwsh"))
    print(f"  tools : {runners}")
    ensure_path(assume_yes=args.yes)


# ── self-install ────────────────────────────────────────────────────────────
def cmd_self_install(argv):
    p = _parser("self-install", "Put the `toolsync` command on PATH and check PATH.")
    p.add_argument("--yes", "-y", action="store_true")
    args = p.parse_args(argv)
    config.resolve_paths(args.root, args.bin_dir)
    config.BIN_DIR.mkdir(parents=True, exist_ok=True)

    shim = Path(__file__).resolve().parent.parent / "sync.py"
    report = []
    if not shim.exists():
        report.append(("FAIL", "toolsync", f"launcher shim not found at {shim}"))
        print_report(report)
        return
    chmod_x(shim)
    for nm in ("toolsync", "toolsync.py"):
        link = config.BIN_DIR / nm
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(shim)
            report.append(("OK", nm, f"-> {shim}"))
        except OSError as e:
            report.append(("FAIL", nm, str(e)))
    print_report(report)
    ensure_path(assume_yes=args.yes)


# ── add ─────────────────────────────────────────────────────────────────────
def _is_git_url(s):
    return s.endswith(".git") or s.startswith(("http://", "https://", "git@", "ssh://"))


def _record(dirpath, ok):
    state = load_state()
    state[dirpath.name] = {
        "sig": dir_signature(dirpath) + "|" + config._ALIASES.get(dirpath.name, ""),
        "ok": bool(ok),
        "toolsync_ver": config.TOOLSYNC_VERSION,
    }
    save_state(state)


def _sync_one(dirpath, report):
    """Run the pipeline + link for a single tool directory (mirrors run_sync's body)."""
    result = run_pipeline(dirpath, report)
    ok = result.ok
    if result.entrypoint is not None:
        ok = link_and_verify(result.entrypoint, command_name_for(dirpath), report, dirpath.name)
    _record(dirpath, ok)
    return ok


def _add_one(target, report):
    # 1) git URL -> clone into the root, then sync
    if _is_git_url(target):
        if not shutil.which("git"):
            report.append(("FAIL", target, "git is not installed"))
            return
        name = Path(target.rstrip("/")).name
        if name.endswith(".git"):
            name = name[:-4]
        dest = config.TOOLS_DIR / name
        if dest.exists():
            report.append(("SKIP", name, f"{dest} already exists — syncing it"))
        else:
            config.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["git", "clone", "--depth", "1", target, str(dest)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                tail = (r.stderr or "").strip().splitlines()
                report.append(("FAIL", name, f"git clone failed: {tail[-1] if tail else ''}"))
                return
            report.append(("OK", name, f"cloned -> {dest}"))
        _sync_one(dest, report)
        return

    # 2) local path
    src = Path(target).expanduser()
    if not src.exists():
        report.append(("FAIL", target, "no such file or directory, and not a git URL"))
        return
    src = src.resolve()

    if src.is_dir():
        if config.TOOLS_DIR == src.parent or config.TOOLS_DIR in src.parents:
            _sync_one(src, report)                      # already under the root
            return
        dest = config.TOOLS_DIR / src.name
        if dest.exists():
            report.append(("SKIP", src.name, f"{dest} already exists — syncing it"))
        else:
            config.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dest, symlinks=True)
            report.append(("OK", src.name, f"copied -> {dest}"))
        _sync_one(dest, report)
        return

    # 3) a lone script/binary -> link it in place (name + name.py + lowercase)
    chmod_x(src)
    if src.suffix == ".py":
        fix_shebang(src, "/usr/bin/env python3")
    primary = src.stem if src.suffix == ".py" else src.name
    link_and_verify(src, primary, report, src.name)


def cmd_add(argv):
    p = _parser("add", "Install a tool from a path or git URL into the tools dir, "
                       "or link a lone script in place.")
    p.add_argument("target", nargs="+", help="path to a script / directory, or a git URL")
    args = p.parse_args(argv)
    config.resolve_paths(args.root, args.bin_dir)
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    config.BIN_DIR.mkdir(parents=True, exist_ok=True)
    config._ALIASES = load_aliases()

    report = []
    for target in args.target:
        _add_one(target, report)
    print_report(report, empty_msg="toolsync add: nothing linked.")


# ── locate ──────────────────────────────────────────────────────────────────
def _tokens(s: str):
    """Split into alnum runs, e.g. 'nc64.exe' -> ['nc64', 'exe'] and
    'mssqlinstance.py' -> ['mssqlinstance', 'py']. Matching token-to-token
    (see _matches) instead of on a fully-flattened string is what keeps a
    short query like 'nc' from matching 'mssqlinstaNCe.py' - '.exe' being its
    own token means 'nc' is never a substring of any one token there, whereas
    a flattened-string substring check would see straight through the word
    boundary."""
    return re.findall(r"[a-z0-9]+", s.lower())


def _split_ext(s: str):
    """(stem, lowercased-extension-without-dot) if `s` has a dot, else (s, None)."""
    if "." in s:
        stem, ext = s.rsplit(".", 1)
        return stem, ext.lower()
    return s, None


def _matches(query: str, name: str) -> bool:
    """True if `query`'s name (ignoring any extension) matches `name`'s name
    (ditto), and - only when the query itself specified an extension - that
    extension matches too.

    The stem comparison requires EVERY stem token in `query` to prefix-match
    some stem token in `name` (and vice versa for a single-token query, so
    'nc' -> 'nc64' and a short alias like 'nxc' -> a longer real tool name
    both still work) - see _tokens. Matching is deliberately stem-vs-stem,
    extension-vs-extension, never one against the other: without that split,
    a query like 'CreateCertGUI.exe' would match every unrelated '*.exe' in
    the tree, because its lone 'exe' token trivially prefix-matches the 'exe'
    token on every other .exe file's name - the stem requirement is what
    keeps the match specific to the actual file.
    """
    q_stem, q_ext = _split_ext(query)
    n_stem, n_ext = _split_ext(name)
    # Single-character tokens (an isolated 'Z' in some_file_Z.rule, say) are
    # dropped before matching - keeping them lets a trivial first-letter
    # coincidence ('zzznope'.startswith('z')) pass as a hit on names that
    # have nothing to do with the query.
    qtoks = [t for t in _tokens(q_stem) if len(t) > 1] or _tokens(q_stem)
    ntoks = [t for t in _tokens(n_stem) if len(t) > 1] or _tokens(n_stem)
    if not qtoks or not ntoks:
        return False
    stem_ok = all(any(nt.startswith(qt) or qt.startswith(nt) for nt in ntoks) for qt in qtoks)
    if not stem_ok:
        return False
    if q_ext is None or n_ext is None:
        return True  # query didn't ask for a specific extension (or name has none to check)
    return n_ext.startswith(q_ext) or q_ext.startswith(n_ext)


def _commands_for(dirpath: Path):
    """Every command in config.BIN_DIR that actually runs something inside
    `dirpath` - a plain symlink resolving under it, or a toolsync-written
    wrapper script whose exec line names a path under it. Lets `locate`
    answer "what do I type" even for tools linked under a different name
    (aliases, raw-name variants, multi-binary subtools)."""
    names = []
    if not config.BIN_DIR.is_dir():
        return names
    try:
        real_dir = dirpath.resolve()
    except OSError:
        return names
    for link in sorted(config.BIN_DIR.iterdir()):
        try:
            if link.is_symlink():
                target = link.resolve()
                if target == real_dir or real_dir in target.parents:
                    names.append(link.name)
                continue
            if link.is_file() and existing_link_is_ours(link):
                text = link.read_text(errors="ignore")
                if str(real_dir) in text:
                    names.append(link.name)
        except OSError:
            continue
    return names


# Noisy subtrees never worth descending into when hunting for a nested file -
# VCS/cache/build metadata that can be huge (node_modules, .git) and never
# holds the kind of tool binary/script `locate` is looking for.
_PRUNE_DIRS = {".git", ".svn", ".hg", "__pycache__", "node_modules", ".venv",
              "venv", ".pytest_cache", "egg-info"}

_NESTED_FILE_LIMIT = 25  # cap printed nested-file hits so one broad query can't flood the terminal


def _find_nested_files(query, skip_dirs):
    """Walk the whole tools tree looking for a FILE (not a directory) whose
    name matches `query` - the fallback for "I know the exact filename (e.g.
    nc.exe) but have no idea which tool directory it's buried in", which the
    directory/alias/PATH-command matches above can't answer since they only
    ever look at top-level directory and command names."""
    hits = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(config.TOOLS_DIR):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS
                       and not d.endswith(".egg-info")]
        root = Path(dirpath)
        if root in skip_dirs or any(p in skip_dirs for p in root.parents):
            continue
        for name in filenames:
            if _matches(query, name):
                if len(hits) >= _NESTED_FILE_LIMIT:
                    truncated = True
                    return hits, truncated
                hits.append(root / name)
    return hits, truncated


def _files_inside(dirpath: Path, query: str):
    """Non-recursive: files directly under `dirpath` whose name matches
    `query` more specifically than the directory's own name did - e.g.
    querying 'nc64' against the 'nc64' directory should surface
    nc64/nc64.exe itself, not just the directory it lives in."""
    hits = []
    try:
        for child in sorted(dirpath.iterdir()):
            if child.is_file() and _matches(query, child.name):
                hits.append(child)
    except OSError:
        pass
    return hits


def cmd_locate(argv):
    p = _parser("locate", "Find where a tool lives, by partial match on its "
                          "directory/command/alias name, or by nested filename "
                          "(e.g. nc.exe buried inside some tool's directory).")
    p.add_argument("query", help="tool name, directory name, alias, filename, or partial name")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="show command name + sync status for each hit, not just the path")
    p.add_argument("--no-walk", action="store_true",
                   help="skip the slower recursive nested-file search; only match "
                        "top-level directory/alias/command names")
    args = p.parse_args(argv)
    config.resolve_paths(args.root, args.bin_dir)
    config._ALIASES = load_aliases()
    state = load_state()

    if not config.TOOLS_DIR.is_dir():
        print(f"toolsync locate: tools root {config.TOOLS_DIR} doesn't exist")
        return 1

    q = args.query.strip()
    if not q:
        print("toolsync locate: empty query")
        return 1

    # directories whose own name, or whose alias command name, shares a
    # prefix-matching token with the query (see _matches)
    dir_hits = []
    for entry in sorted(config.TOOLS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        keys = [entry.name]
        alias = config._ALIASES.get(entry.name)
        if alias:
            keys.append(alias)
        if any(_matches(q, k) for k in keys):
            dir_hits.append(entry)

    # commands already on PATH (BIN_DIR) whose name matches, even when the
    # directory name itself doesn't (e.g. a multi-binary tool's individual
    # subtool commands) - reported in addition to, not instead of, dir_hits,
    # but skipped if they just point into a directory already listed there
    # (its `command` line already names them).
    covered_dirs = set()
    for h in dir_hits:
        try:
            covered_dirs.add(h.resolve())
        except OSError:
            pass
    cmd_hits = []
    if config.BIN_DIR.is_dir():
        for link in sorted(config.BIN_DIR.iterdir()):
            if not _matches(q, link.name):
                continue
            try:
                target = link.resolve() if link.is_symlink() else None
            except OSError:
                target = None
            if target is not None and (target in covered_dirs
                                       or any(d in target.parents for d in covered_dirs)):
                continue
            cmd_hits.append(link)

    # nested files: only bother walking the tree if the cheap top-level
    # matches above came up empty - a directory/command hit already answers
    # "where is it / what do I type", so there's no need to pay for a full
    # recursive walk when that's already settled.
    file_hits, file_hits_truncated = [], False
    if not dir_hits and not cmd_hits and not args.no_walk:
        file_hits, file_hits_truncated = _find_nested_files(q, covered_dirs)

    if not dir_hits and not cmd_hits and not file_hits:
        print(f"toolsync locate: no match for '{args.query}' under {config.TOOLS_DIR} or {config.BIN_DIR}")
        print("  tip: try a shorter substring, or check .toolsync/aliases for the command name you expect")
        return 1

    if args.verbose:
        for entry in dir_hits:
            st = state.get(entry.name, {})
            if entry.name in state:
                status = "linked, verified ok" if st.get("ok") else "synced, but not runnable (see --explain)"
            else:
                status = "not yet synced (run toolsync)"
            commands = _commands_for(entry)
            print(f"{entry.name}")
            print(f"  path     {entry}")
            print(f"  command  {', '.join(commands) if commands else '(none on PATH yet)'}")
            print(f"  status   {status}")
            for f in _files_inside(entry, q):
                print(f"  file     {f}")

        for link in cmd_hits:
            try:
                target = link.resolve() if link.is_symlink() else f"wrapper -> {link}"
            except OSError:
                target = "?"
            print(f"{link.name}  (command on PATH)")
            print(f"  -> {target}")

        for f in file_hits:
            runnable = os.access(f, os.X_OK)
            print(f"{f.name}  (file under {f.parent.relative_to(config.TOOLS_DIR)})")
            print(f"  path     {f}")
            print(f"  command  {'(not executable / not a runnable tool)' if not runnable else '(none on PATH yet)'}")
        if file_hits_truncated:
            print(f"  ... more than {_NESTED_FILE_LIMIT} files matched - narrow your query to see the rest")
        return 0

    # clean/default output: just the path(s), one per line, most specific
    # first - a directory hit is followed by any file inside it that matches
    # even more specifically (e.g. 'nc64' -> both the nc64/ dir and nc64.exe
    # inside it), so the exact file you want is right there instead of
    # making you go look.
    seen = set()
    for entry in dir_hits:
        inner = _files_inside(entry, q)
        for p_ in (inner if inner else [entry]):
            if str(p_) not in seen:
                seen.add(str(p_))
                print(p_)
    for link in cmd_hits:
        try:
            target = link.resolve() if link.is_symlink() else link
        except OSError:
            target = link
        if str(target) not in seen:
            seen.add(str(target))
            print(target)
    for f in file_hits:
        if str(f) not in seen:
            seen.add(str(f))
            print(f)
    if file_hits_truncated:
        print(f"... more than {_NESTED_FILE_LIMIT} files matched - narrow your query to see the rest", file=sys.stderr)

    return 0
