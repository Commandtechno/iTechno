#!/usr/bin/env python3
"""Put your Spotify music on the iPod, and keep it in step.

    uv run sync.py                     interactive: pick playlists, search, sync
    uv run sync.py playlists           choose among your playlists and Liked Songs
    uv run sync.py search QUERY        find tracks; add them, their albums or their artists
    uv run sync.py add LINK|liked ...  add by Spotify link/URI (playlist, album, artist, track)
    uv run sync.py sources             what is kept on the iPod
    uv run sync.py remove [NAME ...]   stop keeping something (interactive without names)
    uv run sync.py run [--prune] [--dry-run] [--inbox DIR] [--bitrate 256] [--no-artwork]
    uv run sync.py fetch               download now, sync later: the iPod does not need to be plugged in
    uv run sync.py status

The first Spotify command asks you to pick "Oggify" in the devices menu of a
Spotify app on this network; that logs you in, once. Syncing is safe to
interrupt and re-run: it always continues from what the iPod already has.

Spotify limits downloads to a burst of ~25 tracks and then one every ~30s, so a
big library takes hours. `fetch` is the part to leave running: `run` then only
copies what is ready.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from functools import cached_property
from pathlib import Path

import questionary
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn
from rich.table import Table

from ipodkit import host, sync
from ipodkit.manager import SaveError
from ipodkit.oggify import Oggify, OggifyError

ROOT = Path(__file__).resolve().parent
KIND_LABELS = {"playlist": "playlist", "liked": "liked songs", "album": "album", "artist": "artist", "track": "track"}
console = Console(highlight=False)


def duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{minutes // 60}h {minutes % 60:02}m" if minutes >= 60 else f"{max(minutes, 1)} min"


def ask(question):
    """Ask, ignoring what was typed before the question showed: keys pressed during a long sync would otherwise
    be replayed into it, and answer in the user's place."""
    try:
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)  # what the terminal still holds
    except (ImportError, OSError, ValueError):  # not a terminal, or not a Unix one
        pass
    try:
        from prompt_toolkit.input.defaults import create_input
        from prompt_toolkit.input.typeahead import clear_typeahead
        clear_typeahead(create_input())  # what the previous prompt read ahead, and keeps for the next one
    except Exception:  # never worth failing a prompt over
        pass
    return question.ask()


def stay_awake() -> None:
    host.stay_awake()


def size(n: float) -> str:
    return f"{n / 2**30:.1f} GB" if n >= 2**30 else f"{n / 2**20:.0f} MB"


