#!/usr/bin/env python3
"""iTechno: Spotify → iPod sync, and an iPod library manager.

    itechno                    interactive: pick playlists, search, sync
    itechno run | fetch | ...  the sync commands (see `itechno --help`)
    itechno ipod ...           manage the iPod's library directly (see `itechno ipod --help`)
    itechno doctor             check that the bundled tools work on this machine

This is the entry point of the packaged release; from a checkout, `sync.py` and `ctrl.py` do the same jobs.
"""
import sys


def doctor() -> int:
    import platform
    import subprocess

    from ipodkit import host, paths

    bad = 0

    def line(ok: bool, what: str, detail: str = "") -> None:
        nonlocal bad
        bad += not ok
        print(f"  {'ok ' if ok else 'FAIL'} {what:<17}{detail}")

    print(f"iTechno on {platform.system()} {platform.machine()}, Python {platform.python_version()}"
          f"{' (packaged)' if paths.FROZEN else ' (from source)'}")
    print(f"  data     {paths.data_dir()}")
    ffmpeg = paths.tool("ffmpeg")
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-version"], capture_output=True, text=True, timeout=20).stdout
        enc = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20).stdout
        has = [c for c in ("aac_at", "aac") if f" {c} " in enc]
        line(bool(has), "ffmpeg", f"{'ffmpeg ' + out.split()[2]} · AAC encoder: {has[0] if has else 'none'} · {ffmpeg}")
    except (OSError, subprocess.SubprocessError, IndexError) as e:
        line(False, "ffmpeg", f"{ffmpeg}: {e}")
    try:
        from ipodkit import oggify
        exe = oggify.binary()
        line(exe.exists(), "spotify backend", str(exe))
    except Exception as e:  # not built, no cargo
        line(False, "spotify backend", str(e))
    try:
        from ipodkit import verify
        line(bool(verify._lib()), "hashAB library", "loads (nano 6G/7G signatures can be checked)")
    except Exception as e:
        line(False, "hashAB library", str(e).splitlines()[0])
    try:
        import iopenpod.itunesdb_writer.hashab as hashab
        hashab.compute_hashab(bytes(20), bytes(8))
        line(True, "hashAB signing", "WASM runtime works")
    except Exception as e:
        line(False, "hashAB signing", f"{type(e).__name__}: {e}")
    try:  # loaded lazily by the engine, so the packager cannot be trusted to have found them
        import numpy  # noqa: F401
        import PIL.Image  # noqa: F401
        from iopenpod.artworkdb_writer import artwork_writer  # noqa: F401
        line(True, "cover art", "artwork writer loads")
    except Exception as e:
        line(False, "cover art", f"{type(e).__name__}: {e}")
    ipods = host.find_ipods()
    print(f"  {'ok ' if ipods else '-- '} {'iPod':<17}" + (", ".join(map(str, ipods)) if ipods else "none plugged in"))
    return 1 if bad else 0


def main() -> None:
    from ipodkit import paths
    if paths.FROZEN:
        sys.argv[0] = "itechno"
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        sys.exit(doctor())
    if len(sys.argv) > 1 and sys.argv[1] == "ipod":
        del sys.argv[1]
        sys.argv[0] += " ipod"
        import ctrl
        if paths.FROZEN:
            ctrl.__doc__ = ctrl.__doc__.replace("uv run ctrl.py", "itechno ipod")
        ctrl.main()
    else:
        import sync
        if paths.FROZEN:
            sync.__doc__ = sync.__doc__.replace("uv run sync.py", "itechno")
        sync.main()


if __name__ == "__main__":
    main()
