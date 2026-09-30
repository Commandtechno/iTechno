"""Where things live, for a source checkout and for the packaged release alike.

A release is a frozen bundle: the bundled tools (oggify, ffmpeg, the hashAB library) sit in its ``bin/`` folder and
the user's data cannot live next to the program, so it goes in the OS's per-user data folder. A checkout keeps
everything where it always was: next to the code.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
ROOT = Path(sys._MEIPASS) if FROZEN else Path(__file__).resolve().parents[1]  # type: ignore[attr-defined]
EXE = ".exe" if sys.platform == "win32" else ""


def bundled(name: str) -> Path | None:
    """A file shipped inside the release's ``bin/`` folder, if this is a release and it has it."""
    p = ROOT / "bin" / name
    return p if FROZEN and p.exists() else None


def tool(name: str) -> str:
    """The command that runs an external tool: the bundled copy, else the one on PATH, else the bare name."""
    if p := bundled(name + EXE):
        return str(p)
    return shutil.which(name) or name


def data_dir() -> Path:
    """Where the user's sync state and database snapshots go. $ITECHNO_HOME overrides."""
    if env := os.environ.get("ITECHNO_HOME"):
        return Path(env)
    if not FROZEN:
        return ROOT
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "iTechno"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "iTechno"
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "iTechno"


def state_dir() -> Path:
    return data_dir() / (".state" if not FROZEN and not os.environ.get("ITECHNO_HOME") else "state")


def snapshots_dir() -> Path:
    return data_dir() / "snapshots"
