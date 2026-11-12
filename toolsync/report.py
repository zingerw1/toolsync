"""Pre-scan report checks (duplicate / typo'd directory names) + printing."""
import re
from pathlib import Path

from .ui import C, rule

# level -> (glyph, colour attribute on ui.C, summary word)
_STYLE = {
    "OK":       ("✔", "green",  "ok"),
    "SKIP":     ("–", "gray",   "skipped"),
    "WARN":     ("▲", "yellow", "warnings"),
    "CONFLICT": ("◆", "yellow", "conflicts"),
    "FAIL":     ("✘", "red",    "failed"),
}


def print_report(report, empty_msg="toolsync: nothing to do.", elapsed=None):
    """Print one glyph-marked line per entry, then a thin rule and a count line."""
    if not report:
        print(f"  {C.gray}{empty_msg}{C.reset}")
        return
    width = max(len(r[1]) for r in report)
    print(rule("report"))
    for level, name, msg in report:
        glyph, colour, _ = _STYLE[level]
        print(f"  {getattr(C, colour)}{glyph}{C.reset}  {C.white}{name.ljust(width)}{C.reset}  {C.gray}{msg}{C.reset}")
    counts = {k: 0 for k in _STYLE}
    for level, _, _ in report:
        counts[level] += 1
    parts = [f"{getattr(C, _STYLE[k][1])}{v} {_STYLE[k][2]}{C.reset}" for k, v in counts.items() if v]
    took = f"   {C.gray}in {elapsed:.1f}s{C.reset}" if elapsed is not None else ""
    print(rule())
    print("  " + "   ".join(parts) + took)


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
