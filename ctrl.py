#!/usr/bin/env python3
"""iPod library manager — proof of concept.

    uv run ipod.py info
    uv run ipod.py list [QUERY]
    uv run ipod.py playlists
    uv run ipod.py verify
    uv run ipod.py add FILE [--title T --artist A --album B] [--playlist NAME]
    uv run ipod.py edit QUERY [--title T --artist A --album B --genre G --rating 0-5]
    uv run ipod.py remove QUERY
    uv run ipod.py playlist-create NAME [QUERY ...]
    uv run ipod.py playlist-delete NAME
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from ipodkit.host import find_ipods
from ipodkit.manager import IPod


def find_mount() -> Path:
    hits = find_ipods()
    if len(hits) != 1:
        sys.exit(f"Expected exactly one mounted iPod, found {len(hits)}; pass --mount")
    return hits[0]


def one_track(ipod: IPod, query: str) -> dict:
    hits = ipod.find_tracks(query)
    if len(hits) != 1:
        for t in hits[:10]:
            print(f"   {t.get('Title')} — {t.get('Artist')}")
        sys.exit(f"'{query}' matched {len(hits)} tracks; need exactly 1")
    return hits[0]


def save(ipod: IPod) -> None:
    r = ipod.save()
    print(f"saved: {r.tracks} tracks | signatures {r.signatures} | pre-write snapshot: {r.snapshot}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mount", type=Path)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info"); sub.add_parser("playlists"); sub.add_parser("verify")
    sub.add_parser("list").add_argument("query", nargs="?", default="")
    p = sub.add_parser("add"); p.add_argument("file", type=Path); p.add_argument("--playlist")
    e = sub.add_parser("edit"); e.add_argument("query"); e.add_argument("--rating", type=int, choices=range(6))
    for q in (p, e):
        for f in ("title", "artist", "album", "genre"):
            q.add_argument(f"--{f}")
    sub.add_parser("remove").add_argument("query")
    c = sub.add_parser("playlist-create"); c.add_argument("name"); c.add_argument("queries", nargs="*")
    sub.add_parser("playlist-delete").add_argument("name")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO if a.verbose else logging.CRITICAL)
    ipod = IPod(a.mount or find_mount())
    d = ipod.device

    if a.cmd == "info":
        print(f"{d.ipod_name}: {d.model_family} {d.generation} {d.capacity} {d.color} ({d.model_number})")
        print(f"serial {d.serial} | firmware {d.firmware} | FireWire ID {d.firewire_guid} | mount {ipod.mount}")
        print(f"{len(ipod.tracks)} tracks, {len(ipod.user_playlists())} user playlists")
    elif a.cmd == "list":
        for t in sorted(ipod.find_tracks(a.query), key=lambda t: (str(t.get("Artist")), str(t.get("Album")), t.get("track_number", 0))):
            stars = "★" * (t.get("rating", 0) // 20)
            print(f"{str(t.get('Artist'))[:28]:28}  {str(t.get('Title'))[:40]:40}  {str(t.get('Album'))[:30]:30} {stars}")
    elif a.cmd == "playlists":
        for pl in ipod.user_playlists():
            kind = "smart" if pl.get("smart_playlist_rules") else "plain"
            print(f"{pl.get('Title'):30} {kind:6} {len(pl.get('items', []))} tracks")
    elif a.cmd == "verify":
        sigs = ipod.verify()
        print(sigs)
        sys.exit(0 if all(sigs.values()) else 1)
    elif a.cmd == "add":
        t = ipod.add_track(a.file, title=a.title, artist=a.artist, album=a.album, genre=a.genre)
        if a.playlist:
            pl = ipod.find_playlist(a.playlist) or ipod.create_playlist(a.playlist)
            ipod.add_to_playlist(pl, t)
        print(f"added '{t['Title']}' -> {t['Location']}")
        save(ipod)
    elif a.cmd == "edit":
        t = one_track(ipod, a.query)
        for f, key in (("title", "Title"), ("artist", "Artist"), ("album", "Album"), ("genre", "Genre")):
            if getattr(a, f) is not None:
                t[key] = getattr(a, f)
        if a.rating is not None:
            t["rating"] = a.rating * 20
        save(ipod)
    elif a.cmd == "remove":
        t = one_track(ipod, a.query)
        ipod.remove_track(t)
        print(f"removed '{t.get('Title')}'")
        save(ipod)
    elif a.cmd == "playlist-create":
        pl = ipod.create_playlist(a.name, [one_track(ipod, q) for q in a.queries])
        print(f"created '{a.name}' with {len(pl['items'])} tracks")
        save(ipod)
    elif a.cmd == "playlist-delete":
        pl = ipod.find_playlist(a.name) or sys.exit(f"no user playlist named '{a.name}'")
        ipod.delete_playlist(pl)
        save(ipod)


if __name__ == "__main__":
    main()
