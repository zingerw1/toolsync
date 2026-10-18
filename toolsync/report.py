"""Pre-scan report checks (duplicate / typo'd directory names) + printing."""
import re
from pathlib import Path

_TAGS = {"OK": "OK   ", "SKIP": "SKIP ", "WARN": "WARN ", "CONFLICT": "CONFL", "FAIL": "FAIL "}


def print_report(report, empty_msg="toolsync: nothing to do."):
    """Print aligned [TAG] name  msg lines plus a one-line count summary."""
    if not report:
        print(empty_msg)
        return
    width = max(len(r[1]) for r in report)
    for level, name, msg in report:
        print(f"[{_TAGS[level]}] {name.ljust(width)}  {msg}")
    counts = {k: 0 for k in _TAGS}
    for level, _, _ in report:
        counts[level] += 1
    summary = " · ".join(f"{k} {v}" for k, v in counts.items() if v)
    if summary:
        print(f"\n{summary}")


def report_duplicate_dirnames(tools_dir: Path, ignore: set, report):
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    seen = {}
    for entry in sorted(tools_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name in ignore:
            continue
        key = norm(entry.name)
        seen.setdefault(key, []).append(entry.name)
    for names in seen.values():
        if len(names) > 1:
            report.append(("WARN", "/".join(names), "these directory names look like duplicates/typos of each other - pick one, remove or .toolsync/ignore the rest"))
