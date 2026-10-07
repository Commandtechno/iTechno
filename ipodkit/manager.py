"""Programmatic library management for iPods: Classic, Mini, Nano 1G-7G and the pre-Classic full-size models.
(Shuffle and Touch are not supported by the engine.)

Thin layer over iOpenPod's engine (https://github.com/TheRealSavi/iOpenPod):
load the library as plain dicts, mutate, save. Every save is

  1. preceded by a snapshot of the on-device database directory,
  2. followed by an independent signature verification for the device's scheme (see verify.py) plus a
     re-parse of what was written, and
  3. rolled back from the snapshot automatically if either check fails,

because an iPod that sees a bad signature refuses the whole library.
"""
from __future__ import annotations

import logging
import random
import secrets
import shutil
import string
import time
from dataclasses import dataclass
from pathlib import Path

import iopenpod.itunesdb_writer.hashab as _hashab
from iopenpod.device import (ChecksumType, capabilities_for_family_gen, identify_ipod_at_path, resolve_itdb_path,
                             set_current_device)
from iopenpod.itunesdb_writer.hash72 import read_hash_info
from iopenpod.itunesdb_parser.ipod_library import load_ipod_library
from iopenpod.itunesdb_shared.playlist_kinds import is_podcast_playlist
from iopenpod.sync.quick_writes import write_cached_itunesdb

from . import paths
from .verify import verify_database

log = logging.getLogger("ipodkit")

# hashAB only (nano 6G/7G). iOpenPod 1.68 signs the header with hashing_scheme=4 and then patches the
# field to 3 *after* signing. The field is covered by the SHA1, so the result
# never verifies. iTunes signs with 3 in place (proven by replaying iTunes' own
# signatures through verify.py), so sign with 3.
_hashab.ITDB_CHECKSUM_HASHAB = 3

_EXT_FILETYPE = {".mp3": "MP3", ".m4a": "AAC", ".aac": "AAC", ".wav": "WAV", ".aif": "AIFF", ".aiff": "AIFF"}
PLAYLIST_KEYS = ("mhlp", "mhlp_podcast", "mhlp_smart")


def has_lyrics(path: str | Path) -> bool:
    """Whether an audio file embeds lyrics where an iPod reads them: an MP4 ``©lyr`` atom or an ID3 ``USLT`` frame."""
    from mutagen import File as MutagenFile

    try:
        tags = MutagenFile(path).tags
    except Exception:
        return False
    if not tags:
        return False
    if hasattr(tags, "getall"):  # ID3
        return any(str(frame.text).strip() for frame in tags.getall("USLT"))
    return any(str(text).strip() for text in tags.get("\xa9lyr", []))


class SaveError(RuntimeError):
    pass


@dataclass
class SaveReport:
    tracks: int
    signatures: dict[str, bool]
    snapshot: Path


