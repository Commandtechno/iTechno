extern crate env_logger;
extern crate futures_util;
extern crate http;
extern crate librespot_audio;
extern crate librespot_core;
extern crate librespot_discovery;
extern crate librespot_metadata;
#[macro_use]
extern crate log;
extern crate protobuf;
extern crate regex;
#[macro_use]
extern crate serde_json;
extern crate sha1;
extern crate tokio;

mod pacer;

use std::collections::HashMap;
use std::env;
use std::error::Error;
use std::io::Write;
use std::io::{self, BufRead, Read};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use env_logger::{Builder, Env};
use futures_util::stream::{self, StreamExt};
use http::Method;
use librespot_audio::{AudioDecrypt, AudioFile};
use librespot_core::cache::Cache;
use librespot_core::config::SessionConfig;
use librespot_core::session::Session;
use librespot_core::spotify_id::SpotifyId;
use librespot_core::FileId;
use librespot_core::spotify_uri::SpotifyUri;
use librespot_discovery::{DeviceType, Discovery};
use librespot_metadata::artist::Artists;
use librespot_metadata::audio::{AudioFileFormat, AudioItem, UniqueFields};
use librespot_metadata::{Album, Artist, Metadata, Playlist, Track};
use protobuf::Message;
use pacer::{Clock, KeyPacer};
use regex::Regex;
use serde_json::Value;
use sha1::{Digest, Sha1};
use tokio::runtime::Runtime;

const DEVICE_NAME: &str = "Oggify";

fn credentials_dir() -> Option<PathBuf> {
  env::var_os("XDG_CACHE_HOME")
    .map(PathBuf::from)
    .or_else(|| env::var_os("HOME").map(|home| PathBuf::from(home).join(".cache")))
    .map(|dir| dir.join("oggify"))
}

/// The name this program goes by as a Spotify Connect device. Parallel sessions need distinct ones
/// ($OGGIFY_DEVICE_NAME): a second connection with the same identity would take the place of the first.
fn device_name() -> String {
  env::var("OGGIFY_DEVICE_NAME").unwrap_or_else(|_| DEVICE_NAME.to_owned())
}

async fn connect() -> Session {
  // Spotify clients expect a stable, 40 hex digits device id
  let device_id = Sha1::digest(device_name().as_bytes())
    .iter()
    .map(|byte| format!("{:02x}", byte))
    .collect::<String>();
  let session_config = SessionConfig {
    device_id,
    ..SessionConfig::default()
  };
  let cache = Cache::new(credentials_dir(), None, None, None).expect("Cannot open credentials cache");

  if let Some(credentials) = cache.credentials() {
    info!("Connecting with cached credentials ...");
    let session = Session::new(session_config.clone(), Some(cache.clone()));
    match session.connect(credentials, true).await {
      Ok(()) => return session,
      Err(e) => warn!("Cached credentials did not work: {}", e),
    }
  }

  let mut discovery = Discovery::builder(
    session_config.device_id.clone(),
    session_config.client_id.clone(),
  )
  .name(device_name())
  .device_type(DeviceType::Computer)
  .launch()
  .expect("Cannot start Spotify Connect discovery");
  // not a log line: this must reach the user whatever the log level is
  eprintln!(
    "Open Spotify on this network and select \"{}\" in the devices menu to log in.",
    device_name()
  );
  loop {
    let credentials = discovery
      .next()
      .await
      .expect("Spotify Connect discovery stopped unexpectedly");
    info!("Connecting ...");
    let session = Session::new(session_config.clone(), Some(cache.clone()));
    match session.connect(credentials, true).await {
      Ok(()) => {
        discovery.shutdown().await;
        return session;
      }
      Err(e) => warn!("Cannot connect: {}, waiting for another login...", e),
    }
  }
}

type Res<T> = Result<T, Box<dyn Error>>;

fn ogg_file(item: &AudioItem) -> Option<(AudioFileFormat, FileId)> {
  [
    AudioFileFormat::OGG_VORBIS_320,
    AudioFileFormat::OGG_VORBIS_160,
    AudioFileFormat::OGG_VORBIS_96,
  ]
  .iter()
  .find_map(|format| item.files.get(format).map(|file_id| (*format, *file_id)))
}

