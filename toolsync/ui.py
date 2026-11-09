"""Live 'what's it doing right now' status line for the sync loop.

Plain, info-dense report lines (OK/SKIP/WARN/FAIL) are still collected and
printed at the end by report.py - this only adds a transient spinner line
during the run so a slow step (pip install, cargo build, ...) doesn't look
like a hang. Falls back to a plain carriage-return line if rich isn't
importable or stdout isn't a terminal, and is a no-op either way if disabled.
"""
import shutil
import sys

try:
    from rich.console import Console
    from rich.live import Live
    from rich.spinner import Spinner
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False


class _Status:
    def __init__(self):
        self._enabled = sys.stdout.isatty()
        self._live = None
        self._spinner = None

    def start(self):
        if not self._enabled:
            return
        if _HAS_RICH:
            self._spinner = Spinner("dots", text="")
            self._live = Live(self._spinner, console=Console(), refresh_per_second=10, transient=True)
            self._live.start()

    def update(self, tool: str, step: str):
        if not self._enabled:
            return
        text = f"{tool}: {step}"
        if _HAS_RICH and self._live:
            self._spinner.update(text=text)
        else:
            width = shutil.get_terminal_size((80, 20)).columns
            sys.stdout.write("\r" + text[:width].ljust(width))
            sys.stdout.flush()

    def stop(self):
        if not self._enabled:
            return
        if _HAS_RICH and self._live:
            self._live.stop()
            self._live = None
        else:
            width = shutil.get_terminal_size((80, 20)).columns
            sys.stdout.write("\r" + " " * width + "\r")
            sys.stdout.flush()


STATUS = _Status()
