# iTechno — programmatic iPod nano 7G library manager (PoC)

Setup: `git clone --recurse-submodules <repo> && cd <repo> && uv sync` (macOS arm64; see bottom to rebuild the verifier lib elsewhere).

```
uv run ipod.py info | list [QUERY] | playlists | verify
uv run ipod.py add FILE [--title --artist --album --genre] [--playlist NAME]
uv run ipod.py edit QUERY [--title --artist --album --genre --rating 0-5]
uv run ipod.py remove QUERY
uv run ipod.py playlist-create NAME [QUERY ...] | playlist-delete NAME
```

As a library: `from ipodkit.manager import IPod` → mutate `ipod.tracks` / `ipod.playlists` (plain dicts) → `ipod.save()`.

## How it works
- `vendor/iOpenPod` — pure-Python iTunesCDB + SQLite (`iTunes Library.itlp`) reader/writer. Used as an engine; GUI unused.
- `ipodkit/verify.py` — independent hashAB verifier (native build of `vendor/hashab-src`). hashAB embeds 23 random
  bytes, so signatures are checked by recovering those bytes and recomputing. Validated against the signatures iTunes
  itself wrote to this device.
- `ipodkit/manager.py` — `save()` snapshots the DB to `snapshots/`, writes, verifies signatures + re-parses, and
  restores the snapshot automatically on any failure.

## Gotchas found the hard way
1. iOpenPod 1.68 signs with `hashing_scheme=4` then patches the field to `3` after signing → invalid signature.
   Worked around in `manager.py` (`ITDB_CHECKSUM_HASHAB = 3`). Worth reporting upstream.
2. The engine needs `set_current_device()` first or it silently writes a malformed iTunesCDB (compression flag 1, not 2).
3. A rebuild renumbers internal IDs, strips trailing whitespace from titles, and gives album-less tracks a placeholder album.

`backup/` holds the pristine pre-PoC copy of `iPod_Control/{iTunes,Device,Artwork}`.
Rebuild the verifier lib: `clang -O2 -shared -fPIC -o ipodkit/libhashab.dylib vendor/hashab-src/src/*.c`