async fn get_available_item(session: &Session, uri: SpotifyUri) -> Res<AudioItem> {
  let playable = |item: &AudioItem| item.availability.is_ok() && ogg_file(item).is_some();
  let item = AudioItem::get_file(session, uri.clone()).await?;
  if playable(&item) {
    return Ok(item);
  }
  warn!("Track {} is not available, finding alternative...", uri);
  for alt_uri in item.alternatives.iter().flat_map(|alts| alts.0.iter()) {
    let alt_item = AudioItem::get_file(session, alt_uri.clone()).await?;
    if playable(&alt_item) {
      warn!("Found track alternative {} -> {}", uri, alt_item.track_id);
      return Ok(alt_item);
    }
  }
  Err(format!("Track {} is not available as Ogg Vorbis, and has no alternative that is", uri).into())
}

/// Returns the best Ogg Vorbis format of the item, and its decrypted stream.
struct SystemClock;

impl Clock for SystemClock {
  fn now(&self) -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |since| since.as_secs_f64())
  }
  fn sleep(&mut self, seconds: f64) {
    std::thread::sleep(Duration::from_secs_f64(seconds));
  }
}

fn download(
  core: &Runtime,
  session: &Session,
  pacer: &mut KeyPacer,
  waiting: impl FnMut(f64),
  item: &AudioItem,
) -> Res<(AudioFileFormat, Vec<u8>)> {
  debug!(
    "File formats: {}",
    item
      .files
      .keys()
      .map(|filetype| format!("{:?}", filetype))
      .collect::<Vec<_>>()
      .join(" ")
  );
  let (format, file_id) = ogg_file(item).ok_or("Could not find a OGG_VORBIS format for the track.")?;
  let track_id = match item.track_id {
    SpotifyUri::Track { id } => id,
    _ => return Err(format!("{} is not a track", item.track_id).into()),
  };
  let key = pacer.acquire(
    &mut SystemClock,
    || core.block_on(session.audio_key().request(track_id, file_id)),
    waiting,
  )?;
  let encrypted_file = core.block_on(AudioFile::open(session, file_id, 40 * 1024))?;
  // blocking reads are fine here: the download runs on the tokio worker threads, not on this one
  let mut decrypted_buffer = Vec::new();
  AudioDecrypt::new(Some(key), encrypted_file).read_to_end(&mut decrypted_buffer)?;
  if decrypted_buffer.len() <= 0xa7 {
    return Err("File stream is too short".into());
  }
  Ok((format, decrypted_buffer.split_off(0xa7)))
}

fn track_id(uri: &SpotifyUri) -> Option<String> {
  match uri {
    SpotifyUri::Track { id } => id.to_base62().ok(),
    _ => None,
  }
}

fn parse_id(id: &str) -> Res<SpotifyId> {
  Ok(SpotifyId::from_base62(id)?)
}

/// Collects the track ids of a context (liked songs, search results, artist...), following its pages.
async fn context_tracks(session: &Session, uri: &str, first_page_only: bool) -> Res<Vec<String>> {
  async fn get(session: &Session, path: &str) -> Res<Value> {
    let body = session
      .spclient()
      .request_as_json(&Method::GET, path, None, None)
      .await?;
    Ok(serde_json::from_slice(&body)?)
  }
  fn take_tracks(page: &Value, ids: &mut Vec<String>) {
    let uris = page["tracks"].as_array().into_iter().flatten();
    ids.extend(
      uris
        .filter_map(|track| track["uri"].as_str()?.strip_prefix("spotify:track:"))
        .map(|id| id.chars().take_while(|c| c.is_ascii_alphanumeric()).collect()),
    );
  }

  let context = get(session, &format!("/context-resolve/v1/{}", uri)).await?;
  let mut ids = Vec::new();
  for page in context["pages"].as_array().into_iter().flatten() {
    let mut page = page.clone();
    if page["tracks"].is_null() {
      match page["page_url"].as_str() {
        Some(url) => page = get(session, url.trim_start_matches("hm:/")).await?,
        None => continue,
      }
    }
    loop {
      take_tracks(&page, &mut ids);
      match page["next_page_url"].as_str() {
        Some(url) if !first_page_only => page = get(session, url.trim_start_matches("hm:/")).await?,
        _ => break,
      }
    }
    if first_page_only {
      break;
    }
  }
  Ok(ids)
}

