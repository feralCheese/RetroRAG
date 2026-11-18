"""Commodore DOS disks: 1541 (.d64, also 40-track and error-info variants) and 1581 (.d81).

Files are kept exactly as the drive stores them; a PRG starts with its 2-byte load address (like a .prg file).
"""
from __future__ import annotations

import re
import struct

from . import Entry, Image, MediaError, MediaFormat, NewFile, Option, register
from .basic import cbm_tokenise

TYPES = ["DEL", "SEQ", "PRG", "USR", "REL"]
D64_SIZES = {174848: 35, 175531: 35, 196608: 40, 197376: 40}
D81_SIZES = (819200, 822400)


def d64_spt(t: int) -> int:
    return 21 if t <= 17 else 19 if t <= 24 else 18 if t <= 30 else 17


class Geo:
    def __init__(self, kind: str, tracks: int):
        self.kind, self.tracks = kind, tracks
        if kind == "d64":
            self.spt = d64_spt
            self.dir_track, self.interleave, self.dir_step = 18, 10, 3
        else:
            self.spt = lambda t: 40
            self.dir_track, self.interleave, self.dir_step = 40, 1, 1
        self.base = {}
        off = 0
        for t in range(1, tracks + 1):
            self.base[t] = off
            off += self.spt(t)
        self.total = off

    def offset(self, t: int, s: int) -> int:
        if not (1 <= t <= self.tracks) or not (0 <= s < self.spt(t)):
            raise MediaError(f"Bad track/sector {t}/{s}")
        return (self.base[t] + s) * 256


def petscii_name(raw: bytes) -> str:
    out = []
    for c in raw:
        if c == 0xA0:
            break
        if 0x20 <= c <= 0x5F:
            out.append("£" if c == 0x5C else chr(c))
        elif 0xC1 <= c <= 0xDA:
            out.append(chr(c - 0x80).lower())
        else:
            out.append("{$%02x}" % c)
    return "".join(out)


def encode_name(name: str, n: int = 16) -> bytes:
    out = bytearray()
    i = 0
    while i < len(name) and len(out) < n:
        m = re.match(r"\{\$([0-9a-fA-F]{2})\}", name[i:])
        if m:
            out.append(int(m.group(1), 16))
            i += len(m.group())
            continue
        ch = name[i].upper()
        o = ord(ch)
        out.append(0x5C if ch == "£" else o if 0x20 <= o <= 0x5F else 0x3F)
        i += 1
    return bytes(out).ljust(n, b"\xa0")


