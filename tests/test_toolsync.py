"""Unit + fixture tests for the toolsync package. Plain asserts, no deps.

Run:  python3 tests/test_toolsync.py   (from the .toolsync/ directory)
"""
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # .toolsync/

from toolsync import config, detect, fsutil, linking, runners, state, verify

PASS = 0
FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}  {extra}")

def mkexe(p: Path, body="#!/bin/sh\necho hi\n", size_pad=0):
    p.write_text(body + ("#" * size_pad))
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p

# ── fsutil._norm / default_command_name ─────────────────────────────────────
check("_norm strips + lowercases", fsutil._norm("Petit-Potam_v2") == "petitpotamv2")
check("_norm empty", fsutil._norm("---") == "")
check("default_command_name keeps dots/dashes",
      linking.default_command_name(Path("/x/enum4linux-ng.py")) == "enum4linux-ng.py")
check("default_command_name strips junk",
      linking.default_command_name(Path("/x/we!rd name")) == "werdname")

# ── fsutil.dir_signature stability ──────────────────────────────────────────
with tempfile.TemporaryDirectory() as d:
    dp = Path(d)
    (dp / "a.py").write_text("x")
    s1 = fsutil.dir_signature(dp)
    s2 = fsutil.dir_signature(dp)
    check("dir_signature stable", s1 == s2)
    os.utime(dp / "a.py", (1, 2))  # change mtime
    check("dir_signature notices mtime change", fsutil.dir_signature(dp) != s1)

# ── detect.find_python_entrypoints ──────────────────────────────────────────
with tempfile.TemporaryDirectory() as d:
    base = Path(d) / "mytool"; base.mkdir()
    (base / "mytool.py").write_text("print()")
    eps = detect.find_python_entrypoints(base)
    check("py entrypoint: <name>.py found", (base / "mytool.py") in eps, eps)
with tempfile.TemporaryDirectory() as d:
    base = Path(d) / "x"; base.mkdir()
    (base / "solo.py").write_text("print()")
    eps = detect.find_python_entrypoints(base)
    check("py entrypoint: single top .py", eps == [base / "solo.py"], eps)
with tempfile.TemporaryDirectory() as d:
    base = Path(d) / "x"; base.mkdir()
    (base / "a.py").write_text(""); (base / "b.py").write_text("")
    check("py entrypoint: ambiguous -> none", detect.find_python_entrypoints(base) == [])

# ── detect.find_binary_entrypoint ───────────────────────────────────────────
with tempfile.TemporaryDirectory() as d:
    base = Path(d) / "chisel"; base.mkdir()
    mkexe(base / "chisel")
    check("binary entrypoint: name match", detect.find_binary_entrypoint(base) == base / "chisel")

# ── detect.find_foreign_entrypoint (+ dir preference) ───────────────────────
with tempfile.TemporaryDirectory() as d:
    base = Path(d) / "GodPotato"; base.mkdir()
    (base / "GodPotato.exe").write_bytes(b"MZ\x00\x00")
    res = detect.find_foreign_entrypoint(base)
    check("foreign: .exe by name", res and res[0].name == "GodPotato.exe" and res[1] == ".exe", res)
with tempfile.TemporaryDirectory() as d:
    base = Path(d) / "t"; base.mkdir()
    (base / "x64").mkdir(); (base / "Win32").mkdir()
    (base / "x64" / "tool.exe").write_bytes(b"MZ")
    (base / "Win32" / "tool.exe").write_bytes(b"MZ")
    res = detect.find_foreign_entrypoint(base)
    check("foreign: prefers x64 over Win32", res and "x64" in str(res[0]), res)
with tempfile.TemporaryDirectory() as d:
    base = Path(d) / "SharpThing"; base.mkdir()
    (base / "SharpThing.exe").write_bytes(b"MZ")
    (base / "SharpThing.ps1").write_text("echo")
    res = detect.find_foreign_entrypoint(base)
    check("foreign: payload (.exe) beats script (.ps1)", res and res[1] == ".exe", res)

# ── fsutil.fix_shebang ──────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "peas.py"; p.write_text("# just a comment\nprint(1)\n")
    changed = fsutil.fix_shebang(p, "/usr/bin/env python3")
    check("fix_shebang adds shebang to .py", changed and p.read_text().startswith("#!/usr/bin/env python3"))
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "crlf.py"; p.write_bytes(b"#!/usr/bin/env\r\nprint(1)\n")
    changed = fsutil.fix_shebang(p, "/usr/bin/env python3")
    check("fix_shebang heals broken bare-env shebang", changed and p.read_bytes().startswith(b"#!/usr/bin/env python3\n"))
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "bin"; p.write_bytes(b"\x7fELF\x00\x00binary")  # ELF, no .py suffix
    check("fix_shebang never touches a binary", fsutil.fix_shebang(p, "/usr/bin/env python3") is False)

