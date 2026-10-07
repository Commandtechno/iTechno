# iTechno — Spotify → iPod sync, and a programmatic iPod library manager

```
sync.py, ctrl.py   the two command line tools
ipodkit/           the library behind them: iPod database (manager, verify), sync engine, Spotify backend client
oggify/            the Spotify backend (Rust, librespot): login, catalogue, downloads. Has its own README.
vendor/            submodules: iOpenPod (iTunesDB engine), hashab-src (signature code, for the verifier)
```

## Download (no setup)
Grab the archive for your system from the [Releases](../../releases) page, unpack it and run it: one file, with the
Spotify backend, a minimal ffmpeg and the signature library inside. Nothing to install.

```
tar -xzf itechno-macos-arm64.tar.gz && ./itechno doctor     # macOS / Linux; Windows: unzip, then itechno.exe doctor
./itechno                    # the interactive sync (what `uv run sync.py` does below)
./itechno ipod list          # the library manager (what `uv run ctrl.py` does below)
```
`itechno doctor` checks that every bundled tool works on your machine and shows where your data goes (macOS
`~/Library/Application Support/iTechno`, Windows `%LOCALAPPDATA%\iTechno`, Linux `~/.local/share/iTechno`, or `$ITECHNO_HOME`).
The first launch takes a couple of seconds while it unpacks itself. The builds are not code-signed: on macOS, a browser
download is quarantined (run `xattr -d com.apple.quarantine itechno`, or fetch it with `curl`); on Windows, SmartScreen
asks you to confirm.
Build one yourself with `uv run --group build python packaging/build.py` (needs `cargo` and a C compiler; `packaging/build_ffmpeg.sh` builds
the ffmpeg: LGPL, only the audio formats used here). CI builds all platforms when a `v*` tag is pushed (`.github/workflows/release.yml`).

Supported iPods (whatever the iOpenPod engine writes): Classic, Mini, Nano 1G–7G and the full-size iPods 1G–5.5G. Shuffle and
Touch are not supported. `manager.py` picks the database file and signing scheme from the device, and `verify.py` checks
every save against that scheme:

| Device | Database | Signature | Verified by |
| --- | --- | --- | --- |
| iPod 1G–5.5G, Mini, Nano 1G–2G | iTunesDB | none | header check |
| Classic, Nano 3G–4G | iTunesDB | HASH58 | recomputed HMAC |
| Nano 5G | iTunesCDB | HASH72 (needs `HashInfo`, created by one iTunes sync) | recomputed AES signature |
| Nano 6G–7G | iTunesCDB + SQLite | hashAB | native hashAB build (see Platforms) |

Only the nano 7G has been run on real hardware; the other models were rehearsed on iOpenPod virtual iPods (save, verify,
and a tamper check that verification fails on a corrupted database).

Setup: `git clone --recurse-submodules <repo> && cd <repo> && uv sync` (see Platforms).

**Platforms:** macOS, Linux and Windows. The iPod is found on its own (macOS `/Volumes`, Linux `/proc/mounts`, Windows drive letters; 
`--mount PATH` overrides). The only native piece is the hashAB verifier for nano 6G/7G: macOS arm64 uses the committed
`libhashab.dylib`; anywhere else it is compiled once from `vendor/hashab-src` on first use, which needs `clang` or `gcc`
on `PATH` (on Windows, MinGW-w64 or LLVM). Other iPod models need no compiler. `sync.py` also needs `cargo` and `ffmpeg`. Only macOS has been
run by the author; Linux and Windows are untested.

```
uv run ctrl.py info | list [QUERY] | playlists | verify
uv run ctrl.py add FILE [--title --artist --album --genre] [--playlist NAME]
uv run ctrl.py edit QUERY [--title --artist --album --genre --rating 0-5]
uv run ctrl.py remove QUERY
uv run ctrl.py playlist-create NAME [QUERY ...] | playlist-delete NAME
```

As a library: `from ipodkit.manager import IPod` → mutate `ipod.tracks` / `ipod.playlists` (plain dicts) → `ipod.save()`.

## Spotify → iPod sync (`sync.py`)
Pick what you want from Spotify; `sync.py` downloads it, converts it and keeps the iPod in step. It needs
`cargo` (the Spotify backend in `oggify/` is built automatically on first use), `ffmpeg`, and a Spotify Premium
account.

```
uv run sync.py                     # interactive: choose playlists, search, sync
uv run sync.py playlists           # tick your playlists and Liked Songs (type to filter)
uv run sync.py search QUERY        # find tracks; add them, their albums, or their artists' top tracks
uv run sync.py add LINK|liked ...  # any Spotify link/URI: playlist, album, artist, track
uv run sync.py sources | remove [NAME ...] | status
uv run sync.py run [--prune] [--dry-run] [--inbox DIR] [--bitrate 256] [--no-artwork]
uv run sync.py fetch               # download now, sync later: the iPod does not need to be plugged in
```

- **Login** happens once, without passwords or developer apps: the first Spotify command makes this computer show
  up as the device "Oggify" in the Spotify app (same network); select it. Credentials are cached in `~/.cache/oggify`.
