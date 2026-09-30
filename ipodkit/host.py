"""The few things that differ per operating system: where a plugged-in iPod shows up, and keeping the machine awake."""
from __future__ import annotations

import os
import shutil
import string
import subprocess
import sys
from pathlib import Path


def _mount_points() -> list[Path]:
    if sys.platform == "darwin":
        return list(Path("/Volumes").iterdir())
    if sys.platform == "win32":
        return [Path(f"{d}:\\") for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
    try:  # Linux and the BSDs: whatever the desktop, udisks or fstab mounted, wherever it put it
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            # the mount point is field 2, with spaces escaped as \040
            return [Path(line.split()[1].replace("\\040", " ")) for line in f if len(line.split()) > 1]
    except OSError:
        return []


def find_ipods() -> list[Path]:
    """Every mounted volume that holds an iPod's database."""
    found = []
    for p in _mount_points():
        try:
            if (p / "iPod_Control" / "iTunes").is_dir():
                found.append(p)
        except OSError:  # a drive letter with no disc, or a mount we may not read
            pass
    return found


def stay_awake() -> None:
    """Keep the machine from idle-sleeping for as long as this process lives: a big download takes hours."""
    if sys.platform == "darwin":
        if shutil.which("caffeinate"):
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    elif sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    elif shutil.which("systemd-inhibit") and shutil.which("tail"):
        # holds the inhibitor for as long as `tail` follows this pid, i.e. until we exit
        subprocess.Popen(["systemd-inhibit", "--what=idle:sleep", "--who=iTechno", "--why=Syncing an iPod",
                          "tail", f"--pid={os.getpid()}", "-f", "/dev/null"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