/// Everything worth tagging about a track. Album-wide facts (totals, label, copyright) need the full album,
/// which is fetched once per album and is optional: a track is still described if its album cannot be.
async fn describe_track(session: &Session, albums: &Mutex<HashMap<SpotifyUri, Option<Album>>>, id: &str) -> Res<Value> {
  let track = Track::get(session, &SpotifyUri::Track { id: parse_id(id)? }).await?;
  let cached = albums.lock().unwrap().get(&track.album.id).cloned();
  let album = match cached {
    Some(album) => album,
    None => {
      let album = Album::get(session, &track.album.id).await.ok();
      albums.lock().unwrap().insert(track.album.id.clone(), album.clone());
      album
    }
  };
  let names = |artists: &Artists| artists.iter().map(|artist| artist.name.clone()).collect::<Vec<_>>();
  let cover = track
    .album
    .covers
    .iter()
    .max_by_key(|image| image.width)
    .and_then(|image| image.id.to_base16().ok())
    .map(|id| format!("https://i.scdn.co/image/{}", id));
  let isrc = track
    .external_ids
    .iter()
    .find(|external_id| external_id.external_type.eq_ignore_ascii_case("isrc"))
    .map(|external_id| external_id.id.to_uppercase().replace('-', ""));
  let date = track.album.date.as_utc();
  let disc_tracks = |album: &Album| {
    album
      .discs
      .iter()
      .find(|disc| disc.number == track.disc_number)
      .map(|disc| disc.tracks.len())
  };
  Ok(json!({
    "id": id,
    "name": track.name,
    "artists": names(&track.artists),
    "artist_ids": track.artists.iter().filter_map(|artist| artist.id.to_id().ok()).collect::<Vec<_>>(),
    "album": track.album.name,
    "album_id": track.album.id.to_id().ok(),
    "album_artists": names(&track.album.artists),
    "album_type": album.as_ref().map(|album| format!("{:?}", album.album_type)),
    "year": date.year(),
    "date": format!("{:04}-{:02}-{:02}", date.year(), date.month() as u8, date.day()),
    "track_number": track.number,
    "disc_number": track.disc_number,
    "total_tracks": album.as_ref().and_then(disc_tracks),
    "total_discs": album.as_ref().map(|album| album.discs.len()),
    "duration_ms": track.duration,
    "explicit": track.is_explicit,
    "isrc": isrc,
    "popularity": track.popularity,
    "label": album.as_ref().map(|album| album.label.clone()),
    "copyright": album.as_ref().and_then(|album| album.copyrights.first().map(|c| c.text.clone())),
    "cover": cover,
  }))
}