# ── state.load_aliases / load_ignore (patch module file paths) ──────────────
with tempfile.TemporaryDirectory() as d:
    af = Path(d) / "aliases"; af.write_text("# c\nenum4linux-ng = enum4linux\nfoo=bar\n\nnoeq\n")
    old = config.ALIASES_FILE
    try:
        config.ALIASES_FILE = af
        a = state.load_aliases()
    finally:
        config.ALIASES_FILE = old
    check("load_aliases parses", a == {"enum4linux-ng": "enum4linux", "foo": "bar"}, a)
with tempfile.TemporaryDirectory() as d:
    ig = Path(d) / "ignore"; ig.write_text("# hdr\nmetasploit\n\nPeas\n")
    old = config.IGNORE_FILE
    try:
        config.IGNORE_FILE = ig
        g = state.load_ignore()
    finally:
        config.IGNORE_FILE = old
    check("load_ignore parses", g == {"metasploit", "Peas"}, g)

# ── config.resolve_paths precedence (arg > env > config-file > default) ──────
import os as _os
_envR, _envB = _os.environ.pop("TOOLSYNC_ROOT", None), _os.environ.pop("TOOLSYNC_BIN", None)
_save = (config.TOOLS_DIR, config.BIN_DIR, config.STATE_DIR, config.STATE_FILE,
         config.IGNORE_FILE, config.ALIASES_FILE, config.CONFIG_FILE)
try:
    config.CONFIG_FILE = Path(tempfile.gettempdir()) / "no_such_toolsync_cfg_xyz"
    config.resolve_paths()  # nothing set -> defaults
    check("resolve defaults to ~/tools", config.TOOLS_DIR == config.DEFAULT_ROOT, config.TOOLS_DIR)
    config.resolve_paths(root="/opt/mytools", bin_dir="/opt/bin")
    check("resolve explicit root", str(config.TOOLS_DIR) == "/opt/mytools", config.TOOLS_DIR)
    check("resolve explicit bin", str(config.BIN_DIR) == "/opt/bin", config.BIN_DIR)
    check("resolve repoints STATE_DIR", config.STATE_DIR == Path("/opt/mytools/.toolsync"), config.STATE_DIR)
    _os.environ["TOOLSYNC_ROOT"] = "/env/tools"
    config.resolve_paths()  # env beats default
    check("resolve env root", str(config.TOOLS_DIR) == "/env/tools", config.TOOLS_DIR)
    config.resolve_paths(root="/arg/tools")  # arg beats env
    check("resolve arg beats env", str(config.TOOLS_DIR) == "/arg/tools", config.TOOLS_DIR)
finally:
    _os.environ.pop("TOOLSYNC_ROOT", None)
    if _envR is not None: _os.environ["TOOLSYNC_ROOT"] = _envR
    if _envB is not None: _os.environ["TOOLSYNC_BIN"] = _envB
    (config.TOOLS_DIR, config.BIN_DIR, config.STATE_DIR, config.STATE_FILE,
     config.IGNORE_FILE, config.ALIASES_FILE, config.CONFIG_FILE) = _save

# ── end-to-end sync against a throwaway root (universality proof) ────────────
from toolsync import cli as ts_cli
with tempfile.TemporaryDirectory() as d:
    root = Path(d) / "tools"; bind = Path(d) / "bin"
    root.mkdir(); bind.mkdir()
    tool = root / "qzfixturetool"; tool.mkdir()
    mkexe(tool / "qzfixturetool", body="#!/bin/sh\necho ok\n", size_pad=50)
    _save2 = (config.TOOLS_DIR, config.BIN_DIR, config.STATE_DIR, config.STATE_FILE,
              config.IGNORE_FILE, config.ALIASES_FILE)
    _argv = sys.argv
    try:
        # main() resolves paths from its own args, so pass --root/--bin through argv
        sys.argv = ["toolsync", "--root", str(root), "--bin", str(bind)]
        ts_cli.main()
        link = bind / "qzfixturetool"
        check("e2e: widget linked into custom bin", link.is_symlink(), list(bind.iterdir()))
        check("e2e: link points at the tool", link.resolve() == (tool / "qzfixturetool").resolve())
        check("e2e: state.json written under custom root", (root / ".toolsync" / "state.json").exists())
    finally:
        sys.argv = _argv
        (config.TOOLS_DIR, config.BIN_DIR, config.STATE_DIR, config.STATE_FILE,
         config.IGNORE_FILE, config.ALIASES_FILE) = _save2

