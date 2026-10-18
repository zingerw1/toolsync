"""Per-directory install handlers and the detection pipeline."""
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

from . import config

from .config import (ARCHIVE_SUFFIXES, SKIP_NAMES, SKIP_SUFFIXES)
from .detect import (_candidate_top_files, find_binary_entrypoint,
                     find_foreign_entrypoint, find_python_entrypoints)
from .fsutil import chmod_x, fix_shebang, has_php_shebang
from .linking import (command_name_for, existing_link_is_ours, link_and_verify,
                      link_case_aliases, link_entrypoint_variants,
                      symlink_into_bin, write_runner_wrapper)
from .runners import FOREIGN_RUNNERS, FOREIGN_SUFFIXES
from .state import InstallResult
from .verify import detect_missing_apt_pkgs, verify_runs


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------

# Directories that failed in a way a re-run of the *same* handler can't fix
# (corrupt download, missing extractor) - remembered per-process so the
# bounded re-detect loop in run_pipeline doesn't hammer out the same FAIL
# line up to 4 times in one run.
_ARCHIVE_GIVE_UP = set()


def handle_archive(dirpath: Path, report) -> InstallResult | None:
    if dirpath in _ARCHIVE_GIVE_UP:
        return None
    files = [p for p in dirpath.iterdir() if p.is_file()]
    archives = [p for p in files if "".join(p.suffixes[-2:]) in ARCHIVE_SUFFIXES or p.suffix in ARCHIVE_SUFFIXES]
    non_archive_substance = [
        p for p in dirpath.iterdir()
        if p not in archives and not p.name.startswith(".") and p.name not in SKIP_NAMES
    ]
    if len(archives) != 1 or non_archive_substance:
        return None
    archive = archives[0]
    suffix = archive.suffix.lower()

    def give_up(msg, level="FAIL"):
        _ARCHIVE_GIVE_UP.add(dirpath)
        report.append((level, dirpath.name, msg))
        return InstallResult(ok=False)

    if archive.stat().st_size < 100:
        return give_up(f"{archive.name} is only {archive.stat().st_size} bytes - looks like a placeholder/failed download, not a real archive. Re-download it, or remove it and add '{dirpath.name}' to .toolsync/ignore")
    try:
        if suffix == ".zip":
            with zipfile.ZipFile(archive) as z:
                z.extractall(dirpath)
        elif suffix in (".tar", ".tgz", ".gz", ".bz2", ".tbz2") or archive.name.endswith((".tar.gz", ".tar.bz2")):
            with tarfile.open(archive) as t:
                t.extractall(dirpath, filter="data")
        elif suffix == ".7z":
            exe = shutil.which("7z") or shutil.which("7zz")
            if not exe:
                return give_up(f"archive {archive.name} needs '7z' (apt install p7zip-full) - not installed", level="SKIP")
            r = subprocess.run([exe, "x", f"-o{dirpath}", "-y", str(archive)], capture_output=True, text=True)
            if r.returncode != 0:
                return give_up(f"7z extraction failed: {r.stderr.strip().splitlines()[-1] if r.stderr else ''}")
        else:
            return None
    except Exception as e:
        return give_up(f"failed to extract {archive.name}: {e}")
    report.append(("OK", dirpath.name, f"extracted {archive.name}"))
    return InstallResult(ok=True, note="extracted")  # caller re-detects


NEVER_A_NESTED_CLONE = {"venv", ".venv", "env", "node_modules", "target", "build", "dist", "__pycache__"}


