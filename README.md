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
```

## What it handles

- Python (venv), Rust (`cargo build`), Go (`go build`), C/autotools
- Loose scripts and binaries, including PHP
- Foreign artifacts: `.exe` (wine/mono), `.jar` (java), `.ps1` (pwsh)
- Archives: extracts `.zip`/`.7z`/`.tar.*` in place, then re-detects

Run `toolsync --help` for the full flag reference.

## License

MIT
