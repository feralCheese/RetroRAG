"""Sinclair ZX Spectrum media: tapes (.tap, .tzx) and Interface 1 Microdrive cartridges (.mdr; '.md' also read).

A file is a BASIC "Program" (with an optional autostart line), "Bytes" (machine code / screens, with a start
address) or an array. Headerless tape blocks (used by most turbo loaders) are listed too.
"""
from __future__ import annotations

import struct

from . import Entry, Image, MediaError, MediaFormat, NewFile, register
from .basic import zx_tokenise

KINDS = {0: "Program", 1: "Number array", 2: "Character array", 3: "Bytes"}
KIND_CODES = {"program": 0, "basic": 0, "number array": 1, "character array": 2, "bytes": 3, "code": 3}
PROG = 23755                        # where a BASIC program starts on a standard Spectrum
SECTOR = 543
TZX_MAGIC = b"ZXTape!\x1a"


def _xor(b: bytes) -> int:
    x = 0
    for c in b:
        x ^= c
    return x


def _zx_name(raw: bytes) -> str:
    return "".join(chr(c) if 32 <= c < 127 else "?" for c in raw).rstrip()


class Tape(Image):
    """Common to TAP and TZX once the data blocks have been pulled out."""

    def _pair(self, blocks: list[tuple[bytes, str]]):
        """blocks: (flag + data + checksum, where) -> entries."""
        i, n_headerless = 0, 0
        while i < len(blocks):
            raw, where = blocks[i]
            if len(raw) == 19 and raw[0] == 0x00:
                h = raw[1:18]
                kind = KINDS.get(h[0], f"type {h[0]}")
                name = _zx_name(h[1:11])
                length, p1, p2 = struct.unpack("<HHH", h[11:17])
                meta = {"checksum_ok": _xor(raw[:-1]) == raw[-1], "block": where}
                if h[0] == 0:
                    meta["autostart"] = p1 if p1 < 32768 else None
                    meta["program_length"] = p2
                elif h[0] == 3:
                    meta["start"] = p1
                    if p1 == 16384 and length == 6912:
                        kind = "Bytes"
                        meta["screen"] = True
                data = b""
                if i + 1 < len(blocks) and blocks[i + 1][0][:1] != b"\x00":
                    data = blocks[i + 1][0][1:-1]
                    meta["data_checksum_ok"] = _xor(blocks[i + 1][0][:-1]) == blocks[i + 1][0][-1]
                    i += 1
                self._entries.append(Entry(name or "(no name)", len(data), kind, meta, data))
            else:
                n_headerless += 1
                self._entries.append(Entry(f"(headerless block {n_headerless})", max(0, len(raw) - 2), "Headerless",
                                           {"flag": raw[0] if raw else None, "block": where}, raw[1:-1]))
            i += 1
        self._dedupe()

    def _dedupe(self):
        seen: dict[str, int] = {}
        for e in self._entries:
            k = e.name
            if k in seen:
                seen[k] += 1
                e.name = f"{k} ({seen[k]})"
            else:
                seen[k] = 1

    def read(self, e: Entry) -> bytes:
        return e.ref


class TapImage(Tape):
    def __init__(self, fmt: MediaFormat, data: bytes):
        super().__init__(fmt, data)
        blocks, pos = [], 0
        while pos + 2 <= len(data):
            ln = struct.unpack("<H", data[pos:pos + 2])[0]
            raw = data[pos + 2:pos + 2 + ln]
            if len(raw) < ln:
                self.info.append(f"Tape ends part-way through a block at byte {pos}")
                break
            blocks.append((raw, f"block {len(blocks) + 1}"))
            pos += 2 + ln
        self._pair(blocks)
        self.info.insert(0, f"{len(blocks)} tape blocks")


TZX_FIXED = {0x12: 4, 0x13: None, 0x20: 2, 0x21: None, 0x22: 0, 0x23: 2, 0x24: 2, 0x25: 0, 0x26: None, 0x27: 0,
             0x2A: 4, 0x30: None, 0x31: None, 0x33: None, 0x5A: 9}
TZX_INFO_IDS = {0: "Title", 1: "Publisher", 2: "Author", 3: "Year", 4: "Language", 5: "Type", 6: "Price",
                7: "Loader", 8: "Origin", 0xFF: "Comment"}


