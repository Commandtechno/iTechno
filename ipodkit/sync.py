"""Make the iPod match a set of Spotify sources (playlists, liked songs, albums, artists, single tracks).

Each run is a fresh diff, so it is idempotent and resumable by construction:

  desired  = tracks of the chosen sources
  present  = iPod tracks whose Comment carries ``spotify:track:<id> … isrc:<ISRC>``
  missing  = desired - present  ->  download, transcode to AAC, tag, import

Spotify files the same recording under several track IDs (album, single,
compilation…); the ISRC identifies the recording itself. Desired tracks are
therefore grouped by ISRC, a group is imported once, and the iPod track's
Comment lists every ID of its group, so all of them resolve to that one file.

The iPod database is the source of truth for what has been imported. The only
extra state is a journal of file copies in flight: a file is journaled before
it is copied and cleared after the database save that references it, so a crash
in between leaves a known orphan that the next run removes. Downloads and
transcodes are kept until that save too, so an interrupted run never pays for
the same track twice.
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
import unicodedata
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .manager import IPod
from .oggify import Oggify, OggifyError

log = logging.getLogger("ipodkit.sync")

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".aif", ".aiff", ".flac", ".ogg", ".oga", ".opus", ".wma"}
NATIVE_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".aif", ".aiff"}  # iPods play these as-is
PLAYLIST_KINDS = {"playlist", "liked", "artist"}  # sources mirrored as an iPod playlist; the rest just add tracks
FREE_SPACE_MARGIN = 64 * 2**20
LYRICS_RECHECK = 30 * 86400  # how long "Spotify has no lyrics for this" is believed before asking again
_ID_RE = re.compile(r"(?:spotify:track:|open\.spotify\.com/track/)([0-9A-Za-z]{22})")
_BARE_ID_RE = re.compile(r"(?<![0-9A-Za-z])([0-9A-Za-z]{22})(?![0-9A-Za-z])")
_ISRC_RE = re.compile(r"\bisrc:([A-Z0-9]{12})\b")
_LINK_RE = re.compile(r"(?:spotify:|open\.spotify\.com/(?:intl-\w+/)?)(track|album|playlist|artist)[:/]([0-9A-Za-z]{22})")


def comment_for(spotify_ids: list[str], isrc: str | None) -> str:
    return " ".join([f"spotify:track:{sid}" for sid in spotify_ids] + ([f"isrc:{isrc}"] if isrc else []))


def spotify_ids_of(ipod_track: dict) -> list[str]:
    return _ID_RE.findall(str(ipod_track.get("Comment") or ""))


def isrc_of(ipod_track: dict) -> str | None:
    m = _ISRC_RE.search(str(ipod_track.get("Comment") or ""))
    return m.group(1) if m else None


def parse_link(text: str) -> tuple[str, str] | None:
    """``(kind, id)`` of a Spotify URL or URI."""
    m = _LINK_RE.search(text)
    return (m.group(1), m.group(2)) if m else None


# ── state ───────────────────────────────────────────────────────────────
class State:
    def __init__(self, root: Path):
        self.root = root
        for d in ("downloads", "transcoded", "covers"):
            (root / d).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "sync.db")
        self.db.executescript("""
            create table if not exists sources(kind text, id text, name text, added integer, primary key(kind, id));
            create table if not exists journal(dest text primary key, spotify_id text, created integer);
            create table if not exists tracks(id text primary key, meta text);
            create table if not exists mirrored(playlist text primary key);
            create table if not exists ipod_index(comment text);
            create table if not exists lyrics(id text primary key, text text, fetched integer);
        """)

    # what the user chose to keep on the iPod
    def sources(self) -> list[tuple[str, str, str]]:
        return self.db.execute("select kind, id, name from sources order by added, rowid").fetchall()

    def add_source(self, kind: str, spotify_id: str, name: str) -> None:
        with self.db:
            self.db.execute("insert into sources values(?,?,?,?) on conflict(kind, id) do update set name=excluded.name",
                            (kind, spotify_id, name, int(time.time())))

    def remove_source(self, kind: str, spotify_id: str) -> None:
        with self.db:
            self.db.execute("delete from sources where kind=? and id=?", (kind, spotify_id))

    def journal_add(self, dest: Path, spotify_id: str) -> None:
        with self.db:  # committed before the copy starts
            self.db.execute("insert or replace into journal values(?,?,?)", (str(dest), spotify_id, int(time.time())))

    def journal_clear(self, dests: list[Path]) -> None:
        with self.db:
            self.db.executemany("delete from journal where dest=?", [(str(d),) for d in dests])

    def journal(self) -> list[Path]:
        return [Path(r[0]) for r in self.db.execute("select dest from journal")]

    # track metadata does not change for an ID, so it is fetched once
    def cached_tracks(self, ids: list[str]) -> dict[str, dict]:
        found = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            rows = self.db.execute(f"select id, meta from tracks where id in ({','.join('?' * len(chunk))})", chunk)
            found.update({sid: json.loads(meta) for sid, meta in rows})
        return found

    def cache_tracks(self, tracks: list[dict]) -> None:
        with self.db:
            self.db.executemany("insert or replace into tracks values(?,?)", [(t["id"], json.dumps(t)) for t in tracks])

    # lyrics, and the absence of any, which is asked about again after a while: Spotify adds lyrics over time
    def cached_lyrics(self, ids: list[str]) -> dict[str, str | None]:
        found, fresh = {}, int(time.time()) - LYRICS_RECHECK
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            rows = self.db.execute(f"select id, text from lyrics where id in ({','.join('?' * len(chunk))}) "
                                   "and (text is not null or fetched > ?)", [*chunk, fresh])
            found.update(dict(rows))
        return found

    def cache_lyrics(self, lyrics: dict[str, str | None]) -> None:
        with self.db:
            self.db.executemany("insert or replace into lyrics values(?,?,?)",
                                [(sid, text, int(time.time())) for sid, text in lyrics.items()])

    # what the iPod held when last seen (the Comments of its synced tracks), to plan downloads while it is away
    def ipod_index(self) -> list[str]:
        return [r[0] for r in self.db.execute("select comment from ipod_index")]

    def save_ipod_index(self, ipod: IPod) -> None:
        with self.db:
            self.db.execute("delete from ipod_index")
            self.db.executemany("insert into ipod_index values(?)",
                                [(t["Comment"],) for t in ipod.tracks if spotify_ids_of(t)])

    # iPod playlists this tool created, so that it only ever deletes its own
    def mirrored(self) -> set[str]:
        return {r[0] for r in self.db.execute("select playlist from mirrored")}

    def set_mirrored(self, names: set[str]) -> None:
        with self.db:
            self.db.execute("delete from mirrored")
            self.db.executemany("insert into mirrored values(?)", [(n,) for n in names])


# ── inbox ───────────────────────────────────────────────────────────────
def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(.*?\)|\[.*?\]| - .*$|\bfeat\.?.*$|\bft\.?.*$", " ", s)  # drop "(Remastered)", " - Live", "feat. X"
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


@dataclass
class InboxFile:
    path: Path
    spotify_id: str | None = None
    isrc: str | None = None
    title: str = ""
    artist: str = ""
    duration_ms: int = 0


class Inbox:
    """Index of a folder of your own audio files, preferred over downloading when they match.

    A file is tied to a Spotify track by, in order of trust: an explicit ID (a
    ``spotify_id`` tag, a Spotify URI/URL in the comment, or an ID in the
    filename), its ISRC tag, or title + artist + duration within 3 seconds.
    """

    def __init__(self, folder: Path):
        from mutagen import File as MutagenFile

        self.files: list[InboxFile] = []
        for p in sorted(folder.rglob("*")) if folder.is_dir() else []:
            if p.suffix.lower() not in AUDIO_EXTS or p.name.startswith("."):
                continue
            try:
                audio = MutagenFile(p, easy=True)
            except Exception:
                audio = None
            if audio is None:
                continue

            def tag(key: str) -> str:
                try:
                    return str((audio.tags.get(key) or [""])[0]) if audio.tags else ""
                except KeyError:  # EasyID3 rejects keys it has no mapping for
                    return ""

            explicit = tag("spotify_id") or " ".join(tag(k) for k in ("comment", "description", "website"))
            m = (_BARE_ID_RE.fullmatch(explicit.strip()) or _ID_RE.search(explicit)
                 or _ID_RE.search(p.name) or _BARE_ID_RE.search(p.stem))
            self.files.append(InboxFile(
                path=p, spotify_id=m.group(1) if m else None, isrc=tag("isrc").upper().replace("-", "") or None,
                title=_norm(tag("title") or p.stem), artist=_norm(tag("artist")),
                duration_ms=int(audio.info.length * 1000)))
        self.by_id = {f.spotify_id: f for f in self.files if f.spotify_id}
        self.by_isrc = {f.isrc: f for f in self.files if f.isrc}

    def find(self, track: dict) -> InboxFile | None:
        if f := self.by_id.get(track["id"]):
            return f
        if track.get("isrc") and (f := self.by_isrc.get(track["isrc"])):
            return f
        title, artists = _norm(track["name"]), [_norm(a) for a in track["artists"]]
        for f in self.files:
            if (not f.spotify_id and f.title == title and abs(f.duration_ms - track["duration_ms"]) <= 3000
                    and any(a and (a in f.artist or f.artist in a) for a in artists)):
                return f
        return None


# ── files ───────────────────────────────────────────────────────────────
def transcode(src: Path, out: Path, bitrate: int) -> Path:
    """Transcode to AAC, which every iPod plays. Reuses a finished transcode of the same source."""
    if out.exists() and (not src.exists() or out.stat().st_mtime >= src.stat().st_mtime):
        return out
    tmp = out.with_suffix(".part.m4a")
    encoders = subprocess.run([paths.tool("ffmpeg"), "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    codec = "aac_at" if " aac_at " in encoders else "aac"  # Apple's encoder where available
    r = subprocess.run([paths.tool("ffmpeg"), "-y", "-loglevel", "error", "-i", str(src), "-vn", "-map_metadata", "-1",
                        "-c:a", codec, "-b:a", f"{bitrate}k", "-movflags", "+faststart", str(tmp)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed on {src.name}: {r.stderr.strip()[:200]}")
    tmp.replace(out)  # atomic: a killed transcode never looks finished
    return out


def fetch_cover(meta: dict, state: State) -> Path | None:
    if not meta.get("cover"):
        return None
    out = state.root / "covers" / f"{meta.get('album_id') or meta['id']}.jpg"
    if not out.exists():
        try:
            with urllib.request.urlopen(meta["cover"], timeout=20) as r:
                data = r.read()
            tmp = out.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(out)
        except OSError:
            return None  # artwork is a nicety, never a reason to fail a track
    return out


def tag(m4a: Path, meta: dict, cover: Path | None, comment: str, lyrics: str | None = None) -> None:
    """Make the file self-describing, so that it stays identifiable without the iPod database."""
    from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

    f = MP4(m4a)
    f["\xa9nam"], f["\xa9ART"], f["\xa9alb"] = meta["name"], ", ".join(meta["artists"]), meta["album"]
    f["aART"] = ", ".join(meta["album_artists"]) or ", ".join(meta["artists"])
    f["\xa9day"], f["\xa9cmt"] = meta["date"], comment
    f["trkn"] = [(meta["track_number"], meta.get("total_tracks") or 0)]
    f["disk"] = [(meta["disc_number"], meta.get("total_discs") or 0)]
    f["rtng"] = [1 if meta.get("explicit") else 0]
    f["cpil"] = meta.get("album_type") == "COMPILATION"
    if meta.get("copyright"):
        f["cprt"] = meta["copyright"]
    for key in ("isrc", "label"):
        if meta.get(key):
            f[f"----:com.apple.iTunes:{key.upper()}"] = MP4FreeForm(meta[key].encode())
    if cover:
        f["covr"] = [MP4Cover(cover.read_bytes(), MP4Cover.FORMAT_JPEG)]
    if lyrics:  # where the iPod reads them from; add_track sees them and sets the database flag that says so
        f["\xa9lyr"] = lyrics
    f.save()


def ipod_fields(meta: dict, comment: str) -> dict:
    released = calendar.timegm(time.strptime(meta["date"], "%Y-%m-%d")) if meta.get("year", 0) > 1970 else None
    return {"Comment": comment, "year": meta.get("year") or None, "date_released": released,
            "track_number": meta["track_number"], "total_tracks": meta.get("total_tracks"),
            "disc_number": meta["disc_number"], "total_discs": meta.get("total_discs"),
            "explicit_flag": 1 if meta.get("explicit") else None,
            "compilation_flag": 1 if meta.get("album_type") == "COMPILATION" else None}


# ── plan ────────────────────────────────────────────────────────────────
class Events:
    """Progress hooks; the CLI overrides them."""
    def phase(self, message: str) -> None: ...
    def metadata_progress(self, done: int, total: int) -> None: ...
    def import_start(self, index: int, total: int, meta: dict) -> None: ...
    def import_end(self, meta: dict, error: str | None) -> None: ...
    def lyrics_progress(self, done: int, total: int) -> None: ...
    def saving(self) -> None: ...


@dataclass
class Group:
    """Every desired Spotify ID of one recording."""
    sids: list[str]
    isrc: str | None
    meta: dict | None  # of the first ID that could be described
    lyrics: str | None = None


@dataclass
class Plan:
    playlists: list[tuple[str, list[str]]]  # iPod playlist name -> wanted track IDs, in order
    desired: list[str]
    groups: list[Group]
    present: list[Group] = field(default_factory=list)  # on the iPod already
    relabel: list[tuple[Group, str]] = field(default_factory=list)  # on the iPod, but its Comment is incomplete
    download: list[Group] = field(default_factory=list)
    unavailable: list[tuple[str, str]] = field(default_factory=list)  # (track ID, why)
    prunable: list[dict] = field(default_factory=list)  # synced iPod tracks that no source wants any more
    stale_playlists: list[str] = field(default_factory=list)  # mirrored playlists whose source is gone
    add_lyrics: list[Group] = field(default_factory=list)  # on the iPod without lyrics, which Spotify now has

    def download_bytes(self, bitrate: int) -> int:
        return sum(g.meta["duration_ms"] * bitrate // 8 for g in self.download)


class AbsentIPod:
    """Stands in for the iPod while it is not plugged in: what it held when last seen, read-only."""
    mount = None

    def __init__(self, state: State):
        self.tracks = [{"Comment": comment} for comment in state.ipod_index()]

    def find_playlist(self, name: str) -> None:
        return None


def ready_file(state: State, g: Group) -> Path:
    """Where the track of a group waits for the iPod, transcoded and tagged."""
    return state.root / "transcoded" / f"{g.sids[0]}.m4a"


def _index(ipod: IPod) -> tuple[dict[str, dict], dict[str, dict]]:
    by_sid, by_isrc = {}, {}
    for t in ipod.tracks:
        for sid in spotify_ids_of(t):
            by_sid.setdefault(sid, t)
        if isrc := isrc_of(t):
            by_isrc.setdefault(isrc, t)
    return by_sid, by_isrc


def _on_ipod(g: Group, by_sid: dict[str, dict], by_isrc: dict[str, dict]) -> dict | None:
    return next((by_sid[s] for s in g.sids if s in by_sid), None) or (by_isrc.get(g.isrc) if g.isrc else None)


def _prunable(ipod: IPod, plan: Plan) -> list[dict]:
    wanted_sids, wanted_isrcs = set(plan.desired), {g.isrc for g in plan.groups if g.isrc}
    return [t for t in ipod.tracks if (sids := spotify_ids_of(t))
            and not wanted_sids.intersection(sids) and isrc_of(t) not in wanted_isrcs]


def _lyrics_id(g: Group) -> str:
    return g.meta["id"] if g.meta else g.sids[0]


def _plan_lyrics(plan: Plan, ipod: IPod | AbsentIPod, state: State, og: Oggify, events: Events) -> None:
    """Find lyrics for what is to be downloaded, and for synced iPod tracks that have none yet."""
    by_sid, by_isrc = _index(ipod)
    backfill = [g for g in plan.present + [g for g, _ in plan.relabel] if (t := _on_ipod(g, by_sid, by_isrc))
                and not t.get("lyrics_flag") and str(t.get("Location", "")).lower().endswith(".m4a")]
    wanted = [g for g in plan.download + backfill if (g.meta or {}).get("has_lyrics") is not False]
    found = state.cached_lyrics([_lyrics_id(g) for g in wanted])
    unknown = list(dict.fromkeys(_lyrics_id(g) for g in wanted if _lyrics_id(g) not in found))
    if unknown:
        events.phase(f"Fetching lyrics for {len(unknown)} tracks")
        try:
            fetched = og.lyrics(unknown, lambda done: events.metadata_progress(done, len(unknown)))
        except OggifyError:  # lyrics are a nicety: never the reason a sync does not happen
            if og.proc.poll() is not None:
                raise
            log.exception("lyrics lookup failed")
            fetched = {}
        state.cache_lyrics(fetched)
        found.update(fetched)
    for g in wanted:
        g.lyrics = found.get(_lyrics_id(g))
    plan.add_lyrics = [g for g in backfill if g.lyrics]


def make_plan(ipod: IPod | AbsentIPod, state: State, og: Oggify, events: Events = Events(), *,
              lyrics: bool = True) -> Plan:
    """Resolve the sources and diff them against the iPod. Touches nothing.

    Any source that fails to resolve aborts the run: a partial view of what is
    wanted must never reach the pruning step.
    """
    if isinstance(ipod, IPod):
        state.save_ipod_index(ipod)
    events.phase("Reading your sources from Spotify")
    playlists, desired = [], {}
    for kind, sid, name in state.sources():
        source = og.source(kind, sid)
        if kind in PLAYLIST_KINDS:
            playlists.append((source["name"] or name, source["tracks"]))
        if source["name"] and source["name"] != name:
            state.add_source(kind, sid, source["name"])  # renamed on Spotify
        desired.update(dict.fromkeys(source["tracks"]))
    desired = list(desired)

    by_sid, by_isrc = _index(ipod)
    meta = state.cached_tracks(desired)
    unknown = [sid for sid in desired if sid not in meta and sid not in by_sid]
    if unknown:
        events.phase(f"Fetching metadata for {len(unknown)} tracks")
        fetched = og.tracks(unknown, lambda done: events.metadata_progress(done, len(unknown)))
        state.cache_tracks([t for t in fetched.values() if "error" not in t])
        meta.update(fetched)

    grouped: dict[str, Group] = {}
    for sid in desired:
        m = meta.get(sid) if "error" not in meta.get(sid, {}) else None
        isrc = (m or {}).get("isrc") or (isrc_of(by_sid[sid]) if sid in by_sid else None)
        g = grouped.setdefault(isrc or sid, Group([], isrc, None))
        g.sids.append(sid)
        g.meta = g.meta or m

    plan = Plan(playlists, desired, list(grouped.values()))
    for g in plan.groups:
        if t := _on_ipod(g, by_sid, by_isrc):
            comment = comment_for(list(dict.fromkeys(spotify_ids_of(t) + g.sids)), g.isrc or isrc_of(t))
            plan.present.append(g) if comment == t.get("Comment") else plan.relabel.append((g, comment))
        elif g.meta:
            plan.download.append(g)
        else:
            plan.unavailable += [(s, meta.get(s, {}).get("error", "no metadata")) for s in g.sids]
    plan.prunable = _prunable(ipod, plan)
    if lyrics:
        _plan_lyrics(plan, ipod, state, og, events)
    names = {name for name, _ in playlists}
    plan.stale_playlists = sorted(n for n in state.mirrored() - names if ipod.find_playlist(n))
    log.info("plan: sources=%s wanted=%d recordings=%d present=%d relabel=%d download=%d unavailable=%s prunable=%d "
             "stale_playlists=%s lyrics=%d add_lyrics=%d", state.sources(), len(desired), len(plan.groups),
             len(plan.present), len(plan.relabel), len(plan.download), plan.unavailable, len(plan.prunable),
             plan.stale_playlists, sum(1 for g in plan.download if g.lyrics), len(plan.add_lyrics))
    return plan


# ── sync ────────────────────────────────────────────────────────────────
class IPodFull(RuntimeError):
    pass


class IPodGone(RuntimeError):
    """The iPod was unplugged or ejected mid-run. Nothing is lost: the copies in flight stay journaled, so the
    next run removes what the database never got to reference, and finished downloads are kept."""


@dataclass
class Report:
    imported: list[str] = field(default_factory=list)
    from_inbox: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    orphans_removed: int = 0
    lyrics_added: int = 0  # to tracks already on the iPod
    ipod_full: bool = False
    playlists: dict[str, tuple[int, int]] = field(default_factory=dict)  # name -> (on iPod, in playlist)


@dataclass
class _Pending:
    dest: Path  # on-device file
    group: Group
    track: dict
    art_source: Path
    temps: list[Path]


def describe(meta: dict) -> str:
    return f"{', '.join(meta['artists'])} — {meta['name']}"


def recover(ipod: IPod, state: State) -> int:
    """Undo file copies that an interrupted run left in flight."""
    referenced = {str(ipod.mount.joinpath(*t["Location"].strip(":").split(":"))) for t in ipod.tracks}
    removed = 0
    for dest in state.journal():
        if str(dest) not in referenced and dest.exists():
            dest.unlink()
            removed += 1
    state.journal_clear(state.journal())
    return removed


def embed_lyrics(ipod: IPod, state: State, track: dict, lyrics: str) -> None:
    """Add lyrics to the file of a track already on the iPod, and flag them in the (unsaved) database.

    The file is tagged as a copy that then replaces it, so it is never half-written; the copy is journaled, so
    that a crash leaves no orphan. Until the next save the database records the old size, which a crash would
    leave behind, but the flag is not set either then, so the next run simply does it again.
    """
    from mutagen.mp4 import MP4

    f = ipod.mount.joinpath(*track["Location"].strip(":").split(":"))
    if shutil.disk_usage(ipod.mount).free - f.stat().st_size < FREE_SPACE_MARGIN:
        raise IPodFull
    tmp = f.with_name(f"{f.stem}.lyrics{f.suffix}")
    state.journal_add(tmp, "")
    try:
        shutil.copyfile(f, tmp)
        m = MP4(tmp)
        m["\xa9lyr"] = lyrics
        m.save()
        os.replace(tmp, f)
    finally:
        tmp.unlink(missing_ok=True)
        state.journal_clear([tmp])
    track["size"], track["lyrics_flag"] = f.stat().st_size, 1


def _prepare(ogg: Path, g: Group, state: State, bitrate: int, artwork: bool) -> Path:
    """Downloaded Ogg -> tagged AAC, ready for the iPod. The Ogg is not needed after that."""
    m4a = transcode(ogg, ready_file(state, g), bitrate)
    tag(m4a, g.meta, fetch_cover(g.meta, state) if artwork else None, comment_for(g.sids, g.isrc), g.lyrics)
    ogg.unlink(missing_ok=True)
    return m4a


def fetch(state: State, og: Oggify, plan: Plan, *, bitrate: int = 256, artwork: bool = True,
          events: Events = Events()) -> Report:
    """Download and prepare everything the plan wants, without touching the iPod (it need not even be there).

    The next ``run`` finds the prepared files and only has to copy them. Spotify's rate limit makes downloading
    the slow part by far, so this is the part worth leaving running.
    """
    rep = Report()
    todo = [g for g in plan.download if not ready_file(state, g).exists()]
    log.info("fetch: %d to download, %d ready already", len(todo), len(plan.download) - len(todo))
    index = 0
    while todo:
        by_first_sid = {g.sids[0]: g for g in todo}
        jobs = [(sid, state.root / "downloads" / f"{sid}.ogg") for sid in by_first_sid]
        failed: list[tuple[Group, str]] = []
        for sid, ogg, error in og.downloads(jobs):
            g, index = by_first_sid[sid], index + 1
            events.import_start(index, len(plan.download), g.meta)
            try:
                if error:
                    raise RuntimeError(error)
                _prepare(ogg, g, state, bitrate, artwork)
                log.info("fetched %s %s", g.sids, describe(g.meta))
                rep.imported.append(describe(g.meta))
            except (OSError, RuntimeError, ValueError) as e:
                log.exception("failed %s %s", g.sids, describe(g.meta))
                failed.append((g, str(e)))
                error = str(e)
            events.import_end(g.meta, error)
        if len(failed) == len(todo):  # a pass without any progress: leave the rest to the next run
            rep.failed = [(describe(g.meta), why) for g, why in failed]
            break
        todo = [g for g, _ in failed]  # hours-long runs meet hiccups (a dropped session…): go again for those
    log.info("fetch done: fetched=%d failed=%d", len(rep.imported), len(rep.failed))
    return rep


def run(ipod: IPod, state: State, og: Oggify, plan: Plan, *, prune: bool = False, inbox_dir: Path | None = None,
        batch: int = 20, bitrate: int = 256, artwork: bool = True, events: Events = Events()) -> Report:
    rep = Report(orphans_removed=recover(ipod, state))
    log.info("run: prune=%s inbox=%s batch=%d bitrate=%d artwork=%s orphans_removed=%d", prune, inbox_dir, batch,
             bitrate, artwork, rep.orphans_removed)
    pending: list[_Pending] = []

    def flush() -> None:
        if not pending:
            return
        events.saving()
        log.info("saving a batch of %d: %s", len(pending), [p.group.sids[0] for p in pending])
        keep_journal = False
        try:
            ipod.save(artwork_sources={p.track["db_track_id"]: str(p.art_source) for p in pending} if artwork else None)
        except Exception as e:  # database was rolled back; undo this batch's copies too
            log.exception("batch save failed")
            if not ipod.itunes_dir.is_dir():
                keep_journal = True  # the copies cannot be undone now: the next run does it, from the journal
                raise IPodGone from e
            for p in pending:
                p.dest.unlink(missing_ok=True)
                rep.failed.append((describe(p.group.meta), f"database save failed: {e}"))
            raise
        finally:
            if not keep_journal:
                state.journal_clear([p.dest for p in pending])
            saved, pending[:] = list(pending), []
        for p in saved:
            rep.imported.append(describe(p.group.meta))
            for f in p.temps:  # the iPod has it now
                f.unlink(missing_ok=True)

    def add(g: Group, src: Path, temps: list[Path]) -> None:
        if shutil.disk_usage(ipod.mount).free - src.stat().st_size < FREE_SPACE_MARGIN:
            raise IPodFull
        m, comment = g.meta, comment_for(g.sids, g.isrc)
        dest = ipod.alloc_path(src.suffix)
        state.journal_add(dest, g.sids[0])
        track = ipod.add_track(src, dest=dest, title=m["name"], artist=", ".join(m["artists"]), album=m["album"],
                               album_artist=", ".join(m["album_artists"]) or None, extra=ipod_fields(m, comment))
        pending.append(_Pending(dest, g, track, src, temps))

    def attempt(g: Group, index: int, work: Callable[[], None]) -> None:
        events.import_start(index, len(plan.download), g.meta)
        try:
            work()
            log.info("imported %s %s", g.sids, describe(g.meta))
            events.import_end(g.meta, None)
        except IPodFull:
            log.warning("iPod full at %s", g.sids)
            rep.ipod_full = True
            events.import_end(g.meta, "the iPod is full")
        except (OSError, RuntimeError, ValueError) as e:
            log.exception("failed %s %s", g.sids, describe(g.meta))
            if not ipod.itunes_dir.is_dir():
                raise IPodGone from e
            rep.failed.append((describe(g.meta), str(e)))
            events.import_end(g.meta, str(e))
        if len(pending) >= batch:  # outside the try: a failed save is not this track's failure, it ends the run
            flush()

    # your own files first, Spotify for the rest
    inbox = Inbox(inbox_dir) if inbox_dir and plan.download else None
    to_download, index = [], 0
    for g in plan.download:
        if rep.ipod_full:
            break
        if inbox and (f := inbox.find(g.meta)):
            index += 1
            out = state.root / "transcoded" / f"{g.sids[0]}.m4a"
            native = f.path.suffix.lower() in NATIVE_EXTS
            attempt(g, index, lambda: add(g, f.path, []) if native else add(g, transcode(f.path, out, bitrate), [out]))
            rep.from_inbox += 1
        else:
            to_download.append(g)

    # then what an earlier `fetch` (or an interrupted run) has prepared already: just a copy
    for g in [g for g in to_download if ready_file(state, g).exists()]:
        if rep.ipod_full:
            break
        to_download.remove(g)
        index += 1
        m4a = ready_file(state, g)
        # tagged again: the group may have grown since the file was prepared
        attempt(g, index, lambda: (tag(m4a, g.meta, fetch_cover(g.meta, state) if artwork else None,
                                       comment_for(g.sids, g.isrc), g.lyrics), add(g, m4a, [m4a])))

    by_first_sid = {g.sids[0]: g for g in to_download}
    jobs = [(sid, state.root / "downloads" / f"{sid}.ogg") for sid in by_first_sid]
    for sid, ogg, error in og.downloads(jobs) if not rep.ipod_full else ():
        g, index = by_first_sid[sid], index + 1

        def work() -> None:
            if error:
                raise RuntimeError(error)
            m4a = _prepare(ogg, g, state, bitrate, artwork)
            add(g, m4a, [ogg, m4a])

        attempt(g, index, work)
        if rep.ipod_full:
            break
    flush()

    # lyrics for tracks synced before, relabelling, playlists and pruning ride in one final save
    dirty = False
    by_sid, by_isrc = _index(ipod)
    for i, g in enumerate(plan.add_lyrics if not rep.ipod_full else []):
        events.lyrics_progress(i, len(plan.add_lyrics))
        if not (t := _on_ipod(g, by_sid, by_isrc)) or t.get("lyrics_flag"):
            continue
        try:
            embed_lyrics(ipod, state, t, g.lyrics)
            rep.lyrics_added, dirty = rep.lyrics_added + 1, True
        except IPodFull:
            log.warning("iPod full while adding lyrics")
            rep.ipod_full = True
            break
        except Exception as e:  # mutagen raises its own errors on odd files; a track without lyrics is no failure
            if not ipod.itunes_dir.is_dir():
                raise IPodGone from e
            log.exception("could not add lyrics to %s", t.get("Location"))
    for g, comment in plan.relabel:
        if t := _on_ipod(g, by_sid, by_isrc):
            t["Comment"], dirty = comment, True
    by_sid, _ = _index(ipod)
    for name, sids in plan.playlists:
        want = list({t["track_id"]: t for s in sids if (t := by_sid.get(s))}.values())  # one entry per recording
        rep.playlists[name] = (len(want), len(sids))
        have = ipod.find_playlist(name)
        if have is None or [i.get("track_id") for i in have.get("items", [])] != [t["track_id"] for t in want]:
            ipod.set_playlist_tracks(name, want)
            dirty = True
    mirrored = {name for name, _ in plan.playlists}
    doomed: list[Path] = []
    if prune:
        for t in _prunable(ipod, plan):
            rep.pruned.append(f"{t.get('Artist')} — {t.get('Title')}")
            doomed.append(ipod.mount.joinpath(*t["Location"].strip(":").split(":")))
            ipod.remove_track(t, delete_file=False)
            dirty = True
        for name in plan.stale_playlists:
            if pl := ipod.find_playlist(name):
                ipod.delete_playlist(pl)
                dirty = True
    else:
        mirrored |= state.mirrored()  # still ours, to be removed by a later --prune
    if dirty:
        events.saving()
        ipod.save()
    state.set_mirrored(mirrored)
    for f in doomed:  # only once the database no longer references them
        f.unlink(missing_ok=True)
    on_ipod, _ = _index(ipod)
    for f in [*(state.root / "downloads").iterdir(), *(state.root / "transcoded").iterdir()]:
        if f.name.split(".")[0] in on_ipod:  # kept by a run that was killed between its save and its cleanup
            f.unlink()
    state.save_ipod_index(ipod)
    log.info("done: imported=%d from_inbox=%d failed=%d pruned=%d lyrics_added=%d full=%s playlists=%s",
             len(rep.imported), rep.from_inbox, len(rep.failed), len(rep.pruned), rep.lyrics_added, rep.ipod_full,
             rep.playlists)
    return rep
