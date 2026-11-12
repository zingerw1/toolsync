"""Minimal terminal UI: a thin animated status line during the sync loop, and
the palette/glyphs the final report shares.

Plain report lines (OK/SKIP/WARN/FAIL) are still collected and printed at the
end by report.py. This only adds a transient line while syncing so a slow step
(pip install, cargo build, ...) doesn't look like a hang:

    ⠹ nmap  pip install .                 ━━━━━━━━━━━━──────────────  12/80  3s

Pure standard library. Falls back to nothing when stdout isn't a terminal, and
honours NO_COLOR.
"""
import os
import shutil
import sys
import threading
import time

_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def color_enabled() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _sgr(code: str) -> str:
    return f"\033[{code}m" if color_enabled() else ""


class Palette:
    """Looked up lazily so a redirected stdout / NO_COLOR is honoured."""
    reset = property(lambda s: _sgr("0"))
    bold = property(lambda s: _sgr("1"))
    dim = property(lambda s: _sgr("2"))
    white = property(lambda s: _sgr("1;38;5;231"))
    gray = property(lambda s: _sgr("38;5;245"))
    track = property(lambda s: _sgr("38;5;238"))
    green = property(lambda s: _sgr("38;5;114"))
    yellow = property(lambda s: _sgr("38;5;179"))
    red = property(lambda s: _sgr("38;5;203"))
    cyan = property(lambda s: _sgr("38;5;80"))
    purple = property(lambda s: _sgr("38;5;141"))


C = Palette()


def term_width(limit: int = 90) -> int:
    return max(40, min(shutil.get_terminal_size((80, 20)).columns, limit))


def rule(label: str = "", width: int = 0) -> str:
    """A thin horizontal line, optionally with a label: `── label ───────`."""
    width = width or term_width()
    if not label:
        return f"{C.track}{'─' * width}{C.reset}"
    rest = max(3, width - len(label) - 4)
    return f"{C.track}──{C.reset} {C.white}{label}{C.reset} {C.track}{'─' * rest}{C.reset}"


def bar(done: int, total: int, width: int = 28) -> str:
    """Thin progress line: white for done, dark grey for the rest."""
    total = max(total, 1)
    filled = min(width, done * width // total)
    return f"{C.white}{'━' * filled}{C.reset}{C.track}{'─' * (width - filled)}{C.reset}"


class _Status:
    def __init__(self):
        self._enabled = sys.stdout.isatty()
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._tool = ""
        self._step = ""
        self._done = 0
        self._total = 0
        self._started = 0.0

    def start(self, total: int = 0):
        if not self._enabled or self._thread:
            return
        self._total, self._done = total, 0
        self._started = time.monotonic()
        self._stop.clear()
        sys.stdout.write("\033[?25l")  # hide cursor while animating
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def update(self, tool: str, step: str):
        with self._lock:
            self._tool, self._step = tool, step

    def advance(self):
        with self._lock:
            self._done += 1

    def _render(self, frame: int) -> str:
        with self._lock:
            tool, step, done, total = self._tool, self._step, self._done, self._total
        width = term_width(100)
        elapsed = int(time.monotonic() - self._started)
        tail = f"  {bar(done, total)}  {C.gray}{done}/{total}  {elapsed}s{C.reset}" if total else f"  {C.gray}{elapsed}s{C.reset}"
        tail_len = (2 + 28 + 2 + len(f"{done}/{total}  {elapsed}s")) if total else 2 + len(f"{elapsed}s")
        room = max(10, width - 4 - tail_len)
        text = f"{tool}  {step}" if step else tool
        if len(text) > room:
            text = text[: room - 1] + "…"
        name, _, rest = text.partition("  ")
        label = f"{C.white}{name}{C.reset}" + (f"  {C.gray}{rest}{C.reset}" if rest else "")
        pad = " " * max(0, room - len(text))
        return f"\r\033[K  {C.cyan}{_FRAMES[frame % len(_FRAMES)]}{C.reset} {label}{pad}{tail}"

    def _run(self):
        frame = 0
        while not self._stop.is_set():
            sys.stdout.write(self._render(frame))
            sys.stdout.flush()
            frame += 1
            self._stop.wait(0.08)

    def stop(self):
        if not self._enabled or not self._thread:
            return
        self._stop.set()
        self._thread.join()
        self._thread = None
        sys.stdout.write("\r\033[K\033[?25h")  # clear the line, restore cursor
        sys.stdout.flush()


STATUS = _Status()