class TzxImage(Tape):
    def __init__(self, fmt: MediaFormat, data: bytes):
        super().__init__(fmt, data)
        if not data.startswith(TZX_MAGIC):
            raise MediaError("Not a TZX tape")
        self.info.append(f"TZX version {data[8]}.{data[9]:02d}")
        blocks, pos, n = [], 10, 0
        while pos < len(data):
            bid = data[pos]
            n += 1
            p = pos + 1
            try:
                if bid == 0x10:                    # standard speed data
                    ln = struct.unpack_from("<H", data, p + 2)[0]
                    blocks.append((data[p + 4:p + 4 + ln], f"TZX block {n} (standard)"))
                    pos = p + 4 + ln
                elif bid == 0x11:                  # turbo speed data
                    ln = int.from_bytes(data[p + 15:p + 18], "little")
                    blocks.append((data[p + 18:p + 18 + ln], f"TZX block {n} (turbo)"))
                    pos = p + 18 + ln
                elif bid == 0x14:                  # pure data
                    ln = int.from_bytes(data[p + 7:p + 10], "little")
                    blocks.append((data[p + 10:p + 10 + ln], f"TZX block {n} (pure data)"))
                    pos = p + 10 + ln
                elif bid == 0x15:                  # direct recording (samples): skip
                    pos = p + 8 + int.from_bytes(data[p + 5:p + 8], "little")
                elif bid in (0x18, 0x19):          # CSW / generalised data
                    pos = p + 4 + struct.unpack_from("<I", data, p)[0]
                elif bid == 0x13:
                    pos = p + 1 + 2 * data[p]
                elif bid == 0x21:
                    pos = p + 1 + data[p]
                elif bid == 0x26:
                    pos = p + 2 + 2 * struct.unpack_from("<H", data, p)[0]
                elif bid == 0x28:
                    pos = p + 2 + struct.unpack_from("<H", data, p)[0]
                elif bid == 0x2B:
                    pos = p + 4 + struct.unpack_from("<I", data, p)[0]
                elif bid == 0x30:
                    self.info.append("Text: " + data[p + 1:p + 1 + data[p]].decode("latin-1", "replace"))
                    pos = p + 1 + data[p]
                elif bid == 0x31:
                    pos = p + 2 + data[p + 1]
                elif bid == 0x32:                  # archive info
                    ln = struct.unpack_from("<H", data, p)[0]
                    q, cnt = p + 3, data[p + 2]
                    for _ in range(cnt):
                        tid, tl = data[q], data[q + 1]
                        txt = data[q + 2:q + 2 + tl].decode("latin-1", "replace").replace("\r", " / ")
                        self.info.append(f"{TZX_INFO_IDS.get(tid, 'Info')}: {txt}")
                        if tid == 0 and not self.label:
                            self.label = txt
                        q += 2 + tl
                    pos = p + 2 + ln
                elif bid == 0x33:
                    pos = p + 1 + 3 * data[p]
                elif bid == 0x35:
                    pos = p + 20 + struct.unpack_from("<I", data, p + 16)[0]
                elif bid in TZX_FIXED and TZX_FIXED[bid] is not None:
                    pos = p + TZX_FIXED[bid]
                else:
                    self.info.append(f"Stopped at TZX block {n}: unknown block type ${bid:02X}")
                    break
            except (struct.error, IndexError):
                self.info.append(f"TZX block {n} is cut short")
                break
        self._pair([b for b in blocks if b[0]])


# ---------------------------------------------------------------- writing tapes
def _header_for(f: NewFile) -> tuple[bytes, bytes]:
    kind = (f.kind or "Bytes").strip().lower()
    code = KIND_CODES.get(kind)
    if code is None:
        raise MediaError(f"{f.name}: Spectrum file type must be Program or Bytes (not {f.kind})")
    if code in (1, 2):
        raise MediaError(f"{f.name}: creating array files isn't supported - save them as Bytes")
    data = bytes(f.data)
    name = f.name.encode("latin-1", "replace")[:10].ljust(10, b" ")
    if code == 0:
        auto = f.exec if f.exec is not None and f.exec < 10000 else 32768
        hdr = bytes([0]) + name + struct.pack("<HHH", len(data), auto, len(data))
    else:
        start = f.load if f.load is not None else 32768
        hdr = bytes([3]) + name + struct.pack("<HHH", len(data), start & 0xFFFF, 32768)
    if len(data) > 65535:
        raise MediaError(f"{f.name}: too big for a Spectrum file ({len(data)} bytes)")
    return hdr, data


def _blocks(files: list[NewFile]) -> list[bytes]:
    out = []
    for f in files:
        hdr, data = _header_for(f)
        for flag, body in ((0x00, hdr), (0xFF, data)):
            blk = bytes([flag]) + body
            out.append(blk + bytes([_xor(blk)]))
    return out


