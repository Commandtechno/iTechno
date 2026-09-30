#!/usr/bin/env python3
"""Builds the release executable for this OS and CPU: one file with the app, the Spotify backend, a minimal ffmpeg
and the hashAB library inside.

    uv run --group build python packaging/build.py [--onedir]

Needs: cargo (https://rustup.rs), a C compiler and, to build ffmpeg, bash + make (MSYS2 on Windows). Output goes to
dist/itechno-<os>-<cpu>[.exe] (a folder with --onedir), plus for the release an archive of it with a .sha256.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "packaging" / "build"
sys.path.insert(0, str(ROOT))

from ipodkit import verify  # noqa: E402

EXE = ".exe" if sys.platform == "win32" else ""
OS = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
CPU = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine().lower())
# what the app does not use of what its dependencies drag in (iOpenPod is a Qt GUI app first)
EXCLUDES = ["PyQt6", "PyQt5", "PySide6", "tkinter", "matplotlib", "IPython", "pytest", "mypy"]


def run(*cmd: object, **kw) -> None:
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def build_oggify() -> Path:
    run("cargo", "build", "--release", cwd=ROOT / "oggify")
    return ROOT / "oggify" / "target" / "release" / f"oggify{EXE}"


def build_ffmpeg() -> Path:
    if env := os.environ.get("FFMPEG_BIN"):  # a build you made earlier
        return Path(env)
    out = BUILD / "ffmpeg"
    run("bash", ROOT / "packaging" / "build_ffmpeg.sh", out)
    return out / f"ffmpeg{EXE}"


def build_hashab() -> Path:
    out = BUILD / f"libhashab{verify._SUFFIX}"
    verify._build(out)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--onedir", action="store_true", help="a folder instead of one file: starts faster, unpacks nothing")
    a = ap.parse_args()

    BUILD.mkdir(parents=True, exist_ok=True)
    tools = [build_oggify(), build_ffmpeg(), build_hashab()]
    name = f"itechno-{OS}-{CPU}"

    cmd: list[object] = [sys.executable, "-m", "PyInstaller", ROOT / "itechno.py", "--name", name, "--noconfirm", "--clean",
                         "--console", "--onedir" if a.onedir else "--onefile",
                         "--distpath", ROOT / "dist", "--workpath", BUILD / "pyinstaller", "--specpath", BUILD,
                         "--paths", ROOT, "--collect-all", "wasmtime", "--collect-data", "iopenpod",
                         "--collect-submodules", "ipodkit"]
    for tool in tools:
        cmd += ["--add-binary", f"{tool}{os.pathsep}bin"]
    # wasmtime's native library is named _libwasmtime.so on Linux, which PyInstaller's lib*.so pattern does not pick up
    import wasmtime
    site = Path(wasmtime.__file__).parent.parent
    for lib in Path(wasmtime.__file__).parent.rglob("_libwasmtime*"):
        cmd += ["--add-binary", f"{lib}{os.pathsep}{lib.parent.relative_to(site)}"]
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    run(*cmd)

    artifact = ROOT / "dist" / (name if a.onedir else name + EXE)
    if a.onedir:
        print(f"\nbuilt {artifact} ({sum(f.stat().st_size for f in artifact.rglob('*') if f.is_file()) / 2**20:.0f} MB)")
        return
    # what gets published: an archive, because a download loses the executable bit
    if sys.platform == "win32":
        archive = artifact.with_suffix(".zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(artifact, "itechno.exe")
    else:
        archive = artifact.with_suffix(".tar.gz")
        with tarfile.open(archive, "w:gz") as t:
            t.add(artifact, "itechno")  # keeps the mode (executable)
    archive.with_name(archive.name + ".sha256").write_text(
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n")
    print(f"\nbuilt {artifact} ({artifact.stat().st_size / 2**20:.0f} MB), published as {archive.name} "
          f"({archive.stat().st_size / 2**20:.0f} MB)")

if __name__ == "__main__":
    main()
