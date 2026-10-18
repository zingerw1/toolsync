"""Universal install helpers layered on the directory-scan core.

Subcommands:
  add <path | git-url> ...   install a tool from anywhere (clone / copy into the
                             root, or link a lone script in place)
  self-install               put the `toolsync` command on PATH, check PATH
  doctor                     report root/bin/PATH health and offer to fix PATH
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import config
from .fsutil import chmod_x, dir_signature, fix_shebang
from .handlers import run_pipeline
from .linking import command_name_for, link_and_verify
from .report import print_report
from .state import load_aliases, load_state, save_state


def dispatch(cmd, argv):
    return {"add": cmd_add, "self-install": cmd_self_install, "doctor": cmd_doctor}[cmd](argv)


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