class App:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.state = sync.State(args.state)

    @cached_property
    def og(self) -> Oggify:
        og = Oggify()
        console.print(f"[green]●[/] Spotify: connected as [bold]{og.username}[/]")
        return og

    def mounts(self) -> list[Path]:
        if self.args.mount:
            return [self.args.mount] if (self.args.mount / "iPod_Control" / "iTunes").is_dir() else []
        return host.find_ipods()

    @cached_property
    def ipod(self):
        from ipodkit.manager import IPod

        hits = self.mounts()
        if len(hits) != 1:
            sys.exit(f"Expected exactly one mounted iPod, found {len(hits)}. Plug it in, or pass --mount.")
        mount = hits[0]
        ipod = IPod(mount, snapshot_root=self.args.snapshots)
        d = ipod.device
        console.print(f"[green]●[/] iPod: [bold]{d.ipod_name}[/] ({d.model_family} {d.generation}), "
                      f"{len(ipod.tracks)} tracks, {size(shutil.disk_usage(mount).free)} free")
        return ipod

    # ── choosing what to keep ───────────────────────────────────────────
    def add(self, kind: str, spotify_id: str, name: str | None = None) -> None:
        name = name or self.og.source(kind, spotify_id)["name"] or sync.describe(self.og.tracks([spotify_id])[spotify_id])
        self.state.add_source(kind, spotify_id, name)
        console.print(f"  [green]+[/] {KIND_LABELS[kind]}: {name}")

    def remove(self, kind: str, spotify_id: str, name: str) -> None:
        self.state.remove_source(kind, spotify_id)
        console.print(f"  [red]-[/] {KIND_LABELS[kind]}: {name}")

    def pick_playlists(self) -> None:
        mine = self.og.playlists()
        kept = {(k, i) for k, i, _ in self.state.sources()}
        choices = [questionary.Choice("♥ Liked Songs", ("liked", "", "Liked Songs"), checked=("liked", "") in kept)]
        choices += [questionary.Choice(f"{p['name']}  ({p['length']} tracks, by {p['owner']})",
                                       ("playlist", p["id"], p["name"]), checked=("playlist", p["id"]) in kept)
                    for p in mine]
        picked = ask(questionary.checkbox("Playlists to keep on the iPod (type to filter, space to toggle)",
                                      choices=choices, use_search_filter=True, use_jk_keys=False))
        if picked is None:
            return
        offered = {c.value[:2] for c in choices}
        for kind, sid, name in picked:
            if (kind, sid) not in kept:
                self.add(kind, sid, name)
        for kind, sid, name in self.state.sources():
            if (kind, sid) in offered and (kind, sid) not in {p[:2] for p in picked}:
                self.remove(kind, sid, name)

    def search(self, query: str | None = None) -> None:
        query = query or ask(questionary.text("Search Spotify for"))
        if not query:
            return
        found = self.og.tracks(self.og.search(query))
        recordings: dict[str, dict] = {}
        for t in found.values():  # one line per recording, best match first
            if "error" not in t:
                recordings.setdefault(t.get("isrc") or t["id"], t)
        tracks = list(recordings.values())
        if not tracks:
            return console.print("  nothing found")
        picked = ask(questionary.checkbox("Results (space to toggle)", choices=[
            questionary.Choice(f"{sync.describe(t)}  [{t['album']}, {t['year']}]{' 🅴' if t['explicit'] else ''}", t)
            for t in tracks]))
        if not picked:
            return
        what = ask(questionary.select("Add", choices=[
            questionary.Choice("just these tracks", "track"), questionary.Choice("their full albums", "album"),
            questionary.Choice("their artists' top tracks", "artist")]))
        for t in picked if what else []:
            if what == "track":
                self.add("track", t["id"], sync.describe(t))
            elif what == "album":
                self.add("album", t["album_id"], f"{', '.join(t['album_artists'])} — {t['album']}")
            else:
                self.add("artist", t["artist_ids"][0])

    def add_links(self, links: list[str]) -> None:
        for link in links:
            if link.lower() == "liked":
                self.add("liked", "", "Liked Songs")
            elif parsed := sync.parse_link(link):
                self.add(*parsed)
            else:
                console.print(f"  [red]?[/] not a Spotify playlist/album/artist/track link: {link}")

    def show_sources(self) -> None:
        table = Table("kind", "name", box=None, header_style="dim")
        for kind, _, name in self.state.sources():
            table.add_row(KIND_LABELS[kind], name)
        console.print(table if table.row_count else "  nothing yet: try `playlists`, `search` or `add`")

    def remove_sources(self, names: list[str]) -> None:
        sources = self.state.sources()
        if names:
            doomed = [s for s in sources if s[2] in names or s[:2] in map(sync.parse_link, names)
                      or (s[0] == "liked" and "liked" in names)]
        else:
            doomed = ask(questionary.checkbox("Stop keeping", choices=[
                questionary.Choice(f"{KIND_LABELS[k]}: {n}", (k, i, n)) for k, i, n in sources])) or []
        for source in doomed:
            self.remove(*source)
        if doomed:
            console.print("  their tracks leave the iPod on the next sync with --prune")

    # ── syncing ─────────────────────────────────────────────────────────
    def sync(self, *, prune: bool, dry_run: bool, confirm: bool, fetch_only: bool = False) -> None:
        a = self.args
        if not self.state.sources():
            return console.print("Nothing to sync yet: add something first (`playlists`, `search` or `add`).")
        if fetch_only and len(self.mounts()) != 1:
            ipod = sync.AbsentIPod(self.state)
            console.print(f"[yellow]●[/] iPod: not plugged in; going by the {len(ipod.tracks)} synced tracks it had "
                          "when last seen")
        else:
            ipod = self.ipod
        og = self.og
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(),
                      console=console, transient=True) as bar:
            task = bar.add_task("Reading your sources from Spotify", total=None)

            class Planning(sync.Events):
                def phase(self, message):
                    bar.update(task, description=message)

                def metadata_progress(self, done, total):
                    bar.update(task, completed=done, total=total)

            plan = sync.make_plan(ipod, self.state, og, Planning())

        ready = [g for g in plan.download if sync.ready_file(self.state, g).exists()]
        downloads = len(plan.download) - len(ready)
        need = plan.download_bytes(a.bitrate)
        free = shutil.disk_usage(ipod.mount).free if ipod.mount else None
        table = Table(box=None, show_header=False)
        table.add_row("wanted", f"{len(plan.desired)} tracks, {len(plan.groups)} distinct recordings (by ISRC)")
        table.add_row("on the iPod", str(len(plan.present) + len(plan.relabel)))
        if ready:
            table.add_row("downloaded already", f"{len(ready)}  (ready to copy)")
        table.add_row("to download", f"{downloads}" + (f"  (the iPod needs about {size(need)}; {size(free)} free)"
                                                       if free is not None and plan.download else ""))
        if downloads:
            # Spotify hands out a burst of download keys, then one per interval: that, not the network, sets the pace
            limits = og.limits()
            burst = limits["available"] if limits["available"] is not None else 20
            eta = downloads * 3 + max(0, downloads - burst) * limits["interval"]
            table.add_row("time", f"about {duration(eta)}" + (
                f"  (Spotify allows ~{burst} tracks right away, then one every {limits['interval']:.0f}s)"
                if downloads > burst else ""))
        if plan.unavailable:
            table.add_row("not resolved", f"{len(plan.unavailable)}  (no metadata from Spotify; retried by the next run)")
        if plan.prunable or plan.stale_playlists:
            table.add_row("no longer wanted", f"{len(plan.prunable)} tracks, {len(plan.stale_playlists)} playlists "
                          + ("(will be removed)" if prune else "(kept; use --prune to remove)"))
        console.print(table)
        if free is not None and need > free:
            console.print("[yellow]That is more than fits: the sync will stop when the iPod is full.[/]")
        if dry_run:
            for g in plan.download:
                if g not in ready:
                    console.print(f"  [dim]would download[/] {sync.describe(g.meta)}")
            for t in plan.prunable if prune else []:
                console.print(f"  [dim]would remove[/] {t.get('Artist')} — {t.get('Title')}")
            return
        if fetch_only and not downloads:
            return console.print(f"Nothing left to download: {len(ready)} tracks are ready, sync to copy them over."
                                 if ready else "Nothing to download: the iPod has it all.")
        if confirm and (plan.download or (prune and plan.prunable)) and not ask(questionary.confirm("Go?")):
            return
        stay_awake()

        with Progress(SpinnerColumn(), TextColumn("{task.description}", table_column=None), BarColumn(),
                      MofNCompleteColumn(), TimeRemainingColumn(), TextColumn("[dim]{task.fields[note]}"),
                      console=console, transient=True) as bar:
            task = bar.add_task("Starting", total=downloads if fetch_only else len(plan.download), note="")
            og.on_waiting = lambda seconds: bar.update(
                task, note=f"Spotify's rate limit: next download in ~{seconds:.0f}s")

            class Syncing(sync.Events):
                def import_start(self, index, total, meta):
                    bar.update(task, description=sync.describe(meta)[:60])

                def import_end(self, meta, error):
                    bar.update(task, advance=1, note="")
                    mark = f"[red]✗[/] {sync.describe(meta)}: {error}" if error else f"[green]✓[/] {sync.describe(meta)}"
                    bar.console.print("  " + mark)

                def saving(self):
                    bar.update(task, description="Writing the iPod database")

            try:
                if fetch_only:
                    rep = sync.fetch(self.state, og, plan, bitrate=a.bitrate, artwork=not a.no_artwork,
                                     events=Syncing())
                else:
                    rep = sync.run(ipod, self.state, og, plan, prune=prune, inbox_dir=a.inbox, batch=a.batch,
                                   bitrate=a.bitrate, artwork=not a.no_artwork, events=Syncing())
            except KeyboardInterrupt:
                bar.stop()
                sys.exit("Interrupted. Run it again to continue: nothing already downloaded or synced is lost.")
            except sync.IPodGone:
                bar.stop()
                sys.exit("The iPod is gone (unplugged or ejected?). Plug it back in and run this again: everything "
                         "saved so far stays, and finished downloads are kept.")
            except SaveError as e:
                bar.stop()
                sys.exit(f"The iPod database could not be written, and was rolled back: {e}\n"
                         f"Finished downloads are kept, so the next run picks up from here. Details: {a.state / 'sync.log'}")

        if fetch_only:
            console.print(f"\n[bold]downloaded {len(rep.imported)}[/], failed {len(rep.failed)}")
            for name, why in rep.failed:
                console.print(f"  [yellow]failed[/] {name}: {why}")
            return console.print("  Plug the iPod in and sync to copy them over; `fetch` again retries failures.")
        console.print(f"\n[bold]imported {len(rep.imported)}[/] ({rep.from_inbox} from your own files), "
                      f"failed {len(rep.failed)}, removed {len(rep.pruned)}")
        for name, (have, total) in rep.playlists.items():
            console.print(f"  playlist “{name}”: {have}/{total} tracks on the iPod")
        for sid, why in plan.unavailable:
            console.print(f"  [yellow]not resolved[/] https://open.spotify.com/track/{sid}: {why}")
        if rep.failed:
            console.print("  [yellow]failed tracks are retried by the next run[/]")
        if rep.ipod_full:
            console.print("[yellow]The iPod is full: the rest was skipped.[/]")
        if rep.orphans_removed:
            console.print(f"  cleaned up {rep.orphans_removed} file(s) left by an interrupted run")
        sigs = ipod.verify()
        console.print(f"  database signatures: {'[green]valid[/]' if all(sigs.values()) else f'[red]{sigs}[/]'}"
                      "  ·  eject the iPod before unplugging")

    def status(self) -> None:
        ipod = self.ipod
        synced = [t for t in ipod.tracks if sync.spotify_ids_of(t)]
        console.print(f"  {len(synced)} of the iPod's tracks come from Spotify; "
                      f"{len(self.state.journal())} copies in flight")
        waiting = list((self.state.root / "transcoded").glob("*.m4a"))
        console.print(f"  {len(waiting)} downloaded tracks ({size(sum(f.stat().st_size for f in waiting))}) wait in "
                      f"{self.state.root} to be copied over")
        self.show_sources()

    def menu(self) -> None:
        self.og  # connect up front, so that problems show before the menu
        if len(self.mounts()) == 1:
            self.ipod
        else:
            console.print("[yellow]●[/] iPod: not plugged in (you can still choose music and download it)")
        actions = {
            "Sync now": lambda: self.sync(prune=False, dry_run=False, confirm=True),
            "Download now, sync later (no iPod needed)": lambda: self.sync(
                prune=False, dry_run=False, confirm=True, fetch_only=True),
            "Sync now, and remove what is no longer wanted": lambda: self.sync(prune=True, dry_run=False, confirm=True),
            "Choose playlists": self.pick_playlists,
            "Search tracks, albums, artists": self.search,
            "Add a Spotify link": lambda: self.add_links((ask(questionary.text("Link or URI")) or "").split()),
            "Show what is kept": self.show_sources,
            "Stop keeping something": lambda: self.remove_sources([]),
            "Quit": None,
        }
        while True:
            n = len(self.state.sources())
            choice = ask(questionary.select(f"What next? ({n} source{'s' * (n != 1)} kept)", choices=list(actions)))
            if not choice or not actions[choice]:
                return
            try:
                actions[choice]()
            except OggifyError as e:
                console.print(f"[red]Spotify: {e}[/]")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mount", type=Path, help="the iPod, when more than one iPod is plugged in")
    ap.add_argument("--state", type=Path, default=ROOT / ".state")
    ap.add_argument("--snapshots", type=Path, default=ROOT / "snapshots", help="where pre-write database copies go")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("playlists"); sub.add_parser("sources"); sub.add_parser("status")
    sub.add_parser("search").add_argument("query", nargs="*")
    sub.add_parser("add").add_argument("links", nargs="+")
    sub.add_parser("remove").add_argument("names", nargs="*")
    r = sub.add_parser("run")
    r.add_argument("--prune", action="store_true", help="remove synced tracks and playlists that no source wants")
    r.add_argument("--dry-run", action="store_true", help="show what would happen")
    r.add_argument("--inbox", type=Path, help="folder of your own audio files, preferred over downloading")
    r.add_argument("--bitrate", type=int, default=256, help="AAC bitrate in kbit/s (default 256)")
    r.add_argument("--batch", type=int, default=20, help="tracks per database save (default 20)")
    r.add_argument("--no-artwork", action="store_true")
    f = sub.add_parser("fetch")
    f.add_argument("--bitrate", type=int, default=256, help="AAC bitrate in kbit/s (default 256)")
    f.add_argument("--no-artwork", action="store_true")
    a = ap.parse_args()
    if a.cmd != "run":  # the menu syncs with the defaults
        a = argparse.Namespace(**{**vars(r.parse_args([])), **vars(a)})
    app = App(a)
    # the terminal stays clean; what the iPod engine had to say is kept for when a write is refused
    logging.basicConfig(filename=a.state / "sync.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.info("──── %s", " ".join(sys.argv))

    try:
        if a.cmd is None:
            app.menu()
        elif a.cmd == "playlists":
            app.pick_playlists()
        elif a.cmd == "search":
            app.search(" ".join(a.query))
        elif a.cmd == "add":
            app.add_links(a.links)
        elif a.cmd == "sources":
            app.show_sources()
        elif a.cmd == "remove":
            app.remove_sources(a.names)
        elif a.cmd == "status":
            app.status()
        elif a.cmd == "run":
            app.sync(prune=a.prune, dry_run=a.dry_run, confirm=False)
        elif a.cmd == "fetch":
            app.sync(prune=False, dry_run=False, confirm=False, fetch_only=True)
    except OggifyError as e:
        sys.exit(f"Spotify: {e}")
    finally:
        if "og" in app.__dict__:
            app.og.close()


if __name__ == "__main__":
    main()