class CbmDisk(Image):
    def __init__(self, fmt: MediaFormat, data: bytes, kind: str):
        super().__init__(fmt, data)
        if kind == "d64":
            tracks = D64_SIZES.get(len(data))
            if not tracks:
                raise MediaError("Not a D64 image (wrong size)")
        else:
            if len(data) not in D81_SIZES:
                raise MediaError("Not a D81 image (wrong size)")
            tracks = 80
        self.geo = g = Geo(kind, tracks)
        if kind == "d64":
            bam = self.sector(18, 0)
            self.label = petscii_name(bam[0x90:0xA0])
            disk_id = petscii_name(bam[0xA2:0xA7])
            free = sum(bam[4 * t] for t in range(1, 36) if t != 18)
            first = (bam[0], bam[1]) if bam[0] == 18 else (18, 1)
        else:
            hdr = self.sector(40, 0)
            self.label = petscii_name(hdr[0x04:0x14])
            disk_id = petscii_name(hdr[0x16:0x1B])
            free = 0
            for s, t0 in ((1, 1), (2, 41)):
                b = self.sector(40, s)
                free += sum(b[0x10 + 6 * (t - t0)] for t in range(t0, t0 + 40) if t != 40)
            first = (hdr[0], hdr[1]) if hdr[0] == 40 else (40, 3)
        self.disk_id = disk_id
        self.info = [f"Disk ID: {disk_id}", f"{free} blocks free",
                     f"{g.tracks} tracks" + (" (with error info)" if len(data) in (175531, 197376, 822400) else "")]
        t, s, seen = first[0], first[1], set()
        while t and (t, s) not in seen and len(seen) < 300:
            seen.add((t, s))
            try:
                sec = self.sector(t, s)
            except MediaError:
                break
            for k in range(8):
                ent = sec[k * 32:k * 32 + 32]
                ftype = ent[2]
                if ftype & 0x07 == 0 and not ftype & 0x80:
                    continue                       # scratched / empty slot
                tname = TYPES[ftype & 7] if (ftype & 7) < len(TYPES) else "???"
                blocks = struct.unpack("<H", ent[30:32])[0]
                meta = {"blocks": blocks, "locked": bool(ftype & 0x40), "closed": bool(ftype & 0x80),
                        "track": ent[3], "sector": ent[4]}
                e = Entry(petscii_name(ent[5:21]), 0, tname, meta, (ent[3], ent[4]))
                try:
                    data_ = self._chain(ent[3], ent[4]) if ent[3] else b""
                except MediaError:
                    data_ = b""
                e.size = len(data_)
                e.ref = data_
                if tname == "PRG" and len(data_) >= 2:
                    meta["load"] = data_[0] | data_[1] << 8
                self._entries.append(e)
            t, s = sec[0], sec[1]

    def sector(self, t: int, s: int) -> bytes:
        o = self.geo.offset(t, s)
        return self.data[o:o + 256]

    def _chain(self, t: int, s: int) -> bytes:
        out, seen = bytearray(), set()
        while t:
            if (t, s) in seen or len(seen) > self.geo.total:
                raise MediaError("File chain loops")
            seen.add((t, s))
            sec = self.sector(t, s)
            if sec[0] == 0:
                out += sec[2:max(2, sec[1] + 1)]
                break
            out += sec[2:]
            t, s = sec[0], sec[1]
        return bytes(out)

    def read(self, e: Entry) -> bytes:
        return e.ref


# ---------------------------------------------------------------- writing
class Allocator:
    def __init__(self, geo: Geo, track_order: list[int]):
        self.geo = geo
        self.free = {t: set(range(geo.spt(t))) for t in range(1, geo.tracks + 1)}
        self.order = track_order
        self.t, self.s = None, 0

    def take(self, t: int, s: int):
        self.free[t].discard(s)

    def next(self) -> tuple[int, int]:
        g = self.geo
        if self.t is not None and self.free[self.t]:
            n = g.spt(self.t)
            for k in range(n):
                s = (self.s + g.interleave + k) % n
                if s in self.free[self.t]:
                    self.s = s
                    self.free[self.t].discard(s)
                    return self.t, s
        for t in self.order:
            if self.free[t]:
                self.t, self.s = t, min(self.free[t])
                self.free[t].discard(self.s)
                return self.t, self.s
        raise MediaError("Disk full")


def _alternate(centre: int, lo: int, hi: int) -> list[int]:
    out = []
    for d in range(1, hi - lo + 2):
        for t in (centre - d, centre + d):
            if lo <= t <= hi and t not in out:
                out.append(t)
    return out


def _prepare(f: NewFile) -> tuple[bytes, int]:
    kind = (f.kind or "PRG").upper()
    if kind not in ("PRG", "SEQ", "USR", "DEL"):
        raise MediaError(f"{f.name}: Commodore file type must be PRG, SEQ or USR (not {f.kind})")
    data = bytes(f.data)
    if kind == "PRG" and f.load is not None:
        data = struct.pack("<H", f.load & 0xFFFF) + data
    return data, TYPES.index(kind)


