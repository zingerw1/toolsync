"""Argument parsing and the top-level sync orchestration."""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import config
from .config import (SKIP_NAMES, SKIP_SUFFIXES, TOOLSYNC_VERSION)
from .fsutil import dir_signature
from .handlers import process_loose_file, run_pipeline
from .linking import command_name_for, link_and_verify
from .report import report_duplicate_dirnames
from .state import load_aliases, load_ignore, load_state, save_state


def maybe_install_apt_pkgs(pkgs: set, report, assume_yes: bool):
    if not pkgs:
        return
    pkgs = sorted(pkgs)
    print(f"\ntoolsync: missing system build dependencies detected: {' '.join(pkgs)}")
    if not shutil.which("apt-get"):
        print(f"  install them with your distro's package manager, then re-run toolsync.")
        return
    if assume_yes:
        answer = "y"
    else:
        try:
            answer = input(f"  run: sudo apt-get install -y {' '.join(pkgs)} ?  [y/N] ").strip().lower()
        except EOFError:
            # no TTY attached (e.g. run non-interactively) - never assume
            # consent for a sudo command just because we couldn't ask.
            answer = "n"
    if answer != "y":
        print(f"  skipped. install manually with: sudo apt-get install -y {' '.join(pkgs)}")
        return
    r = subprocess.run(["sudo", "apt-get", "install", "-y", *pkgs])
    if r.returncode != 0:
        print("  apt-get install failed - see output above.")
    else:
        print("  installed. re-run toolsync (or toolsync --force-all) to retry the affected tools.")


def git_pull(dirpath: Path, report) -> bool:
    """Returns True if repo is now at a state worth re-scanning."""
    if not (dirpath / ".git").exists():
        return False
    status = subprocess.run(["git", "status", "--porcelain"], cwd=str(dirpath), capture_output=True, text=True)
    if status.stdout.strip():
        report.append(("SKIP", dirpath.name, "local changes present - not pulling (commit/stash first, or use `git pull` yourself)"))
        return False
    r = subprocess.run(["git", "pull", "--ff-only"], cwd=str(dirpath), capture_output=True, text=True)
    if r.returncode != 0:
        report.append(("WARN", dirpath.name, f"git pull failed: {r.stderr.strip().splitlines()[-1] if r.stderr else ''}"))
        return False
    if "Already up to date" in (r.stdout or ""):
        return False
    report.append(("OK", dirpath.name, "pulled latest from git"))
    return True


SUBCOMMANDS = ("add", "self-install", "doctor")


def main():
    """Dispatch to a subcommand, or run the default directory sync."""
    argv = sys.argv[1:]
    if argv and argv[0] in SUBCOMMANDS:
        from . import universal
        return universal.dispatch(argv[0], argv[1:])
    return run_sync()


def run_sync():
    parser = argparse.ArgumentParser(
        prog="toolsync",
        description="Scan a tools directory and wire every tool onto PATH. "
                    "Subcommands: add, self-install, doctor.")
    parser.add_argument("--update", action="store_true", help="git pull clean repos before syncing")
    parser.add_argument("--force", metavar="NAME", action="append", default=[], help="re-run install+verify for this tool even if unchanged (repeatable)")
    parser.add_argument("--force-all", action="store_true", help="re-run install+verify for every tool")
    parser.add_argument("--explain", metavar="NAME", help="print the last recorded notes for this tool and exit")
    parser.add_argument("--yes", "-y", action="store_true", help="auto-confirm apt-get installs for missing build deps")
    parser.add_argument("--root", metavar="DIR", help="tools directory to scan (default: ~/tools, or $TOOLSYNC_ROOT / config file)")
    parser.add_argument("--bin", metavar="DIR", dest="bin_dir", help="where to put the PATH links (default: ~/.local/bin, or $TOOLSYNC_BIN)")
    args = parser.parse_args()

    config.resolve_paths(args.root, args.bin_dir)
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)  # fresh root may not have it yet

    ignore = load_ignore()
    config._ALIASES = load_aliases()
    state = load_state()

    if args.explain:
        entry = state.get(args.explain)
        if not entry:
            print(f"no recorded state for '{args.explain}'")
        else:
            print(json.dumps(entry, indent=2))
        return

    report = []
    report_duplicate_dirnames(config.TOOLS_DIR, ignore, report)

    force_names = set(args.force)
    all_missing_apt_pkgs = set()

    for entry in sorted(config.TOOLS_DIR.iterdir()):
        name = entry.name
        if name.startswith(".") or name in ignore:
            continue

        if entry.is_file() or entry.is_symlink():
            if entry.suffix in SKIP_SUFFIXES or name in SKIP_NAMES:
                continue
            process_loose_file(entry, report)
            continue

        if not entry.is_dir():
            continue

        alias_tag = config._ALIASES.get(name, "")
        try:
            if not os.access(entry, os.R_OK | os.X_OK):
                continue

            if args.update:
                git_pull(entry, report)

            sig = dir_signature(entry) + "|" + alias_tag
            prev = state.get(name)
            force = args.force_all or name in force_names
            stale_version = prev is not None and prev.get("toolsync_ver", 0) < TOOLSYNC_VERSION
            if prev and prev.get("sig") == sig and not force and not stale_version:
                continue  # unchanged since last successful sync, and toolsync logic hasn't moved on

            result = run_pipeline(entry, report)
            all_missing_apt_pkgs |= result.missing_apt_pkgs
            ok = result.ok

            if result.entrypoint is not None:
                name_for = command_name_for(entry)
                ok = link_and_verify(result.entrypoint, name_for, report, name)
        except PermissionError:
            continue
        except subprocess.CalledProcessError as e:
            report.append(("FAIL", name, f"setup failed: {e}"))
            ok = False
        except Exception as e:
            # A single tool's installer misbehaving (crash, unexpected
            # exception from a build tool, etc.) must never take down the
            # whole sync - report it and keep going to the next tool.
            report.append(("FAIL", name, f"unexpected error: {type(e).__name__}: {e}"))
            ok = False

        # Re-snapshot *after* setup: installers (setup.py/pip/make/cargo) can
        # leave build artifacts with fresh mtimes in-tree, so the stored
        # signature must reflect the settled post-install state, or every
        # run looks "changed".
        state[name] = {
            "sig": dir_signature(entry) + "|" + alias_tag,
            "ok": bool(ok),
            "toolsync_ver": TOOLSYNC_VERSION,
        }

    save_state(state)
    maybe_install_apt_pkgs(all_missing_apt_pkgs, report, assume_yes=args.yes)

    if not report:
        print("toolsync: everything already up to date.")
        return

    tags = {"OK": "OK   ", "SKIP": "SKIP ", "WARN": "WARN ", "CONFLICT": "CONFL", "FAIL": "FAIL "}
    width = max(len(r[1]) for r in report)
    for level, name, msg in report:
        print(f"[{tags[level]}] {name.ljust(width)}  {msg}")

    counts = {k: 0 for k in tags}
    for level, _, _ in report:
        counts[level] += 1
    summary = " · ".join(f"{k} {v}" for k, v in counts.items() if v)
    print(f"\n{summary}")