def create_tap(files: list[NewFile], label: str = "", **_) -> bytes:
    return b"".join(struct.pack("<H", len(b)) + b for b in _blocks(files))


def create_tzx(files: list[NewFile], label: str = "", pause: int = 1000, **_) -> bytes:
    out = bytearray(TZX_MAGIC + bytes([1, 20]))
    if label:
        t = label.encode("latin-1", "replace")[:255]
        info = bytes([1, 0, len(t)]) + t
        out += bytes([0x32]) + struct.pack("<H", len(info)) + info
    for b in _blocks(files):
        out += bytes([0x10]) + struct.pack("<HH", pause, len(b)) + b
    return bytes(out)


# ---------------------------------------------------------------- Microdrive
def mdr_sum(b: bytes) -> int:
    return sum(b) % 255


class MdrImage(Image):
    def __init__(self, fmt: MediaFormat, data: bytes):
        super().__init__(fmt, data)
        n = len(data) // SECTOR
        if n < 1 or len(data) % SECTOR not in (0, 1):
            raise MediaError("Not a Microdrive cartridge image")
        recs: dict[str, list] = {}
        order: list[str] = []
        good = used = 0
        for i in range(n):
            s = data[i * SECTOR:(i + 1) * SECTOR]
            if not s[0] & 1 or mdr_sum(s[0:14]) != s[14]:
                continue
            good += 1
            if not self.label:
                self.label = _zx_name(s[4:14])
            flg, num, ln = s[15], s[16], s[17] | s[18] << 8
            if flg & 1 or (not flg & 2 and ln == 0):
                continue                               # free sector
            used += 1
            name = _zx_name(s[19:29])
            if name not in recs:
                recs[name] = []
                order.append(name)
            recs[name].append((num, s[30:30 + min(ln, 512)], flg, mdr_sum(s[30:542]) == s[542]))
        self.info = [f"{good} good sectors, {used} used, {good - used} free ({(good - used) * 512 // 1024} KB)",
                     "Write protected" if len(data) % SECTOR == 1 and data[-1] else "Not write protected"]
        for name in order:
            parts = sorted(recs[name], key=lambda r: r[0])
            body = b"".join(p[1] for p in parts)
            complete = bool(parts[-1][2] & 2) and [p[0] for p in parts] == list(range(len(parts)))
            meta = {"records": len(parts), "complete": complete}
            if parts[0][2] & 4 or len(body) < 9:
                self._entries.append(Entry(name, len(body), "Print file", meta, body))
                continue
            typ, length, start, plen, line = struct.unpack("<BHHHH", body[:9])
            kind = KINDS.get(typ, f"type {typ}")
            if typ == 0:
                meta.update(autostart=line if line < 32768 else None, program_length=plen)
            elif typ == 3:
                meta["start"] = start
            data_ = body[9:9 + length]
            self._entries.append(Entry(name, len(data_), kind, meta, data_))

    def read(self, e: Entry) -> bytes:
        return e.ref