def create_cbm(kind: str, files: list[NewFile], label: str, disk_id: str = "RR", locked: bool = False) -> bytes:
    geo = Geo(kind, 35 if kind == "d64" else 80)
    img = bytearray(geo.total * 256)
    dt = geo.dir_track
    alloc = Allocator(geo, _alternate(dt, 1, geo.tracks))
    did = encode_name(disk_id or "RR", 2)
    if kind == "d64":
        alloc.take(18, 0)
        dir_order = [s for first in (1, 2, 3) for s in range(first, 19, 3)]   # 1541 order: 1, 4, 7 ...
    else:
        for s in (0, 1, 2):
            alloc.take(40, s)
        dir_order = list(range(3, 40))
    if len(files) > len(dir_order) * 8:
        raise MediaError(f"Too many files: a {kind.upper()} directory holds {len(dir_order) * 8}")
    for s in dir_order:
        alloc.take(dt, s)
    alloc.free[dt].clear()                          # the directory track holds nothing else
    used_dir = dir_order[:max(1, (len(files) + 7) // 8)]

    names = set()
    entries = []
    for f in files:
        data, tcode = _prepare(f)
        name = encode_name(f.name)
        if name in names:
            raise MediaError(f"Two files called {f.name!r}")
        names.add(name)
        chain = []
        n = max(1, (len(data) + 253) // 254)
        for _ in range(n):
            chain.append(alloc.next())
        for i, (t, s) in enumerate(chain):
            o = geo.offset(t, s)
            part = data[i * 254:(i + 1) * 254]
            if i + 1 < len(chain):
                img[o:o + 2] = bytes(chain[i + 1])
            else:
                img[o:o + 2] = bytes([0, len(part) + 1])
            img[o + 2:o + 2 + len(part)] = part
        entries.append((0x80 | tcode | (0x40 if locked else 0), chain[0], name, len(chain)))

    for i, s in enumerate(used_dir):
        o = geo.offset(dt, s)
        nxt = (dt, used_dir[i + 1]) if i + 1 < len(used_dir) else (0, 0xFF)
        for k in range(8):
            idx = i * 8 + k
            eo = o + k * 32
            if k == 0:
                img[eo:eo + 2] = bytes(nxt)
            if idx < len(entries):
                ftype, (t0, s0), name, blocks = entries[idx]
                img[eo + 2] = ftype
                img[eo + 3:eo + 5] = bytes([t0, s0])
                img[eo + 5:eo + 21] = name
                img[eo + 30:eo + 32] = struct.pack("<H", blocks)
    for s in dir_order:                               # unused directory sectors go back to the free pool
        if s not in used_dir:
            alloc.free[dt].add(s)

    lab = encode_name(label or "RETRORAG")
    if kind == "d64":
        o = geo.offset(18, 0)
        img[o:o + 4] = bytes([18, 1, 0x41, 0x00])
        for t in range(1, 36):
            img[o + 4 * t:o + 4 * t + 4] = _bam_entry(alloc.free[t], 3)
        img[o + 0x90:o + 0xA0] = lab
        img[o + 0xA0:o + 0xAB] = b"\xa0\xa0" + did + b"\xa0" + b"2A" + b"\xa0" * 4
    else:
        o = geo.offset(40, 0)
        img[o:o + 4] = bytes([40, 3, 0x44, 0x00])
        img[o + 0x04:o + 0x14] = lab
        img[o + 0x14:o + 0x1D] = b"\xa0\xa0" + did + b"\xa0" + b"3D" + b"\xa0\xa0"
        for s, t0 in ((1, 1), (2, 41)):
            b = geo.offset(40, s)
            img[b:b + 8] = bytes([40, 2] if s == 1 else [0, 0xFF]) + b"D\xbb" + did + b"\xc0\x00"
            for t in range(t0, t0 + 40):
                e = b + 0x10 + 6 * (t - t0)
                img[e:e + 6] = _bam_entry(alloc.free[t], 5)
    return bytes(img)


def _bam_entry(free: set[int], nbytes: int) -> bytes:
    bits = 0
    for s in free:
        bits |= 1 << s
    return bytes([len(free)]) + bits.to_bytes(nbytes, "little")


# ---------------------------------------------------------------- detection
def _sniff_d64(data: bytes, ext: str) -> int:
    if len(data) not in D64_SIZES:
        return 0
    o = Geo("d64", 35).offset(18, 0)
    return 10 if data[o + 2] in (0x41, 0x00) and data[o] in (18, 0) else 3


def _sniff_d81(data: bytes, ext: str) -> int:
    if len(data) not in D81_SIZES:
        return 0
    o = Geo("d81", 80).offset(40, 0)
    return 10 if data[o + 2] == 0x44 else 3


def guess_cbm(host_name: str, data: bytes) -> NewFile:
    """A file from the PC -> what to put on a Commodore disk."""
    stem, _, ext = host_name.rpartition(".")
    stem, ext = (stem, ext.lower()) if stem else (host_name, "")
    if data[:8] == b"C64File\x00" and len(data) >= 26:            # PC64 .P00/.S00 wrapper
        name = data[8:24].split(b"\x00")[0].decode("latin-1")
        kind = {"p": "PRG", "s": "SEQ", "u": "USR"}.get(ext[:1], "PRG")
        return NewFile(name, data[26:], kind)
    name = stem.upper()[:16]
    if ext in ("seq", "usr", "prg"):
        return NewFile(name, data, ext.upper())
    if ext in ("bas", "txt") and _looks_like_basic_text(data):
        return NewFile(name, cbm_tokenise(data.decode("latin-1")), "PRG")
    if _is_ascii(data):                                           # text -> PETSCII SEQ file
        return NewFile(name, ascii_to_petscii(data), "SEQ")
    return NewFile(name, data, "PRG")


def ascii_to_petscii(data: bytes) -> bytes:
    out = bytearray()
    for c in data.replace(b"\r\n", b"\n"):
        if c == 0x0A:
            out.append(0x0D)
        elif 0x61 <= c <= 0x7A:
            out.append(c - 0x20)
        elif 0x41 <= c <= 0x5A:
            out.append(c + 0x80)
        else:
            out.append(c)
    return bytes(out)


def _is_ascii(data: bytes) -> bool:
    return bool(data) and all(32 <= c < 127 or c in (9, 10, 13) for c in data[:4096])


def _looks_like_basic_text(data: bytes) -> bool:
    lines = [ln for ln in data.decode("latin-1", "replace").splitlines() if ln.strip()]
    return bool(lines) and sum(1 for ln in lines if re.match(r"\s*\d+\s*\S", ln)) >= 0.8 * len(lines)


_CBM_OPTS = (Option("disk_id", "Disk ID", [("RR", "RR")], "Two characters shown after the disk name"),
             Option("locked", "Lock files", [("No", False), ("Yes", True)], "Locked files can't be scratched"))

FORMATS_D64 = register(MediaFormat(
    "d64", "Commodore 1541 disk (D64)", (".d64",), "disk",
    ("commodore 64", "c64", "c128", "commodore 128", "vic 20", "vic20", "c16", "commodore 16", "plus/4", "plus4", "pet",
     "cbm", "1541", "sx 64", "sx64", "max machine", "commodore 64c", "c64c", "ultimax"),
    lambda d: CbmDisk(FORMATS_D64, d, "d64"), lambda files, label, **o: create_cbm("d64", files, label, **o),
    _sniff_d64, kinds=("PRG", "SEQ", "USR"), max_name=16, max_label=16, addresses="load",
    options=_CBM_OPTS, basic="cbm", guess=guess_cbm,
    notes="170 KB, up to 144 files. PRG files start with their 2-byte load address; give a load address to "
          "add one to a raw binary."))
FORMATS_D81 = register(MediaFormat(
    "d81", "Commodore 1581 disk (D81)", (".d81",), "disk",
    ("commodore 64", "c64", "c128", "commodore 128", "c65", "1581", "plus/4", "plus4", "c16", "commodore 16"),
    lambda d: CbmDisk(FORMATS_D81, d, "d81"), lambda files, label, **o: create_cbm("d81", files, label, **o),
    _sniff_d81, kinds=("PRG", "SEQ", "USR"), max_name=16, max_label=16, addresses="load",
    options=_CBM_OPTS, basic="cbm", guess=guess_cbm,
    notes="800 KB 3.5\" disk, up to 296 files."))
