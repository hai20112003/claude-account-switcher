"""Small helpers that differ between macOS and Windows."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

_POSIX_DIRS = [
    Path.home() / ".local" / "bin",
    Path("/usr/local/bin"),
    Path("/opt/homebrew/bin"),
]


def _windows_dirs() -> list[Path]:
    dirs = [Path.home() / ".local" / "bin"]
    appdata = os.environ.get("APPDATA")
    if appdata:
        dirs.append(Path(appdata) / "npm")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Programs" / "claude")
    return dirs


def find_binary(name: str) -> str | None:
    """Find a CLI on PATH or in the usual install folders for this platform."""
    found = shutil.which(name)
    if found:
        return found
    if IS_WINDOWS:
        for directory in _windows_dirs():
            for suffix in (".exe", ".cmd", ".bat"):
                candidate = directory / f"{name}{suffix}"
                if candidate.is_file():
                    return str(candidate)
        return None
    for directory in _POSIX_DIRS:
        candidate = directory / name
        if candidate.is_file():
            return str(candidate)
    return None


def hidden_process_kwargs() -> dict:
    """Keep background CLI calls from flashing a console window on Windows."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def visible_console_kwargs() -> dict:
    """Give interactive CLI calls (login) their own console window on Windows."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_CONSOLE}
    return {}