# ── runners._is_dotnet_assembly ─────────────────────────────────────────────
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "native.exe"; p.write_bytes(b"MZ" + b"\x00" * 200)
    check("_is_dotnet_assembly: not a .NET stub -> False", runners._is_dotnet_assembly(p) is False)
    p2 = Path(d) / "notpe.txt"; p2.write_text("hello")
    check("_is_dotnet_assembly: non-PE -> False", runners._is_dotnet_assembly(p2) is False)

# ── verify.detect_missing_apt_pkgs ──────────────────────────────────────────
check("apt map: ldap header", verify.detect_missing_apt_pkgs("fatal error: lber.h: No such file") == {"libldap2-dev"})
check("apt map: nothing", verify.detect_missing_apt_pkgs("ordinary output") == set())

# ── config invariants ───────────────────────────────────────────────────────
check("http is NOT a toolsync concern (sanity: version int)", isinstance(config.TOOLSYNC_VERSION, int))
check("foreign runners cover exe/jar/ps1", set(runners.FOREIGN_RUNNERS) == {".jar", ".exe", ".ps1"})

# ── universal: add (lone script / dir copy / git clone) ─────────────────────
from toolsync import universal
import subprocess as _sp

def _save_cfg():
    return (config.TOOLS_DIR, config.BIN_DIR, config.STATE_DIR, config.STATE_FILE,
            config.IGNORE_FILE, config.ALIASES_FILE, config._ALIASES)
def _restore_cfg(s):
    (config.TOOLS_DIR, config.BIN_DIR, config.STATE_DIR, config.STATE_FILE,
     config.IGNORE_FILE, config.ALIASES_FILE, config._ALIASES) = s

with tempfile.TemporaryDirectory() as d:
    root = Path(d) / "tools"; bind = Path(d) / "bin"; root.mkdir(); bind.mkdir()
    saved = _save_cfg()
    try:
        # (a) lone .py script -> linked in place as <stem> and <stem>.py
        f = Path(d) / "qzlone.py"
        f.write_text("#!/usr/bin/env python3\nimport sys\nif '--help' in sys.argv: print('usage'); sys.exit(0)\n")
        universal.cmd_add(["--root", str(root), "--bin", str(bind), str(f)])
        check("add lone .py -> <stem> link", (bind / "qzlone").is_symlink(), list(bind.iterdir()))
        check("add lone .py -> <stem>.py alias", (bind / "qzlone.py").is_symlink())
        check("add lone .py links in place (not moved)", (bind / "qzlone").resolve() == f.resolve())
        check("add lone .py did NOT copy into root", not (root / "qzlone.py").exists())

        # (b) external directory -> copied into root, then synced
        src = Path(d) / "qzdir"; src.mkdir()
        mkexe(src / "qzdir", body="#!/bin/sh\n[ \"$1\" = --help ] && echo usage\necho hi\n", size_pad=40)
        universal.cmd_add(["--root", str(root), "--bin", str(bind), str(src)])
        check("add dir -> copied into root", (root / "qzdir").is_dir())
        check("add dir -> command linked", (bind / "qzdir").is_symlink(), list(bind.iterdir()))

        # (c) git URL (local bare repo, no network) -> cloned into root, synced
        if shutil.which("git"):
            wk = Path(d) / "wk"; wk.mkdir()
            mkexe(wk / "qzgit", body="#!/bin/sh\necho hi\n", size_pad=40)
            env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
            _sp.run(["git", "init", "-q", str(wk)], check=True)
            _sp.run(["git", "-C", str(wk), "add", "-A"], check=True)
            _sp.run(["git", "-C", str(wk), "commit", "-qm", "init"], env=env, check=True)
            bare = Path(d) / "qzgitrepo.git"
            _sp.run(["git", "clone", "-q", "--bare", str(wk), str(bare)], check=True)
            universal.cmd_add(["--root", str(root), "--bin", str(bind), str(bare)])
            check("add git url -> cloned into root", (root / "qzgitrepo").is_dir(), list(root.iterdir()))

        # (d) bogus target -> graceful FAIL, no crash
        rep = []
        universal._add_one(str(Path(d) / "does_not_exist_xyz"), rep)
        check("add bogus path -> FAIL entry", rep and rep[0][0] == "FAIL", rep)
    finally:
        _restore_cfg(saved)

print(f"\nTOOLSYNC: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