class IPod:
    def __init__(self, mount: str | Path, snapshot_root: str | Path | None = None):
        self.mount = Path(mount)
        self.itunes_dir = self.mount / "iPod_Control" / "iTunes"
        self.artwork_db = self.mount / "iPod_Control" / "Artwork" / "ArtworkDB"
        self.snapshot_root = Path(snapshot_root) if snapshot_root else paths.snapshots_dir()
        self.keep_snapshots = 5  # of each kind (see snapshot); each is ~20MB, a batched sync takes one per batch
        self._snapshotted = False
        self.device = identify_ipod_at_path(str(self.mount))
        if self.device is None:
            raise RuntimeError(f"No iPod identified at {self.mount}")
        # The engine resolves capabilities (iTunesCDB compression, SQLite,
        # checksum type) from this registry and silently degrades without it.
        set_current_device(self.device)
        self.caps = capabilities_for_family_gen(
            self.device.model_family, self.device.generation or "",
            capacity=self.device.capacity or None, model_number=self.device.model_number or None)
        if self.caps is None:
            raise RuntimeError(f"Unrecognised iPod model: {self.device.model_family} {self.device.generation}")
        if self.caps.is_shuffle:
            raise RuntimeError("iPod Shuffle databases (iTunesSD) are not supported by the engine")
        self.checksum: ChecksumType = self.caps.checksum
        self.fwid = bytes.fromhex(self.device.firewire_guid or "")
        if self.checksum in (ChecksumType.HASH58, ChecksumType.HASHAB) and len(self.fwid) < 8:
            raise RuntimeError(f"{self.device.model_family} {self.device.generation} databases are signed with the "
                               "FireWire ID, which could not be read from this iPod")
        # iTunesCDB on nano 5G and later, iTunesDB on everything before
        self.db_path = Path(resolve_itdb_path(str(self.mount)) or self.itunes_dir / "iTunesDB")
        self.reload()

    # ── read ────────────────────────────────────────────────────────────
    def reload(self) -> None:
        lib = load_ipod_library(str(self.db_path))
        if lib is None:
            raise RuntimeError("Could not parse the iPod database")
        self._loaded = self._db_stamp()
        self.tracks: list[dict] = lib["mhlt"]
        self.playlists: list[dict] = [p for k in PLAYLIST_KEYS for p in lib.get(k, [])]
        self._mirror_orphaned_playlists()

    def _mirror_orphaned_playlists(self) -> None:
        # The nano 6G/7G builds its playlist list from the SQLite library, which the engine writes from dataset 2
        # only. iTunes/Music keeps user playlists in dataset 3 alone, and the engine never mirrors a dataset-3
        # playlist back, so after one such sync every playlist but the ones created since is invisible on the device.
        in_ds2 = {p.get("playlist_id") for p in self.playlists if p.get("_mhsd_result_key") == "mhlp"}
        for p in list(self.playlists):
            if (p.get("_mhsd_result_key") == "mhlp_podcast" and not p.get("master_flag") and not p.get("mhsd5_type")
                    and p.get("playlist_id") not in in_ds2 and not is_podcast_playlist(p)):
                in_ds2.add(p.get("playlist_id"))
                self.playlists.append({**p, "items": [dict(i) for i in p.get("items", [])],
                                       "_mhsd_dataset_type": 2, "_mhsd_result_key": "mhlp"})

    def _db_stamp(self) -> tuple[int, int]:
        st = self.db_path.stat()
        return st.st_mtime_ns, st.st_size

    def user_playlists(self) -> list[dict]:
        # The nano 5G+ mirrors each user playlist across two datasets; show one.
        seen: dict[int, dict] = {}
        for p in self.playlists:
            if not p.get("master_flag") and not p.get("mhsd5_type") and p.get("_mhsd_result_key") != "mhlp_smart":
                seen.setdefault(p.get("playlist_id"), p)
        return list(seen.values())

    def add_to_playlist(self, playlist: dict, track: dict) -> None:
        for p in self.playlists:  # keep every mirrored copy in step
            if p.get("playlist_id") == playlist.get("playlist_id"):
                p.setdefault("items", []).append({"track_id": track["track_id"]})

    def find_tracks(self, query: str) -> list[dict]:
        q = query.lower()
        return [t for t in self.tracks
                if q in " ".join(str(t.get(k) or "") for k in ("Title", "Artist", "Album")).lower()]

    def find_playlist(self, name: str) -> dict | None:
        return next((p for p in self.user_playlists() if p.get("Title") == name), None)

    def verify(self) -> dict[str, bool]:
        return verify_database(self.mount, self.db_path, self.checksum, self.fwid)

    # ── mutate (in memory until save) ───────────────────────────────────
    def alloc_path(self, ext: str) -> Path:
        """Pick an unused on-device path, so callers can journal it before copying."""
        music = self.mount / "iPod_Control" / "Music"
        folders = sorted(d for d in music.iterdir() if d.is_dir())
        while True:
            dest = random.choice(folders) / ("".join(random.choices(string.ascii_uppercase, k=4)) + ext.lower())
            if not dest.exists():
                return dest

    def add_track(self, src: str | Path, dest: Path | None = None, extra: dict | None = None, **tags) -> dict:
        """Copy an audio file onto the iPod and add it to the library.

        ``extra`` is merged into the track dict last (e.g. Comment, year,
        track_number), overriding anything read from the file's own tags.
        """
        from mutagen import File as MutagenFile

        src = Path(src)
        ext = src.suffix.lower()
        if ext not in _EXT_FILETYPE:
            raise ValueError(f"Unsupported format {ext}; iPods play {sorted(_EXT_FILETYPE)}")
        audio = MutagenFile(src, easy=True)
        if audio is None:
            raise ValueError(f"Not a readable audio file: {src}")

        dest = dest or self.alloc_path(ext)
        shutil.copyfile(src, dest)

        def tag(key: str) -> str | None:
            try:
                return (audio.tags.get(key) or [None])[0] if audio.tags else None
            except KeyError:
                return None

        now = int(time.time())
        track = {
            "Title": tags.get("title") or tag("title") or src.stem,
            "Artist": tags.get("artist") or tag("artist"),
            "Album": tags.get("album") or tag("album"),
            "Album Artist": tags.get("album_artist") or tag("albumartist"),
            "Genre": tags.get("genre") or tag("genre"),
            "Location": ":" + ":".join(dest.relative_to(self.mount).parts),
            "filetype": _EXT_FILETYPE[ext],
            "size": dest.stat().st_size,
            "length": int(audio.info.length * 1000),
            "bitrate": int(getattr(audio.info, "bitrate", 0) / 1000),
            "sample_rate_1": int(getattr(audio.info, "sample_rate", 44100)),
            "date_added": now,
            "last_modified": now,
            "media_type": 1,
            "track_id": max((t.get("track_id", 0) for t in self.tracks), default=0) + 1,
            "db_track_id": secrets.randbits(63) | 1,
            # the iPod shows the lyrics embedded in the file, but only looks for them when this flag says so
            "lyrics_flag": 1 if has_lyrics(src) else 0,
        }
        track.update({k: v for k, v in (extra or {}).items() if v is not None})
        self.tracks.append(track)
        self._master()["items"].append({"track_id": track["track_id"]})
        return track

    def set_playlist_tracks(self, name: str, tracks: list[dict]) -> dict:
        """Create or replace a plain playlist so it holds exactly ``tracks``, in order."""
        items = [{"track_id": t["track_id"]} for t in tracks]
        existing = next((p for p in self.user_playlists()
                         if p.get("Title") == name and not p.get("smart_playlist_rules")), None)
        if existing is None:
            return self.create_playlist(name, tracks)
        for p in self.playlists:  # every mirrored copy
            if p.get("playlist_id") == existing.get("playlist_id"):
                p["items"] = [dict(i) for i in items]
        return existing

    def remove_track(self, track: dict, delete_file: bool = True) -> None:
        self.tracks.remove(track)
        for p in self.playlists:
            p["items"] = [i for i in p.get("items", []) if i.get("track_id") != track.get("track_id")]
        if delete_file:
            f = self.mount.joinpath(*track["Location"].strip(":").split(":"))
            f.unlink(missing_ok=True)

    def create_playlist(self, name: str, tracks: list[dict] = ()) -> dict:
        pl = {"Title": name, "playlist_id": secrets.randbits(63) | 1, "timestamp": int(time.time()),
              "sort_order": 1, "items": [{"track_id": t["track_id"]} for t in tracks]}
        self.playlists.append(pl)
        return pl

    def delete_playlist(self, playlist: dict) -> None:
        # User playlists are mirrored across datasets; drop every copy.
        self.playlists = [p for p in self.playlists if p.get("playlist_id") != playlist.get("playlist_id")]

    def _master(self) -> dict:
        return next(p for p in self.playlists if p.get("master_flag"))

    # ── write ───────────────────────────────────────────────────────────
    def snapshot(self) -> Path:
        # The first snapshot of a session is the state from before it touched anything. It is rotated apart from
        # the per-save ones, which a long batched sync would otherwise push out within minutes.
        first, self._snapshotted = not self._snapshotted, True
        dest = self.snapshot_root / (time.strftime("%Y%m%d-%H%M%S") + ("-start" if first else ""))
        n = 0
        while dest.exists():
            n += 1
            dest = dest.with_name(f"{dest.name.split('_')[0]}_{n}")
        # AppleDouble files and the engine's short-lived probe files are not part of the database
        shutil.copytree(self.itunes_dir, dest / "iTunes", copy_function=shutil.copyfile,
                        ignore=shutil.ignore_patterns("._*", ".iOpenPod_*"))
        if self.artwork_db.exists():  # the index only: thumbnails already in the .ithmb files are never rewritten
            shutil.copyfile(self.artwork_db, dest / "ArtworkDB")
        snapshots = sorted(p for p in self.snapshot_root.iterdir() if p.is_dir())
        for kind in (True, False):
            for old in [p for p in snapshots if ("-start" in p.name) == kind][:-self.keep_snapshots]:
                shutil.rmtree(old, ignore_errors=True)
        return dest

    def restore(self, snapshot: Path) -> None:
        for f in (snapshot / "iTunes").rglob("*"):
            if f.is_file() and not f.name.startswith("._"):
                target = self.itunes_dir / f.relative_to(snapshot / "iTunes")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, target)
        if (snapshot / "ArtworkDB").exists():
            shutil.copyfile(snapshot / "ArtworkDB", self.artwork_db)

    def save(self, artwork_sources: dict[int, str] | None = None) -> SaveReport:
        """Write the library. ``artwork_sources`` maps ``db_track_id`` to an audio file whose embedded cover
        becomes the track's artwork; tracks not listed keep the artwork they have."""
        if self._db_stamp() != self._loaded:
            # erased, restored or synced by something else meanwhile: what is in memory describes an iPod that is gone
            raise SaveError("the iPod's database changed since it was read (wiped or synced elsewhere?); run again")
        if self.checksum == ChecksumType.HASH72 and read_hash_info(str(self.mount)) is None:
            raise SaveError("nano 5G databases are signed with a HashInfo file, which this iPod lacks; "
                            "sync it once with iTunes to create it")
        snap = self.snapshot()
        expected = len(self.tracks)
        try:
            result = write_cached_itunesdb(str(self.mount), tracks_data=self.tracks, playlists_data=self.playlists,
                                           artwork_sources=artwork_sources or None)
            if not result.success:
                raise SaveError(f"engine refused the write: {result.error}")
            sigs = self.verify()
            if not all(sigs.values()):
                raise SaveError(f"written database fails {self.checksum.name} verification: {sigs}")
            self.reload()
            if len(self.tracks) != expected:
                raise SaveError(f"re-read {len(self.tracks)} tracks, expected {expected}")
        except BaseException:  # Ctrl-C included: a half-written database must not survive
            log.error("save failed; restoring database from %s", snap)
            self.restore(snap)
            self.reload()
            raise
        return SaveReport(tracks=expected, signatures=sigs, snapshot=snap)
