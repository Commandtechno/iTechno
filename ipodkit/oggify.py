"""Client for the Spotify backend: the ``oggify`` binary in serve mode.

oggify (the Rust crate in oggify/, built on librespot) owns everything Spotify: the login
(it shows up as a Spotify Connect device; credentials are then cached), the
library and catalogue lookups, and the audio downloads. It is driven over a
pipe: one request per line in, one JSON reply per line out.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

log = logging.getLogger("ipodkit.oggify")
CRATE = Path(__file__).resolve().parents[1] / "oggify"
SOURCE_KINDS = ("playlist", "album", "artist", "liked", "track")


class OggifyError(RuntimeError):
    pass


def binary() -> Path:
    """The oggify executable: $OGGIFY_BIN, or the crate of this repository, (re)built when its sources are newer."""
    if env := os.environ.get("OGGIFY_BIN"):
        return Path(env)
    exe = CRATE / "target" / "release" / ("oggify.exe" if sys.platform == "win32" else "oggify")
    sources = [CRATE / "Cargo.toml", CRATE / "Cargo.lock", *(CRATE / "src").glob("*.rs")]
    if not exe.exists() or any(f.exists() and f.stat().st_mtime > exe.stat().st_mtime for f in sources):
        if not shutil.which("cargo"):
            raise OggifyError(f"{exe} is not built and cargo is not installed (https://rustup.rs)")
        print("Building the Spotify backend (first run only, takes a minute)…")
        if subprocess.run(["cargo", "build", "--release"], cwd=CRATE).returncode != 0:
            raise OggifyError("could not build oggify")
    return exe


class Oggify:
    def __init__(self) -> None:
        env = {"RUST_LOG": "info,libmdns=error", **os.environ}
        self.proc = subprocess.Popen([str(binary()), "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=env)
        threading.Thread(target=self._stderr, daemon=True).start()
        self.on_waiting: Callable[[float], None] | None = None  # told how long Spotify's rate limit holds a download up
        self.username: str = self._receive()["username"]

    def _stderr(self) -> None:
        """Everything the backend says goes to the log; the terminal only gets what the user must see."""
        for line in self.proc.stderr:
            line = line.rstrip()
            is_log = line.startswith("[")  # env_logger lines; the rest is addressed to the user (the login prompt)
            log.info("backend: %s", line)
            expected = "error audio key" in line  # the rate limit at work: handled, and reported as a waiting event
            if not is_log or ((" WARN " in line or " ERROR " in line) and not expected):
                print(line, file=sys.stderr)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)

    def __enter__(self) -> Oggify:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── protocol ────────────────────────────────────────────────────────
    def _send(self, request: str) -> None:
        if "\n" in request:
            raise ValueError("requests are single lines")
        log.info("-> %s", request)
        try:
            self.proc.stdin.write(request + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError:
            raise OggifyError("the Spotify backend exited unexpectedly") from None

    def _receive(self) -> dict:
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise OggifyError("the Spotify backend exited unexpectedly")
            log.info("<- %s", line.strip()[:600])
            reply = json.loads(line)
            if reply.get("event") != "waiting":
                break
            if self.on_waiting:
                self.on_waiting(reply["seconds"])
        if not reply.get("ok"):
            raise OggifyError(reply.get("error", "unknown error"))
        return reply

    def _ask(self, request: str) -> dict:
        self._send(request)
        return self._receive()

    # ── library and catalogue ───────────────────────────────────────────
    def playlists(self) -> list[dict]:
        """The user's playlists (own and followed), as ``{id, name, length, owner}``."""
        return [p for p in self._ask("rootlist")["playlists"] if p.get("name")]

    def source(self, kind: str, spotify_id: str = "") -> dict:
        """Resolve a source to ``{name, tracks: [track id, …]}``, in order."""
        if kind == "track":
            return {"name": None, "tracks": [spotify_id]}
        return self._ask(f"{kind} {spotify_id}".strip())

    def limits(self) -> dict:
        """Spotify's rate limit on downloads, as the backend currently models it: ``{interval, available}``."""
        return self._ask("limits")

    def search(self, query: str) -> list[str]:
        return self._ask("search " + " ".join(query.split()))["tracks"]

    def tracks(self, ids: Iterable[str], progress: Callable[[int], None] | None = None) -> dict[str, dict]:
        """Metadata per track ID. Tracks that could not be described carry an ``error`` key instead."""
        ids, found = list(dict.fromkeys(ids)), {}
        for i in range(0, len(ids), 40):
            for t in self._ask("tracks " + " ".join(ids[i:i + 40]))["tracks"]:
                found[t["id"]] = t
            if progress:
                progress(min(i + 40, len(ids)))
        return found

    def downloads(self, jobs: Iterable[tuple[str, Path]]) -> Iterator[tuple[str, Path, str | None]]:
        """Download tracks as Ogg Vorbis, yielding ``(id, file, error)`` as each one ends.

        The next download is already running while the caller handles the one
        just yielded. Files that already exist are not fetched again.
        """
        def start(job: tuple[str, Path] | None) -> bool:
            if job and not job[1].exists():
                self._send(f"download {job[0]} {job[1]}")
                return True
            return False

        jobs = iter(jobs)
        current = next(jobs, None)
        in_flight = start(current)
        try:
            while current:
                error = None
                if in_flight:
                    in_flight = False
                    try:
                        self._receive()
                    except OggifyError as e:
                        if self.proc.poll() is not None:
                            raise
                        error = str(e)
                following = next(jobs, None)
                in_flight = start(following)
                yield current[0], current[1], error
                current = following
        finally:
            if in_flight and self.proc.poll() is None:  # abandoned early: keep requests and replies paired
                try:
                    self._receive()
                except OggifyError:
                    pass