def handle_nested_clone(dirpath: Path, report) -> InstallResult | None:
    entries = [p for p in dirpath.iterdir() if not p.name.startswith(".")]
    if any(p.is_dir() and p.name in NEVER_A_NESTED_CLONE for p in entries):
        # A bare `venv/` sitting alone at the top of a tool's directory (no
        # requirements.txt alongside it, because the source was deleted or
        # it was pip-installed directly) must never be treated as "the real
        # project nested one level down" - venv/bin/<name> console scripts
        # have an absolute shebang hardcoded to *this exact path*, and
        # moving the venv breaks every one of them.
        return None
    subdirs = [p for p in entries if p.is_dir()]
    others = [p for p in entries if not p.is_dir()]
    # Tolerate a leftover archive sitting next to the already-extracted repo
    # (e.g. `rusthound/v1.1.69.zip` alongside `rusthound/RustHound-1.1.69/`
    # after a manual unzip) - it doesn't block flattening the real subdir.
    if len(subdirs) != 1 or any(
        "".join(p.suffixes[-2:]) not in ARCHIVE_SUFFIXES and p.suffix not in ARCHIVE_SUFFIXES
        for p in others
    ):
        return None
    inner = subdirs[0]
    looks_like_repo = (
        (inner / ".git").exists()
        or (inner / "requirements.txt").exists()
        or (inner / "pyproject.toml").exists()
        or (inner / "setup.py").exists()
        or (inner / "Cargo.toml").exists()
        or (inner / "go.mod").exists()
        or (inner / "Makefile").exists()
        or find_binary_entrypoint(inner) is not None
    )
    if not looks_like_repo:
        return None
    # Move inner out of the way first under a temp name - moving its
    # contents up in place can otherwise collide when inner contains an
    # item whose name equals inner's own name (e.g. phpggc/phpggc/phpggc),
    # where `dirpath / item.name` resolves to inner's own (about-to-be-removed)
    # path and silently drops that item.
    staging = dirpath / f".toolsync-flatten-{inner.name}"
    try:
        shutil.rmtree(staging, ignore_errors=True)
        inner.rename(staging)
        for item in list(staging.iterdir()) + list(staging.glob(".*")):
            dest = dirpath / item.name
            if dest.exists():
                continue
            shutil.move(str(item), str(dest))
        staging.rmdir()
    except OSError as e:
        report.append(("WARN", dirpath.name, f"detected nested clone ({inner.name}/) but couldn't flatten it automatically: {e}"))
        return InstallResult(ok=False)
    report.append(("OK", dirpath.name, f"flattened nested clone ({inner.name}/ -> {dirpath.name}/)"))
    return InstallResult(ok=True, note="flattened")  # caller re-detects


def _run_build(cmd, cwd, dirpath, report, what) -> bool:
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=1800)
    except FileNotFoundError:
        report.append(("SKIP", dirpath.name, f"{what} not available - skipping build"))
        return False
    except subprocess.TimeoutExpired:
        report.append(("FAIL", dirpath.name, f"{what} build timed out"))
        return False
    if r.returncode != 0:
        tail = _error_tail(r.stderr) or _error_tail(r.stdout)
        report.append(("FAIL", dirpath.name, f"{what} build failed: {tail}"))
        return False
    return True


def _error_tail(text: str) -> str:
    """Pick the most useful line out of a build tool's output: prefer the
    last line that actually says 'error' over the last line overall, which
    is very often just a trailing 'warning: build failed, waiting for other
    jobs to finish...' (cargo) or similar noise that hides the real cause."""
    if not text:
        return ""
    lines = [l for l in text.strip().splitlines() if l.strip()]
    if not lines:
        return ""
    error_lines = [l for l in lines if re.search(r"\berror\b", l, re.IGNORECASE)]
    return (error_lines[-1] if error_lines else lines[-1]).strip()


