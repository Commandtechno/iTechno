#!/usr/bin/env bash
# Builds a minimal, LGPL-only ffmpeg: just the audio formats iTechno reads (Spotify's Ogg Vorbis and whatever is in
# an --inbox folder) and the one it writes (AAC in an .m4a). Works on macOS, Linux and MSYS2/MinGW (Windows).
#
#   packaging/build_ffmpeg.sh [OUTPUT_DIR]      -> OUTPUT_DIR/ffmpeg (ffmpeg.exe on Windows); default packaging/build/ffmpeg
set -euo pipefail

VERSION=7.1.1
# Pinned so that a build is reproducible and a swapped tarball is noticed. Taken from the first download of the
# release (ffmpeg.org/releases), not from a signed source: check the .asc against FFmpeg's key to raise the trust.
SHA256=733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1

here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-$here/build/ffmpeg}"
work="$here/build/ffmpeg-src"
mkdir -p "$out" "$work"

tarball="$work/ffmpeg-$VERSION.tar.xz"
if [ ! -f "$tarball" ]; then
  curl -fsSL -o "$tarball" "https://ffmpeg.org/releases/ffmpeg-$VERSION.tar.xz"
fi
echo "$SHA256  $tarball" > "$work/sha256"
if command -v sha256sum >/dev/null; then sha256sum -c "$work/sha256"; else shasum -a 256 -c "$work/sha256"; fi
[ -d "$work/ffmpeg-$VERSION" ] || tar -xf "$tarball" -C "$work"

DECODERS=vorbis,opus,flac,mp3,mp3float,aac,aac_fixed,alac,wmav1,wmav2,pcm_s16le,pcm_s16be,pcm_s24le,pcm_s24be,pcm_s32le,pcm_f32le,pcm_u8
DEMUXERS=ogg,flac,mp3,mov,wav,aiff,aac,asf,matroska
PARSERS=vorbis,opus,flac,mpegaudio,aac,aac_latm
FILTERS=aresample,aformat,anull,anullsink,atrim,volume

extra=()
case "$(uname -s)" in
  Darwin) extra+=(--enable-audiotoolbox --enable-encoder=aac_at) ;;  # aac_at: Apple's AAC encoder, better than ffmpeg's own, and a system framework
esac

cd "$work/ffmpeg-$VERSION"
./configure \
  --disable-everything --disable-autodetect --disable-doc --disable-debug --disable-network \
  --disable-ffplay --disable-ffprobe --disable-avdevice --disable-swscale --disable-postproc \
  --disable-shared --enable-static \
  --disable-x86asm \
  --enable-small --enable-ffmpeg \
  --enable-protocol=file,pipe \
  --enable-demuxer="$DEMUXERS" --enable-parser="$PARSERS" --enable-decoder="$DECODERS" \
  --enable-encoder=aac --enable-muxer=ipod,mp4,mov --enable-bsf=aac_adtstoasc \
  --enable-filter="$FILTERS" \
  "${extra[@]}"
jobs="$( (nproc || sysctl -n hw.ncpu) 2>/dev/null || echo 4)"
exe=ffmpeg
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) exe=ffmpeg.exe ;; esac  # the make target carries the suffix on Windows
make -j"$jobs" "$exe"
install -m 755 "$exe" "$out/$exe"
"$out/$exe" -hide_banner -version | head -1
