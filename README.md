# oggify
Download Spotify tracks to Ogg Vorbis (with a premium account).

This library uses [librespot](https://github.com/librespot-org/librespot). It is my first program in Rust so you may see some horrors in the way I handle tokio, futures and such.

# Usage
To download a number of tracks as `"artists" - "title".ogg`, run
```
oggify < tracks_list
```
Oggify logs in through Spotify Connect: on the first run it shows up as a device named "Oggify" on your local network. Open a Spotify client on the same network (logged in with your premium account), and select "Oggify" in the devices menu. The credentials are then cached in `$XDG_CACHE_HOME/oggify` (or `~/.cache/oggify`), so later runs connect right away. Delete that directory to log in with another account.

Oggify reads from stdin and looks for a track URL or URI in each line. The two formats are those you get with the track menu items "Share->Copy Song Link" or "Share->Copy Song URI" in the Spotify client, for example `open.spotify.com/track/1xPQDRSXDN5QJWm7qHg5Ku` or `spotify:track:1xPQDRSXDN5QJWm7qHg5Ku`.

## Helper script
A second form of invocation of oggify is
```
oggify "helper_script" < tracks_list
```
In this form `helper_script` is invoked for each new track:
```
helper_script "spotify_id" "title" "album" "artist1" ["artist2"...] < ogg_stream
```
The script `tag_ogg` in the source tree can be used to automatically add the track information (spotify ID, title, album, artists) as vorbis comments.

## Serve mode
`oggify serve` turns oggify into a Spotify backend for another program (it is what `../sync.py` drives to sync
an iPod): one request per stdin line, one JSON reply per stdout line, logs and the login prompt on stderr.
```
rootlist                  -> {"playlists": [{"id", "name", "length", "owner"}]}   your playlists
playlist ID | album ID | artist ID | liked
                          -> {"name", "tracks": [ID]}                              artist = top tracks
search QUERY              -> {"tracks": [ID]}
tracks ID...              -> {"tracks": [{"id", "name", "artists", "album", "isrc", "cover", ...} or {"id", "error"}]}
download ID DEST_FILE     -> {"file", "format", "actual_id"}                       Ogg Vorbis, best quality available
limits                    -> {"interval", "available"}                             the download rate limit, as modelled
```
Replies carry `"ok": true`; failures are `{"ok": false, "error"}` and do not end the session. A `download` reply may
be preceded by `{"event": "waiting", "seconds"}` lines.

## Rate limit
Spotify hands out audio keys from a per-account token bucket: a burst of ~25, then one every ~30s. Instead of backing
off from refusals, oggify models the bucket (`src/pacer.rs`): it asks for each key when it is due, measures the real
refill interval as it goes, and keeps the model in `~/.cache/oggify/keys.json`. Both modes use it.

### Converting to MP3
Use `oggify` with the `tag_ogg` helper script as described above, then convert with ffmpeg:
```
for ogg in *.ogg; do
	ffmpeg -i "$ogg" -map_metadata 0:s:0 -id3v2_version 3 -codec:a libmp3lame -qscale:a 2 "$(basename "$ogg" .ogg).mp3"
done
```