def create_mdr(files: list[NewFile], label: str = "", **_) -> bytes:
    nsec = 254
    cart = (label or "RETRORAG").encode("latin-1", "replace")[:10].ljust(10, b" ")
    sectors = []
    for i in range(nsec):
        hd = bytes([0x01, 254 - i, 0, 0]) + cart
        sectors.append(bytearray(hd + bytes([mdr_sum(hd)]) + bytes(SECTOR - 15)))
    nxt = 0
    names = set()
    for f in files:
        hdr, data = _header_for(f)
        typ = hdr[0]
        name = f.name.encode("latin-1", "replace")[:10].ljust(10, b" ")
        if name.upper() in names:
            raise MediaError(f"Two files called {f.name!r}")
        names.add(name.upper())
        if typ == 0:
            line = f.exec if f.exec is not None and f.exec < 10000 else 0xFFFF
            head = struct.pack("<BHHHH", 0, len(data), PROG, len(data), line)
        else:
            head = struct.pack("<BHHHH", 3, len(data), f.load if f.load is not None else 32768, 0xFFFF, 0xFFFF)
        stream = head + data
        nrec = max(1, -(-len(stream) // 512))
        if nxt + nrec > nsec:
            raise MediaError("Cartridge full")
        for r in range(nrec):
            part = stream[r * 512:(r + 1) * 512]
            s = sectors[nxt]
            nxt += 1
            flg = 0x02 if r == nrec - 1 else 0x00
            desc = bytes([flg, r]) + struct.pack("<H", len(part)) + name
            s[15:30] = desc + bytes([mdr_sum(desc)])
            body = part.ljust(512, b"\x00")
            s[30:542] = body
            s[542] = mdr_sum(body)
    for s in sectors[nxt:]:                       # free sectors still carry valid checksums
        desc = bytes(4) + b" " * 10
        s[15:30] = desc + bytes([mdr_sum(desc)])
        s[542] = 0
    return b"".join(bytes(s) for s in sectors) + b"\x00"


# ---------------------------------------------------------------- detection / registration
def _sniff_tap(data: bytes, ext: str) -> int:
    pos, n = 0, 0
    while pos + 2 <= len(data) and n < 5000:
        ln = struct.unpack("<H", data[pos:pos + 2])[0]
        if ln == 0 or pos + 2 + ln > len(data):
            return 0
        pos += 2 + ln
        n += 1
    if pos != len(data) or not n:
        return 0
    first = data[2:3]
    return 8 if first in (b"\x00", b"\xff") else 4


def _sniff_tzx(data: bytes, ext: str) -> int:
    return 10 if data.startswith(TZX_MAGIC) else 0


def _sniff_mdr(data: bytes, ext: str) -> int:
    if len(data) < SECTOR or len(data) % SECTOR not in (0, 1):
        return 0
    good = sum(1 for i in range(min(8, len(data) // SECTOR))
               if data[i * SECTOR] & 1 and mdr_sum(data[i * SECTOR:i * SECTOR + 14]) == data[i * SECTOR + 14])
    return 10 if good >= min(4, len(data) // SECTOR) else 0


def guess_zx(host_name: str, data: bytes) -> NewFile:
    leaf = host_name.replace("\\", "/").split("/")[-1]
    stem, _, ext = leaf.rpartition(".")
    if not stem:
        stem, ext = leaf, ""
    ext = ext.lower()
    if ext in ("bas", "txt", "zxbas") and not _binary(data):
        try:
            prog = zx_tokenise(data.decode("latin-1"))
            first = struct.unpack(">H", prog[:2])[0]
            return NewFile(stem[:10], prog, "Program", exec=first)
        except ValueError:
            pass
    if ext == "scr" or len(data) == 6912:
        return NewFile(stem[:10], data, "Bytes", load=16384)
    return NewFile(stem[:10], data, "Bytes", load=32768)


def _binary(data: bytes) -> bool:
    return any(c < 9 or 13 < c < 32 for c in data[:4096])


_ZX = ("spectrum", "zx spectrum", "sinclair", "zx", "timex", "ts2068", "pentagon", "scorpion", "+2", "+3")
_ZX_NOTES = ("Files are BASIC Programs (autostart line optional) or Bytes (start address). BASIC text is "
             "tokenised so LOAD \"\" runs it.")
register(MediaFormat(
    "tap", "ZX Spectrum tape (TAP)", (".tap",), "tape", _ZX, lambda d: TapImage(_fmt("tap"), d), create_tap,
    _sniff_tap, kinds=("Program", "Bytes"), max_name=10, max_label=0, addresses="start+autostart", basic="zx",
    guess=guess_zx, notes=_ZX_NOTES))
register(MediaFormat(
    "tzx", "ZX Spectrum tape (TZX)", (".tzx",), "tape", _ZX, lambda d: TzxImage(_fmt("tzx"), d), create_tzx,
    _sniff_tzx, kinds=("Program", "Bytes"), max_name=10, max_label=60, addresses="start+autostart", basic="zx",
    guess=guess_zx, notes=_ZX_NOTES + " Reading also lists turbo-loader blocks."))
register(MediaFormat(
    "mdr", "ZX Microdrive cartridge (MDR)", (".mdr", ".md"), "tape",
    ("spectrum", "zx spectrum", "interface 1", "microdrive", "sinclair"),
    lambda d: MdrImage(_fmt("mdr"), d), create_mdr, _sniff_mdr, kinds=("Program", "Bytes"), max_name=10,
    max_label=10, addresses="start+autostart", basic="zx", guess=guess_zx,
    notes="Interface 1 Microdrive cartridge, 254 sectors (about 126 KB). Saved as .mdr - the usual emulator "
          "extension; .md files are read too, but RetroRAG treats new .md files as Markdown text."))


def _fmt(fid: str) -> MediaFormat:
    from . import FORMATS
    return FORMATS[fid]