def handle_cargo(dirpath: Path, report) -> InstallResult | None:
    manifest = dirpath / "Cargo.toml"
    if not manifest.exists():
        return None
    if not shutil.which("cargo"):
        report.append(("SKIP", dirpath.name, "Cargo.toml found but 'cargo' is not installed (apt install cargo / rustup)"))
        return InstallResult(ok=False)
    bin_name = dirpath.name
    try:
        for line in manifest.read_text().splitlines():
            m = re.match(r'^\s*name\s*=\s*"([^"]+)"', line)
            if m:
                bin_name = m.group(1)
                break
    except OSError:
        pass
    if not _run_build(["cargo", "build", "--release"], dirpath, dirpath, report, "cargo"):
        # A failed cargo build can still leave a huge target/ behind
        # (downloaded + partially-compiled dependencies - commonly several
        # hundred MB to 1GB+). Since the build didn't produce anything
        # usable anyway, and toolsync will retry it on every future run
        # until the real compile error is fixed, don't let each retry eat
        # disk space for nothing.
        shutil.rmtree(dirpath / "target", ignore_errors=True)
        return InstallResult(ok=False)
    built = dirpath / "target" / "release" / bin_name
    if not built.is_file():
        candidates = [p for p in (dirpath / "target" / "release").glob("*") if p.is_file() and os.access(p, os.X_OK)]
        built = candidates[0] if len(candidates) == 1 else None
    if not built:
        report.append(("WARN", dirpath.name, "cargo build succeeded but couldn't find the built binary in target/release/"))
        return InstallResult(ok=False)
    return InstallResult(ok=True, entrypoint=built)


def handle_go(dirpath: Path, report) -> InstallResult | None:
    if not (dirpath / "go.mod").exists():
        return None
    if not shutil.which("go"):
        report.append(("SKIP", dirpath.name, "go.mod found but 'go' is not installed"))
        return InstallResult(ok=False)
    out = dirpath / dirpath.name
    if not _run_build(["go", "build", "-o", str(out), "."], dirpath, dirpath, report, "go"):
        return InstallResult(ok=False)
    if not out.is_file():
        report.append(("WARN", dirpath.name, "go build succeeded but output binary wasn't where expected"))
        return InstallResult(ok=False)
    return InstallResult(ok=True, entrypoint=out)


def handle_make(dirpath: Path, report) -> InstallResult | None:
    # Build dir: top-level, or one level down in `src/` - the classic
    # autotools layout (john-jumbo ships its ./configure && make in src/,
    # with the top level only holding docs/run/).
    build_dir = dirpath
    if not (dirpath / "Makefile").exists() and not (dirpath / "configure").exists():
        src = dirpath / "src"
        if (src / "Makefile").exists() or (src / "configure").exists():
            build_dir = src
        else:
            return None
    makefile = build_dir / "Makefile"
    configure = build_dir / "configure"
    if not shutil.which("make"):
        report.append(("SKIP", dirpath.name, "Makefile found but 'make' is not installed"))
        return InstallResult(ok=False)

    before = {p for p in dirpath.rglob("*") if p.is_file() and os.access(p, os.X_OK)}

    if configure.exists() and not makefile.exists():
        if not _run_build(["./configure"], build_dir, dirpath, report, "configure"):
            return InstallResult(ok=False)
    if (build_dir / "Makefile").exists():
        if not _run_build(["make", "-j", str(os.cpu_count() or 2)], build_dir, dirpath, report, "make"):
            return InstallResult(ok=False)

    after = {p for p in dirpath.rglob("*") if p.is_file() and os.access(p, os.X_OK)}
    new_execs = [
        p for p in (after - before)
        if p.suffix not in SKIP_SUFFIXES and ".git" not in p.parts
    ]
    name_variants = {dirpath.name, dirpath.name.lower()}
    exact = [p for p in new_execs if p.name in name_variants or p.stem in name_variants]
    if exact:
        return InstallResult(ok=True, entrypoint=exact[0])
    if len(new_execs) == 1:
        return InstallResult(ok=True, entrypoint=new_execs[0])
    report.append(("WARN", dirpath.name, f"build succeeded but produced {len(new_execs)} new executables - not guessing which is the entrypoint; link one yourself"))
    return InstallResult(ok=False)


