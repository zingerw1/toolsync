# toolsync

Scan a tools directory and wire every tool onto PATH: venvs, builds, scripts,
binaries and foreign artifacts (`.exe`, `.jar`, `.ps1`), callable by name.

## Install

```bash
pip install .
toolsync self-install
```

## Usage

```bash
cd ~/tools
git clone <repo-url>
toolsync
```

Now it's callable by name from anywhere.

```bash
toolsync --update          # git pull clean repos before syncing
toolsync doctor            # check PATH + interpreters
toolsync add <path-or-url> # install a tool from outside ~/tools
toolsync locate <name>     # find where a tool lives and how to run it
```

While syncing, a live status line shows which tool is being processed and
what step it's on (cloning, pip install, cargo build, etc.) instead of
sitting silently during a slow step.

### Sync flags

```bash
toolsync --update            # git pull clean repos before syncing
toolsync --force <name>      # re-run install+verify for one tool, even if unchanged (repeatable)
toolsync --force-all         # re-run install+verify for every tool
toolsync --fix               # auto-confirm missing apt build deps and retry the tools that needed them, in the same run
toolsync --yes / -y          # auto-confirm apt-get installs (without retrying affected tools)
toolsync --explain <name>    # print the last recorded state/notes for a tool
toolsync --root <dir>        # tools directory to scan (default: ~/tools, or $TOOLSYNC_ROOT)
toolsync --bin <dir>         # where to put the PATH links (default: ~/.local/bin, or $TOOLSYNC_BIN)
```

`--fix` only resolves WARN/FAIL entries caused by missing system build
packages (e.g. a missing header for a C extension) — it installs them via
apt and automatically retries the affected tools without a second manual
run. Other WARN/FAIL/CONFLICT entries (an ambiguous entrypoint, a genuine
compile error, a `~/.local/bin` name clash) still need a human decision and
are left in the report.

### Subcommands

```bash
toolsync add <path-or-url>   # install a tool from anywhere: clone a git URL, copy a local
                              # path into the root, or link a lone script in place
toolsync self-install        # put the `toolsync` command on PATH
toolsync doctor [--yes]      # report root/bin/PATH health; offer to fix PATH
toolsync locate <name>       # find where a tool lives and how to run it (fuzzy match on
                              # directory/command name, falling back to a filename search)
```

`add`, `self-install`, and `doctor` also take `--root <dir>` / `--bin <dir>`
to override the defaults.

## What it handles

- Python (venv), Rust (`cargo build`), Go (`go build`), C/autotools
- Loose scripts and binaries, including PHP
- Foreign artifacts: `.exe` (wine/mono), `.jar` (java), `.ps1` (pwsh)
- Archives: extracts `.zip`/`.7z`/`.tar.*` in place, then re-detects

Run `toolsync --help` for the full flag reference.

## Development

The tests are plain asserts with no dependencies:

```bash
python3 tests/test_toolsync.py
```

Python 3.10 or newer is required.

## License

MIT