async fn handle(session: &Session, albums: &Mutex<HashMap<SpotifyUri, Option<Album>>>, request: &str) -> Res<Value> {
  let (command, arg) = request.split_once(' ').unwrap_or((request, ""));
  match (command, arg) {
    ("rootlist", "") => {
      let mut playlists = Vec::new();
      loop {
        let body = session.spclient().get_rootlist(playlists.len(), None).await?;
        let page = <Playlist as Metadata>::Message::parse_from_bytes(&body)?;
        let items = &page.contents.items;
        for (i, item) in items.iter().enumerate() {
          // the rest are folder markers
          if let Some(id) = item.uri().strip_prefix("spotify:playlist:") {
            let meta = page.contents.meta_items.get(i);
            playlists.push(json!({
              "id": id,
              "name": meta.map(|meta| meta.attributes.name()),
              "length": meta.map(|meta| meta.length()),
              "owner": meta.map(|meta| meta.owner_username()),
            }));
          }
        }
        if items.is_empty() || !page.contents.truncated() {
          break;
        }
      }
      Ok(json!({ "playlists": playlists }))
    }
    ("playlist", id) => {
      let uri = SpotifyUri::Playlist { user: None, id: parse_id(id)? };
      let playlist = Playlist::get(session, &uri).await?;
      Ok(json!({"name": playlist.name(), "tracks": playlist.tracks().filter_map(track_id).collect::<Vec<_>>()}))
    }
    ("album", id) => {
      let album = Album::get(session, &SpotifyUri::Album { id: parse_id(id)? }).await?;
      let artists = album.artists.iter().map(|artist| artist.name.as_str()).collect::<Vec<_>>();
      let name = format!("{} — {}", artists.join(", "), album.name);
      Ok(json!({"name": name, "tracks": album.tracks().filter_map(track_id).collect::<Vec<_>>()}))
    }
    ("artist", id) => {
      let artist = Artist::get(session, &SpotifyUri::Artist { id: parse_id(id)? }).await?;
      let tracks = context_tracks(session, &format!("spotify:artist:{}", id), true).await?;
      Ok(json!({"name": format!("{} — top tracks", artist.name), "tracks": tracks}))
    }
    ("liked", "") => {
      let uri = format!("spotify:user:{}:collection", session.username());
      Ok(json!({"name": "Liked Songs", "tracks": context_tracks(session, &uri, false).await?}))
    }
    ("search", query) if !query.is_empty() => {
      let query = query.split_whitespace().collect::<Vec<_>>().join("+");
      Ok(json!({"tracks": context_tracks(session, &format!("spotify:search:{}", query), true).await?}))
    }
    ("tracks", ids) => {
      let tracks = stream::iter(ids.split_whitespace())
        .map(|id| async move {
          // a big burst of lookups gets throttled: slow down and ask again, rather than give up on the track
          let mut wait = Duration::from_secs(2);
          loop {
            match describe_track(session, albums, id).await {
              Ok(track) => break track,
              Err(e) if e.to_string().contains("rate limited") && wait <= Duration::from_secs(32) => {
                debug!("Metadata of {} is rate limited, retrying in {}s", id, wait.as_secs());
                tokio::time::sleep(wait).await;
                wait *= 2;
              }
              Err(e) => break json!({"id": id, "error": e.to_string()}),
            }
          }
        })
        .buffered(4)
        .collect::<Vec<_>>()
        .await;
      Ok(json!({ "tracks": tracks }))
    }
    _ => Err(format!("Bad request: {}", request).into()),
  }
}

/// Serves requests from a controlling program: one request per stdin line, one JSON reply per stdout line.
///   rootlist                      -> {"playlists": [{"id", "name", "length", "owner"}]}: the user's playlists
///   playlist ID | album ID | artist ID | liked
///                                 -> {"name", "tracks": [ID]}
///   search QUERY                  -> {"tracks": [ID]}
///   tracks ID...                  -> {"tracks": [{"id", "name", "isrc", ...} or {"id", "error"}]}
///   download ID DEST_FILE         -> {"file", "format", "actual_id"}: fetch a track as Ogg Vorbis; may be preceded
///                                    by {"event": "waiting", "seconds"} lines while Spotify's rate limit is waited out
///   limits                        -> {"interval", "available"}: the rate limit on downloads as currently modelled
/// Replies carry "ok": true, failures are reported as {"ok": false, "error"}.
fn serve(core: &Runtime) {
  let mut session = core.block_on(connect());
  println!("{}", json!({"ok": true, "ready": true, "username": session.username()}));
  let albums = Mutex::new(HashMap::new());
  let mut pacer = KeyPacer::load(credentials_dir().map(|dir| dir.join("keys.json")));
  for line in io::stdin().lock().lines() {
    let line = line.expect("Cannot read request");
    if session.is_invalid() {
      warn!("Session lost, reconnecting ...");
      session = core.block_on(connect());
    }
    let reply = match line.trim().strip_prefix("download ").and_then(|args| args.split_once(' ')) {
      Some((id, dest)) => parse_id(id)
        .and_then(|id| core.block_on(get_available_item(&session, SpotifyUri::Track { id })))
        .and_then(|item| {
          // not a reply: the controlling program may want to say why nothing happens for a while
          let waiting = |seconds: f64| println!("{}", json!({"event": "waiting", "seconds": seconds}));
          let (format, ogg) = download(core, &session, &mut pacer, waiting, &item)?;
          // a partial file must never look like a finished download
          let part = format!("{}.part", dest);
          std::fs::write(&part, &ogg)?;
          std::fs::rename(&part, dest)?;
          Ok(json!({"file": dest, "format": format!("{:?}", format), "actual_id": track_id(&item.track_id)}))
        }),
      None if line.trim() == "limits" => Ok(json!({
        "interval": pacer.interval(),
        "available": pacer.available(SystemClock.now()),
      })),
      None => core.block_on(handle(&session, &albums, line.trim())),
    };
    let reply = match reply {
      Ok(mut reply) => {
        reply["ok"] = json!(true);
        reply
      }
      Err(e) => json!({"ok": false, "error": e.to_string()}),
    };
    println!("{}", reply);
  }
}

