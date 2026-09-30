"""Offline signature verifiers for iPod databases, one per checksum scheme the engine writes.

  NONE    iPod 1G-5G, Mini, Nano 1G/2G: nothing to sign; only the header is checked.
  HASH58  Classic, Nano 3G/4G: HMAC-SHA1 keyed from the FireWire ID (deterministic, so recomputed and compared).
  HASH72  Nano 5G: AES over the SHA1, using the device's HashInfo file (deterministic, recomputed and compared).
  HASHAB  Nano 6G/7G: the one described below.

The HASH58/HASH72 checks reuse the engine's primitives but redo the zero-the-hash-fields step themselves, so they
catch a database whose fields were altered after signing, which is how the hashAB bug was hiding.

hashAB signatures embed 23 random bytes, so two valid signatures over the same
data never compare equal. To verify one, recover its random bytes, recompute
the signature with them, and check the result is identical. This is the same
check the iPod firmware effectively performs, so a database that passes here
is one the device will accept.

Uses a native build of https://github.com/dstaley/hashab (the WASM build
hard-codes the random bytes, so it can sign but not verify).
"""
from __future__ import annotations

import ctypes
import functools
import hashlib
import hmac
from pathlib import Path

from iopenpod.device import ChecksumType


@functools.cache
def _lib() -> ctypes.CDLL:  # only hashAB devices need the native build
    return ctypes.CDLL(str(Path(__file__).with_name("libhashab.dylib")))


# Output permutation from hashab's calcHashAB.c: sources < 23 are random bytes.
_P56 = [0x15, 0x1c, 0x06, 0x0c, 0x07, 0x1a, 0x05, 0x13, 0x08, 0x19, 0x03, 0x01, 0x2d, 0x1e,
        0x10, 0x31, 0x1d, 0x14, 0x28, 0x27, 0x35, 0x00, 0x2f, 0x1b, 0x26, 0x0b, 0x0e, 0x02,
        0x23, 0x17, 0x24, 0x22, 0x12, 0x1f, 0x20, 0x04, 0x29, 0x25, 0x21, 0x09, 0x18, 0x0d,
        0x32, 0x0f, 0x11, 0x2e, 0x33, 0x2b, 0x30, 0x2a, 0x36, 0x0a, 0x2c, 0x34, 0x16, 0x37]


def calc_hashab(sha1: bytes, fwid: bytes, rnd: bytes) -> bytes:
    out = ctypes.create_string_buffer(57)
    _lib().calcHashAB(out, sha1, fwid[:8], rnd)
    return out.raw


def signature_valid(sig: bytes, sha1: bytes, fwid: bytes) -> bool:
    rnd = bytearray(23)
    for i, src in enumerate(_P56):
        if src < 23:
            rnd[src] = sig[i + 2]
    return calc_hashab(sha1, fwid, bytes(rnd)) == bytes(sig)


def cdb_sha1(data: bytes) -> bytes:
    """SHA1 of an iTunesCDB exactly as stored, with only the hash fields zeroed."""
    d = bytearray(data)
    for off, n in ((0x18, 8), (0x32, 20), (0x58, 20), (0x72, 46), (0xAB, 57)):
        d[off:off + n] = bytes(n)
    return hashlib.sha1(d).digest()


def verify_cdb(path: str | Path, fwid: bytes) -> bool:
    data = Path(path).read_bytes()
    return signature_valid(data[0xAB:0xAB + 57], cdb_sha1(data), fwid)


def verify_cbk(cbk_path: str | Path, locations_path: str | Path, fwid: bytes) -> bool:
    cbk, loc = Path(cbk_path).read_bytes(), Path(locations_path).read_bytes()
    blocks = b"".join(hashlib.sha1(loc[i:i + 1024]).digest() for i in range(0, len(loc), 1024))
    final = hashlib.sha1(blocks).digest()
    return cbk[57:77] == final and cbk[77:] == blocks and signature_valid(cbk[:57], final, fwid)


def verify_itunes_dir(itunes_dir: str | Path, fwid: bytes) -> dict[str, bool]:
    d = Path(itunes_dir)
    itlp = d / "iTunes Library.itlp"
    return {
        "iTunesCDB": verify_cdb(d / "iTunesCDB", fwid),
        "Locations.itdb.cbk": verify_cbk(itlp / "Locations.itdb.cbk", itlp / "Locations.itdb", fwid),
    }


# ── other schemes ───────────────────────────────────────────────────────
_OFF_DB_ID, _OFF_SCHEME, _OFF_UNK32, _OFF_H58, _OFF_H72 = 0x18, 0x30, 0x32, 0x58, 0x72


def _header(path: Path) -> bytearray | None:
    data = bytearray(path.read_bytes())
    return data if len(data) >= 0xA0 and data[:4] == b"mhbd" else None


def verify_hash58(path: str | Path, fwid: bytes) -> bool:
    from iopenpod.itunesdb_writer.hash58 import compute_hash58

    d = _header(Path(path))
    if d is None or int.from_bytes(d[_OFF_SCHEME:_OFF_SCHEME + 2], "little") != 1:
        return False
    stored = bytes(d[_OFF_H58:_OFF_H58 + 20])
    for off, n in ((_OFF_DB_ID, 8), (_OFF_UNK32, 20), (_OFF_H58, 20)):
        d[off:off + n] = bytes(n)
    return hmac.compare_digest(compute_hash58(fwid, bytes(d)), stored)


def verify_hash72(path: str | Path, mount: str | Path) -> bool:
    from iopenpod.itunesdb_writer.hash72 import compute_hash72

    d = _header(Path(path))
    if d is None or int.from_bytes(d[_OFF_SCHEME:_OFF_SCHEME + 2], "little") != 2:
        return False
    try:
        expected = compute_hash72(str(mount), bytes(d))
    except FileNotFoundError:  # no HashInfo: nothing to check against
        return False
    return hmac.compare_digest(expected, bytes(d[_OFF_H72:_OFF_H72 + 46]))


def verify_database(mount: str | Path, db_path: str | Path, checksum: ChecksumType, fwid: bytes) -> dict[str, bool]:
    """Check the database the device will read against the scheme it requires. All values True means it is accepted."""
    db = Path(db_path)
    if checksum == ChecksumType.HASHAB:
        return verify_itunes_dir(db.parent, fwid)
    if checksum == ChecksumType.HASH58:
        return {db.name: verify_hash58(db, fwid)}
    if checksum == ChecksumType.HASH72:
        return {db.name: verify_hash72(db, mount)}
    if checksum == ChecksumType.NONE:  # unsigned; all that can be checked is that it is a database
        return {db.name: _header(db) is not None}
    raise ValueError(f"No verifier for checksum type {checksum!r}")
