"""Foreign-artifact interpreters: how to run a .jar / .exe / .ps1."""
import shutil
from pathlib import Path


def _is_dotnet_assembly(path: Path) -> bool:
    """True if a PE file is a .NET/CLR assembly (mono can run it) rather than a
    native Windows binary. Parses the PE optional header's COM-descriptor data
    directory (index 14) - the definitive, file-size-independent check. (A
    plain string scan for 'BSJB'/mscoree is unreliable: in a larger assembly
    the CLR metadata can sit megabytes into the file, e.g. SharpHound.exe.)"""
    try:
        import struct
        with open(path, "rb") as f:
            data = f.read(1024)
        if data[:2] != b"MZ" or len(data) < 0x40:
            return False
        e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
        if e_lfanew + 24 > len(data) or data[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
            return False
        opt = e_lfanew + 24  # start of the optional header
        magic = struct.unpack_from("<H", data, opt)[0]
        if magic == 0x10B:      # PE32
            num_rvas_off, datadir_off = opt + 92, opt + 96
        elif magic == 0x20B:    # PE32+
            num_rvas_off, datadir_off = opt + 108, opt + 112
        else:
            return False
        if num_rvas_off + 4 > len(data):
            return False
        num_rvas = struct.unpack_from("<I", data, num_rvas_off)[0]
        COM_DESCRIPTOR = 14
        if num_rvas <= COM_DESCRIPTOR:
            return False
        entry = datadir_off + COM_DESCRIPTOR * 8
        if entry + 8 > len(data):
            return False
        va, size = struct.unpack_from("<II", data, entry)
        return va != 0 and size != 0
    except (OSError, struct.error):
        return False


def _exe_runner(path: Path):
    """Pick a runner for a Windows .exe from whatever is installed: mono for a
    .NET assembly, otherwise wine. Returns None if nothing suitable is present
    (the artifact is then linked locatable-only, not executed)."""
    if _is_dotnet_assembly(path):
        if shutil.which("mono"):
            return ["mono"]
    if shutil.which("wine64"):
        return ["wine64"]
    if shutil.which("wine"):
        return ["wine"]
    # A .NET exe with no mono but with wine still runs under wine.
    return None


def _ps1_runner(path: Path):
    for exe in ("pwsh", "powershell"):
        if shutil.which(exe):
            return [exe, "-NoProfile", "-File"]
    return None


# Foreign artifact type -> how to run it. This table is the single place to
# teach toolsync a new file type: map an extension to a function returning the
# argv prefix to exec the artifact with (or None if no interpreter for it is
# installed, in which case the artifact is still linked so it's locatable on
# PATH - handy for payloads you transfer elsewhere - just not run here).
FOREIGN_RUNNERS = {
    ".jar": lambda p: (["java", "-jar"] if shutil.which("java") else None),
    ".exe": _exe_runner,
    ".ps1": _ps1_runner,
}
FOREIGN_SUFFIXES = tuple(FOREIGN_RUNNERS)