def venv_usable(venv_dir: Path) -> bool:
    """A venv is only as good as its interpreter AND its pip actually being
    invocable - directory existing is not enough. Two independent ways a
    venv goes stale, both seen in the wild here:
      - bin/python3 is a symlink to a base-python path that's since moved
        (e.g. after a manual directory rename/flatten)
      - bin/pip's *own* shebang hardcodes an absolute path to this venv,
        which breaks the same way if the venv's directory ever moves - this
        is why pip is checked via `python3 -m pip`, never by exec'ing the
        pip script directly, everywhere in this file."""
    python3 = venv_dir / "bin" / "python3"
    if not python3.exists():
        return False
    try:
        r = subprocess.run([str(python3), "-m", "pip", "--version"], capture_output=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def setup_python_project(dirpath: Path, report) -> InstallResult:
    venv_dir = dirpath / "venv"
    req = dirpath / "requirements.txt"
    pyproject = dirpath / "pyproject.toml"
    setup_py = dirpath / "setup.py"
    has_package = pyproject.exists() or setup_py.exists()

    created = False
    if venv_dir.exists() and not venv_usable(venv_dir):
        # A venv can go stale (e.g. its interpreter path vanished after a
        # manual directory rename/flatten, or its base python was removed).
        # Reusing a broken venv as-is just produces a confusing
        # FileNotFoundError pointing at venv/bin/pip - rebuild it instead.
        report.append(("WARN", dirpath.name, "existing venv/ is broken (stale interpreter path) - rebuilding it"))
        shutil.rmtree(venv_dir, ignore_errors=True)
    if not venv_dir.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
        created = True
    # Always invoke pip via `python3 -m pip`, never by exec'ing the `pip`
    # script directly - that script's own shebang hardcodes an absolute
    # path back into this venv, which silently breaks (FileNotFoundError on
    # a file that very much exists) if the venv's directory is ever moved
    # or renamed (manual flatten, repo reorg, etc). `-m pip` has no such
    # fragile path baked in.
    pip_cmd = [str(venv_dir / "bin" / "python3"), "-m", "pip"]
    venv_bin = venv_dir / "bin"
    missing_apt_pkgs = set()

    if created or req.exists() or has_package:
        subprocess.run([*pip_cmd, "install", "--upgrade", "pip", "-q"], check=False)
    if req.exists():
        r = subprocess.run([*pip_cmd, "install", "-q", "-r", str(req)], capture_output=True, text=True)
        if r.returncode != 0:
            missing_apt_pkgs |= detect_missing_apt_pkgs(r.stderr)
            report.append(("WARN", dirpath.name, f"some requirements.txt deps failed to install: {r.stderr.strip().splitlines()[-1] if r.stderr else ''}"))
    if has_package:
        r = subprocess.run([*pip_cmd, "install", "-q", "."], cwd=str(dirpath), capture_output=True, text=True)
        if r.returncode != 0 and pyproject.exists():
            # not every pyproject.toml is an installable package (e.g. "package = false"
            # script projects that just list deps) - fall back to installing its
            # [project.dependencies] directly.
            try:
                import tomllib
                deps = tomllib.loads(pyproject.read_text()).get("project", {}).get("dependencies", [])
            except Exception:
                deps = []
            if deps:
                r2 = subprocess.run([*pip_cmd, "install", "-q", *deps], capture_output=True, text=True)
                if r2.returncode != 0:
                    missing_apt_pkgs |= detect_missing_apt_pkgs(r2.stderr)
                    report.append(("WARN", dirpath.name, f"failed installing pyproject.toml dependencies: {r2.stderr.strip().splitlines()[-1] if r2.stderr else ''}"))
            elif not req.exists():
                missing_apt_pkgs |= detect_missing_apt_pkgs(r.stderr)
                report.append(("WARN", dirpath.name, f"pip install . failed and no [project.dependencies] to fall back on: {r.stderr.strip().splitlines()[-1] if r.stderr else ''}"))
        elif r.returncode != 0 and not req.exists():
            missing_apt_pkgs |= detect_missing_apt_pkgs(r.stderr)
            report.append(("WARN", dirpath.name, f"pip install . (setup.py) failed: {r.stderr.strip().splitlines()[-1] if r.stderr else ''}"))

    # NB: don't .resolve() this - on many distros venv/bin/python3 is itself
    # a symlink to the system interpreter, and resolving it walks straight
    # through the venv to that system python, silently dropping the venv's
    # installed packages. The unresolved venv/bin/python3 path is what makes
    # the interpreter pick up site-packages from *this* venv.
    venv_python = str(venv_dir / "bin" / "python3")

    # Prefer a console-script pip installed into venv/bin - the correct
    # entrypoint for any properly packaged (setup.py/pyproject) tool.
    venv_infra = {"pip", "pip3", "python", "python3", "activate", "Activate.ps1"}
    candidate_bins = {
        p.name for p in (venv_bin.iterdir() if venv_bin.is_dir() else [])
        if p.name not in venv_infra and not p.name.startswith("activate")
        and not re.match(r"^pip3?\.\d", p.name) and not re.match(r"^python3\.\d", p.name)
    }
    norm = lambda s: re.sub(r"[-_. ]", "", s).lower()
    dir_norm = norm(dirpath.name)
    console_entry = None
    exact = [b for b in candidate_bins if norm(b) == dir_norm]
    if exact:
        console_entry = venv_bin / exact[0]

    if console_entry:
        return InstallResult(ok=True, entrypoint=console_entry, note="installed console script", missing_apt_pkgs=missing_apt_pkgs)

    entrypoints = find_python_entrypoints(dirpath)
    if not entrypoints:
        # No single named entrypoint, but a packaged project can still ship
        # several genuinely distinct CLI scripts at the top level (e.g.
        # PKINITtools: gettgtpkinit.py / getnthash.py / gets4uticket.py) -
        # link each under its own name through the venv python, the same
        # principle handle_bare_python_script / handle_multi_binary use. This
        # keeps the handler type-driven rather than giving up on a whole shape.
        scripts = [
            p for p in dirpath.glob("*.py")
            if p.is_file() and p.name not in {"setup.py", "conftest.py", "__init__.py"}
        ]
        if 2 <= len(scripts) <= 12:
            linked_any = False
            for ep in scripts:
                fix_shebang(ep, venv_python)
                if ep.read_bytes()[:2] != b"#!":
                    continue
                chmod_x(ep)
                if link_and_verify(ep, ep.stem, report, dirpath.name):
                    linked_any = True
            if linked_any:
                return InstallResult(ok=True, entrypoint=None, missing_apt_pkgs=missing_apt_pkgs)
        report.append(("SKIP", dirpath.name, "python project detected but no clear entrypoint (.py matching dir name / main.py) - add one or symlink it yourself"))
        return InstallResult(ok=False, missing_apt_pkgs=missing_apt_pkgs)

    linked_any = None
    for ep in entrypoints:
        fix_shebang(ep, venv_python)
        chmod_x(ep)
        name = command_name_for(dirpath) if ep.stem.lower() in {dirpath.name.lower(), "main"} else command_name_for(ep.with_suffix(""))
        if link_entrypoint_variants(ep, name, report):
            ok, tail = verify_runs(ep)
            if ok:
                report.append(("OK", dirpath.name, f"linked '{name}' -> venv python3 + {ep.relative_to(dirpath)}"))
            else:
                report.append(("WARN", dirpath.name, f"linked '{name}' but it fails to run: {tail}"))
            linked_any = linked_any or ok
    return InstallResult(ok=bool(linked_any), entrypoint=None, missing_apt_pkgs=missing_apt_pkgs)


def handle_bare_venv(dirpath: Path, report) -> InstallResult | None:
    """A venv/ with no package manifest beside it (requirements.txt /
    pyproject.toml / setup.py all gone - the source was removed after a direct
    `pip install <tool>` into the venv, or the repo was trimmed to just its
    venv). The real console script still sits in venv/bin; link it. Keyed on
    the venv layout, not on any specific tool."""
    if (dirpath / "requirements.txt").exists() or (dirpath / "pyproject.toml").exists() or (dirpath / "setup.py").exists():
        return None  # a real project - handle_python owns it
    venv_bin = dirpath / "venv" / "bin"
    if not venv_bin.is_dir() or not venv_usable(dirpath / "venv"):
        return None
    venv_infra = {"pip", "pip3", "python", "python3", "activate", "Activate.ps1",
                  "activate.csh", "activate.fish", "wheel", "easy_install"}
    candidates = [
        p for p in venv_bin.iterdir()
        if p.is_file() and p.name not in venv_infra and not p.name.startswith("activate")
        and not re.match(r"^(pip3?|python3?|easy_install)[-.]?\d", p.name)
    ]
    if not candidates:
        return None
    norm = lambda s: re.sub(r"[-_. ]", "", s).lower()
    name_match = [p for p in candidates if norm(p.name) == norm(dirpath.name)]
    if name_match:
        return InstallResult(ok=True, entrypoint=name_match[0], note="linked venv console script (no manifest)")
    if len(candidates) == 1:
        return InstallResult(ok=True, entrypoint=candidates[0], note="linked venv console script (no manifest)")
    # Several console scripts and none matches the dir name (some are deps'
    # helper scripts). Link each under its own name rather than guess one.
    linked_any = False
    for ep in candidates:
        if link_and_verify(ep, ep.name, report, dirpath.name):
            linked_any = True
    if linked_any:
        return InstallResult(ok=True, entrypoint=None, note="linked venv console scripts (no manifest)")
    return None


def handle_python(dirpath: Path, report) -> InstallResult | None:
    if not ((dirpath / "requirements.txt").exists() or (dirpath / "pyproject.toml").exists() or (dirpath / "setup.py").exists()):
        return None
    return setup_python_project(dirpath, report)


def handle_bare_python_script(dirpath: Path, report) -> InstallResult | None:
    """Top-level .py file(s) with no package manifest at all (no
    requirements.txt/pyproject.toml/setup.py) - the common 'one or a few
    loose scripts, no packaging' shape. Covers two cases:
      - a single script whose name has nothing to do with the repo name
        (e.g. DNSTool's actual script is dnslookup.py, not dnstool.py) ->
        linked under the repo's name
      - several genuinely distinct scripts in one repo (e.g. krbrelayx.py
        ships addspn.py, dnstool.py, krbrelayx.py, printerbug.py as four
        separate tools, not one tool with four candidate names) -> each
        linked under its own name, the same principle as
        handle_multi_binary for compiled binaries
    Deps: if the directory ships its own venv/ (hand-made with `pip install`
    into it, even though there's no manifest to drive it - e.g. krbrelayx),
    point each script's shebang at that venv's python so it reliably picks up
    venv-only packages; otherwise fall back to the system python3. Without
    this a script needing, say, impacket from the venv would silently run
    under system python and either break (ModuleNotFoundError) or work only by
    luck when the same package also happens to be installed system-wide."""
    if (dirpath / "requirements.txt").exists() or (dirpath / "pyproject.toml").exists() or (dirpath / "setup.py").exists():
        return None  # handled by handle_python instead
    py_files = [
        p for p in dirpath.glob("*.py")
        if p.is_file() and p.name not in {"setup.py", "conftest.py", "__init__.py"}
    ]
    if not py_files:
        return None
    if len(py_files) > 12:
        return None  # too many to be "a few distinct tools" - leave to handle_suite

    # Prefer the directory's own venv interpreter when it has a usable one, so
    # venv-installed deps are always on the path for these scripts.
    venv_dir = dirpath / "venv"
    interpreter = str(venv_dir / "bin" / "python3") if venv_usable(venv_dir) else "/usr/bin/env python3"

    def make_runnable(ep: Path) -> bool:
        fix_shebang(ep, interpreter)
        if ep.read_bytes()[:2] != b"#!":
            # No shebang and fix_shebang's safety guard declined to add one
            # (shouldn't happen for real text, but never force it).
            return False
        chmod_x(ep)
        return True

    if len(py_files) == 1:
        ep = py_files[0]
        if not make_runnable(ep):
            return None
        return InstallResult(ok=True, entrypoint=ep)

    linked_any = False
    for ep in py_files:
        if not make_runnable(ep):
            continue
        if link_and_verify(ep, ep.stem, report, dirpath.name):
            linked_any = True
    return InstallResult(ok=linked_any, entrypoint=None)


def handle_php(dirpath: Path, report) -> InstallResult | None:
    ep = find_binary_entrypoint(dirpath)
    if not ep or not has_php_shebang(ep):
        return None
    chmod_x(ep)
    return InstallResult(ok=True, entrypoint=ep)


def handle_binary(dirpath: Path, report) -> InstallResult | None:
    ep = find_binary_entrypoint(dirpath)
    if not ep:
        return None
    chmod_x(ep)
    return InstallResult(ok=True, entrypoint=ep)


def handle_multi_binary(dirpath: Path, report) -> InstallResult | None:
    """A handful of top-level executables that are each their own distinct
    tool, not one tool with a guessable primary name (e.g. a folder holding
    both ligolo-ng-agent and ligolo-ng-proxy). find_binary_entrypoint
    correctly refuses to pick one of these - this handler links each one
    under its own name instead of skipping the whole directory."""
    executables = [p for p in _candidate_top_files(dirpath) if os.access(p, os.X_OK)]
    if not (2 <= len(executables) <= 12):
        return None
    linked_any = False
    for ep in executables:
        chmod_x(ep)
        if link_and_verify(ep, ep.name, report, dirpath.name):
            linked_any = True
    return InstallResult(ok=linked_any, entrypoint=None)


def _link_foreign(ep: Path, suffix: str, primary_name: str, report, label: str) -> bool:
    """Put a foreign artifact on PATH. If an interpreter for its type is
    installed, write a wrapper that execs it through that interpreter (and
    verify it actually runs); otherwise link it as-is so it's at least
    locatable on PATH (useful for a Windows payload you transfer elsewhere) -
    reported OK with a clear note, not WARN, because nothing is broken."""
    runner = FOREIGN_RUNNERS[suffix](ep)
    raw_name = ep.name
    if runner is None:
        # No interpreter for this type here - link it so `which`/PATH find it.
        made = symlink_into_bin(ep, primary_name, report)
        if raw_name != primary_name:
            symlink_into_bin(ep, raw_name, report)
        if made:
            link_case_aliases([primary_name, raw_name], report)
            runner_names = {".exe": "wine/mono", ".jar": "java", ".ps1": "pwsh"}.get(suffix, "its interpreter")
            report.append(("OK", label, f"linked '{primary_name}' -> {ep} (no {runner_names} installed - linked so it's locatable on PATH, not executed here)"))
        return made
    link = config.BIN_DIR / primary_name
    if link.exists() or link.is_symlink():
        if not existing_link_is_ours(link):
            report.append(("CONFLICT", primary_name, f"~/.local/bin/{primary_name} already points outside ~/tools - not touching it"))
            return False
    config.BIN_DIR.mkdir(parents=True, exist_ok=True)
    if not write_runner_wrapper(ep, link, runner):
        report.append(("WARN", label, f"couldn't write wrapper for '{primary_name}'"))
        return False
    if raw_name != primary_name:
        raw_link = config.BIN_DIR / raw_name
        if not (raw_link.exists() and not existing_link_is_ours(raw_link)):
            write_runner_wrapper(ep, raw_link, runner)
    link_case_aliases([primary_name, raw_name], report)
    ok, tail = verify_runs(primary_name, extra_env={"PATH": f"{config.BIN_DIR}:{os.environ.get('PATH', '')}"})
    runner_desc = " ".join(runner)
    if ok:
        report.append(("OK", label, f"linked '{primary_name}' via {runner_desc} wrapper -> {ep}"))
    else:
        report.append(("WARN", label, f"linked '{primary_name}' via {runner_desc} but it fails to run: {tail}"))
    return ok


def handle_foreign(dirpath: Path, report) -> InstallResult | None:
    """Foreign (non-ELF, non-shell-script) artifacts: Windows .exe, Java .jar,
    PowerShell .ps1, etc. The extension->interpreter mapping lives in
    FOREIGN_RUNNERS, which is the one place to extend for a new file type.
    Finds the entrypoint by file type + name (never by a hard-coded tool
    name)."""
    found = find_foreign_entrypoint(dirpath)
    if not found:
        return None
    ep, suffix = found
    primary = command_name_for(dirpath)
    ok = _link_foreign(ep, suffix, primary, report, dirpath.name)
    return InstallResult(ok=ok, entrypoint=None)


def handle_suite(dirpath: Path, report) -> InstallResult:
    """Dirs with many same-pattern scripts but no single entrypoint
    (e.g. john-jumbo/run/*2john.py, or a PowerShell module collection like
    PowerSploit). Report richly instead of a generic SKIP."""
    try:
        has_anything = any(not p.name.startswith(".") for p in dirpath.iterdir())
    except OSError:
        has_anything = True
    if not has_anything:
        report.append(("SKIP", dirpath.name, "directory is empty - nothing to link (remove it, or add it to .toolsync/ignore)"))
        return InstallResult(ok=False)
    script_exts = {".py", ".pl", ".sh", ".rb", ".ps1", ".psm1"}
    scripts = [
        p for depth in (dirpath, *(d for d in dirpath.iterdir() if d.is_dir()))
        for p in (depth.iterdir() if depth.is_dir() else [])
        if p.is_file() and p.suffix in script_exts
    ]
    if len(scripts) >= 5:
        report.append(("SKIP", dirpath.name, f"{len(scripts)} scripts found, no single entrypoint - this looks like a multi-script suite; run them directly from {dirpath}, or symlink/dot-source the ones you need"))
    else:
        report.append(("SKIP", dirpath.name, "no recognized build file (requirements.txt/pyproject.toml/Cargo.toml/go.mod/Makefile) and no obvious single entrypoint (binary/.py/.jar/.exe/.ps1) - skipped"))
    return InstallResult(ok=False)


ARCHIVE_AND_FLATTEN_HANDLERS = [handle_archive, handle_nested_clone]
# Python/PHP/binary come before the generic Make handler: a tool can ship a
# Makefile for unrelated dev tasks (docker, lint, a `make sync` helper target
# like SSRFmap's) alongside its real requirements.txt/pyproject.toml - that
# Makefile should never preempt the actual install path.
BUILD_HANDLERS = [handle_cargo, handle_go, handle_python, handle_bare_python_script, handle_bare_venv, handle_php, handle_binary, handle_make, handle_multi_binary, handle_foreign]


def run_pipeline(dirpath: Path, report) -> InstallResult:
    # Archive extraction / nested-clone flattening can change what's in the
    # directory, so re-run detection against the new contents up to a few
    # times (bounded, so a malformed tree can't loop forever).
    for _ in range(4):
        changed = False
        for handler in ARCHIVE_AND_FLATTEN_HANDLERS:
            result = handler(dirpath, report)
            if result is not None:
                if not result.ok:
                    return result  # already reported FAIL/SKIP/WARN - don't retry the same failure
                changed = True
                break
        if not changed:
            break
    for handler in BUILD_HANDLERS:
        result = handler(dirpath, report)
        if result is not None:
            return result
    return handle_suite(dirpath, report)


def process_loose_file(path: Path, report):
    if not os.access(path, os.X_OK):
        return
    # A top-level entry that is a symlink pointing into a tool subdirectory
    # (e.g. ~/tools/ntlm_theft.py -> ~/tools/ntlm_theft/ntlm_theft.py) belongs
    # to that subdirectory's handler, which may have set a venv shebang on it.
    # Rewriting it here to the system python would clobber that and strip the
    # tool of its venv-only deps - so leave such symlinks alone and only touch
    # a genuinely loose script that lives directly at the top level.
    try:
        real = path.resolve()
    except OSError:
        return
    if real.parent != config.TOOLS_DIR and config.TOOLS_DIR in real.parents:
        return
    changed = fix_shebang(path, "/usr/bin/env python3")
    if changed:
        report.append(("OK", path.name, "fixed broken 'python' shebang -> python3 (already on PATH via ~/tools)"))