fn main() {
  let args: Vec<_> = env::args().collect();
  let serving = args.len() == 2 && args[1] == "serve";
  // when serving, the controlling program owns the terminal: keep quiet unless something is wrong
  let default_filter = if serving { "warn,libmdns=error" } else { "info,libmdns=error" };
  Builder::from_env(Env::default().default_filter_or(default_filter)).init();

  assert!(
    args.len() == 1 || args.len() == 2,
    "Usage: {0} [helper_script] < tracks_file\n       {0} serve",
    args[0]
  );

  let core = Runtime::new().unwrap();
  if serving {
    return serve(&core);
  }
  let session = core.block_on(connect());
  info!("Connected!");
  let mut pacer = KeyPacer::load(credentials_dir().map(|dir| dir.join("keys.json")));

  let spotify_uri = Regex::new(r"spotify:track:([[:alnum:]]+)").unwrap();
  let spotify_url = Regex::new(r"open\.spotify\.com/track/([[:alnum:]]+)").unwrap();

  io::stdin()
    .lock()
    .lines()
    .filter_map(|line| {
      line.ok().and_then(|str| {
        spotify_uri
          .captures(&str)
          .or(spotify_url.captures(&str))
          .or_else(|| {
            warn!("Cannot parse track from string {}", str);
            None
          })
          .and_then(|capture| SpotifyId::from_base62(&capture[1]).ok())
      })
    })
    .for_each(|id| {
      let id_str = id.to_base62().expect("Cannot format track id");
      info!("Getting track {}...", id_str);
      let item = core
        .block_on(get_available_item(&session, SpotifyUri::Track { id }))
        .expect("Cannot get track");
      let waiting = |seconds: f64| info!("Spotify limits the download rate: next track in about {:.0}s", seconds);
      let (_, ogg) = download(&core, &session, &mut pacer, waiting, &item).expect("Cannot download track");
      let (artists_strs, album): (Vec<_>, _) = match item.unique_fields {
        UniqueFields::Track { artists, album, .. } => {
          (artists.0.into_iter().map(|artist| artist.name).collect(), album)
        }
        _ => panic!("{} is not a track", item.track_id),
      };
      if args.len() == 1 {
        let fname = format!("{} - {}.ogg", artists_strs.join(", "), item.name);
        std::fs::write(&fname, &ogg).expect("Cannot write decrypted track");
        info!("Filename: {}", fname);
      } else {
        let mut cmd = Command::new(args[1].to_owned());
        cmd.stdin(Stdio::piped());
        cmd
          .arg(id_str)
          .arg(item.name)
          .arg(album)
          .args(artists_strs.iter());
        let mut child = cmd.spawn().expect("Could not run helper program");
        let pipe = child.stdin.as_mut().expect("Could not open helper stdin");
        pipe.write_all(&ogg).expect("Failed to write to stdin");
        assert!(
          child
            .wait()
            .expect("Out of ideas for error messages")
            .success(),
          "Helper script returned an error"
        );
      }
    });
}