- **What is synced**: playlists, Liked Songs and artists (top tracks) are mirrored as iPod playlists, by name and in
  order; albums and single tracks just join the library. Tracks are 256k AAC (from Spotify's 320k Vorbis) carrying
  title, artists, album, album artist, year and release date, track/disc numbers and totals, explicit and compilation
  flags, and cover art; the files themselves are tagged too (plus ISRC, label, copyright).
- **One file per recording.** Spotify lists the same recording under several track IDs (album, single,
  compilation…). Tracks are grouped by ISRC: a recording is downloaded once, and every playlist that wants any of its
  IDs points at that one file. An ID met later is attached to the file already there instead of downloaded again.
- **The iPod is the record of what is synced**: an imported track's Comment holds
  `spotify:track:<id> [spotify:track:<id> …] isrc:<ISRC>`. Every run is a fresh diff against that, so re-running is
  always safe and only fetches what is missing; `.state/` (chosen sources, metadata cache, work in progress) can be
  lost without losing track of anything on the device.
- **Crash-safe and resumable**: file copies are journaled in `.state/sync.db` before they start and cleared after the
  database save that references them; the next run deletes unreferenced leftovers. Downloads and transcodes are kept
  until that save, so an interrupted run resumes without fetching anything twice. Tested by hard-killing mid-batch.
- `--prune` removes synced tracks that no source wants any more, and playlists this tool created whose source is
  gone — never your other music. If a source cannot be read from Spotify the run stops before changing anything.
- `--inbox DIR` prefers your own files over downloading, matched by Spotify ID (tag, comment or filename), ISRC
  tag, or title + artist + duration (±3s). FLAC/OGG/Opus are transcoded; MP3/M4A/WAV/AIFF are copied as-is.
- The sync stops cleanly when the iPod is full. Failed or unavailable tracks are listed and retried by the next run.
- **Spotify's rate limit sets the pace, and is planned for rather than run into.** Measured: decryption keys come
  from a per-account token bucket, a burst of ~25 tracks and then one more every ~30s (~120 tracks/hour; a second
  session shares the same bucket, and a refused request costs nothing). The backend models that bucket, asks for
  each key right when it is due (within 0.1% of the possible maximum in simulation), keeps measuring the real
  interval, and remembers the state between runs. The plan shows an honest time estimate; while waiting, the
  progress line says so. Playing music on the same account during a sync draws from the same bucket.
- **`fetch` is the part to leave running.** At ~120 tracks/hour a big library takes hours, none of which need the
  iPod: `fetch` downloads, converts and tags everything still missing into `.state/transcoded/` (about 7 MB a track),
  going by the iPod if it is plugged in and by what it held when last seen if not. It keeps the machine awake while it
  works, retries what failed, and can be stopped and restarted at will. The next `run` then just copies.
- **Debugging**: every run appends to `.state/sync.log`: the plan, each request to and reply from the Spotify
  backend, the backend's own log, every import with full tracebacks for failures, and all of the iPod engine's
  output. When something goes wrong, that file has the reason.

## How it works
- `vendor/iOpenPod` — pure-Python iTunesCDB + SQLite (`iTunes Library.itlp`) reader/writer. Used as an engine; GUI unused.
  It follows upstream's `1.x` branch: 2.0 is a rewrite (`iPodDB`, `storage`) without the API `manager.py` builds on.
- `ipodkit/verify.py` — signature verifiers per scheme; the hashAB one is independent (native build of `vendor/hashab-src`). hashAB embeds 23 random
  bytes, so signatures are checked by recovering those bytes and recomputing. Validated against the signatures iTunes
  itself wrote to this device.
- `ipodkit/manager.py` — `save()` snapshots the DB to `snapshots/`, writes, verifies signatures + re-parses, and
  restores the snapshot automatically on any failure (Ctrl-C included). The last 5 snapshots are kept, and separately
  the last 5 `-start` ones: the state from before each session touched anything.
- `ipodkit/oggify.py` — drives `oggify serve` (the crate in `oggify/`: Rust, librespot) over a pipe: login, playlists, liked songs, search,
  track metadata and downloads. The official Web API is not used: Spotify rate-limits it to nothing for this kind of
  session, and it would need a developer app.
- `ipodkit/sync.py` — the diff/plan/import engine described above; `sync.py` is only its terminal front end.

## Gotchas found the hard way
1. iOpenPod 1.68 signs with `hashing_scheme=4` then patches the field to `3` after signing → invalid signature.
   Worked around in `manager.py` (`ITDB_CHECKSUM_HASHAB = 3`). Worth reporting upstream.
2. The engine needs `set_current_device()` first or it silently writes a malformed iTunesCDB (compression flag 1, not 2).
3. A rebuild renumbers internal IDs, strips trailing whitespace from titles, and gives album-less tracks a placeholder album.

`backup/` holds the pristine pre-PoC copy of `iPod_Control/{iTunes,Device,Artwork}`.
Rebuild the verifier lib by hand: `clang -O2 -shared -fPIC -o ipodkit/libhashab.dylib vendor/hashab-src/src/*.c` (delete the
old one, or a `libhashab-<cpu>` file, to make `verify.py` build it again itself).
