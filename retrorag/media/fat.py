"""FAT floppies: PC / DOS (.img .ima), Atari ST (.st, and .msa compressed), plus FAT12/16 partitions in hard-disk
images (read only).

Reading understands sub-folders and Windows long file names. New disks use 8.3 names (what DOS and TOS expect);
folders are made from "/" in a file's name, e.g. "GAMES/TETRIS.PRG".
"""
from __future__ import annotations

import random
import re
import struct
import time

from . import Entry, Image, MediaError, MediaFormat, NewFile, Option, register

# geometry: total sectors, sectors/cluster, root entries, media byte, sectors/FAT, sectors/track, heads
GEOMS = {
    "pc1440": (2880, 1, 224, 0xF0, 9, 18, 2), "pc720": (1440, 2, 112, 0xF9, 3, 9, 2),
    "pc360": (720, 2, 112, 0xFD, 2, 9, 2), "pc1200": (2400, 1, 224, 0xF9, 7, 15, 2),
    "pc2880": (5760, 2, 240, 0xF0, 9, 36, 2),
    "st720": (1440, 2, 112, 0xF9, 5, 9, 2), "st360": (720, 2, 112, 0xF8, 5, 9, 1),
    "st800": (1600, 2, 112, 0xF9, 5, 10, 2),
}


def _u16(b, o):
    return b[o] | b[o + 1] << 8


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _bpb_ok(b: bytes, o: int = 0) -> bool:
    if len(b) < o + 512:
        return False
    bps, spc, res, nf, rootn, spf = _u16(b, o + 11), b[o + 13], _u16(b, o + 14), b[o + 16], _u16(b, o + 17), _u16(b, o + 22)
    return (bps in (128, 256, 512, 1024, 2048, 4096) and spc in (1, 2, 4, 8, 16, 32, 64, 128) and res >= 1
            and nf in (1, 2) and 0 < rootn <= 4096 and 0 < spf <= 256)


def _dos_datetime(d: int, t: int) -> str:
    if not d:
        return ""
    return f"{1980 + (d >> 9)}-{(d >> 5) & 15:02d}-{d & 31:02d} {t >> 11:02d}:{(t >> 5) & 63:02d}"


class FatImage(Image):
    def __init__(self, fmt: MediaFormat, data: bytes):
        super().__init__(fmt, data)
        base = 0
        if not _bpb_ok(data):
            base = self._partition(data)
        b = data
        self.base = base
        self.bps = bps = _u16(b, base + 11)
        self.spc = b[base + 13]
        res, nf, rootn = _u16(b, base + 14), b[base + 16], _u16(b, base + 17)
        tot = _u16(b, base + 19) or _u32(b, base + 32)
        spf = _u16(b, base + 22)
        if not tot or base + tot * bps > len(b) + 64 * bps:
            tot = (len(b) - base) // bps
        self.fat_off = base + res * bps
        self.root_off = self.fat_off + nf * spf * bps
        self.root_len = rootn * 32
        self.data_off = self.root_off + -(-self.root_len // bps) * bps
        self.nclusters = (tot - (self.data_off - base) // bps) // max(1, self.spc)
        self.fat16 = self.nclusters >= 4085
        self.csize = self.spc * bps
        media = b[base + 21]
        free = sum(1 for c in range(2, self.nclusters + 2) if self._fat(c) == 0)
        self.info = [f"FAT{'16' if self.fat16 else '12'}, {tot * bps // 1024} KB, {bps}-byte sectors"
                     + (f", partition at sector {base // 512}" if base else ""),
                     f"{free * self.csize // 1024} KB free ({free} clusters of {self.csize} bytes)",
                     f"Media byte ${media:02X}, {_u16(b, base + 24)} sectors/track, {_u16(b, base + 26)} sides"]
        self._walk(self.data[self.root_off:self.root_off + self.root_len], "", set())

    @staticmethod
    def _partition(b: bytes) -> int:
        if len(b) >= 512 and b[510:512] == b"\x55\xaa":
            for k in range(4):
                e = 0x1BE + 16 * k
                ptype, lba = b[e + 4], _u32(b, e + 8)
                if ptype in (0x01, 0x04, 0x06, 0x0E) and lba and _bpb_ok(b, lba * 512):
                    return lba * 512
        raise MediaError("No FAT file system found")

    def _fat(self, n: int) -> int:
        b = self.data
        if self.fat16:
            return _u16(b, self.fat_off + 2 * n)
        o = self.fat_off + n * 3 // 2
        v = b[o] | b[o + 1] << 8
        return (v >> 4) if n & 1 else (v & 0xFFF)

    def _chain(self, first: int, size: int | None = None) -> bytes:
        out, c, seen = bytearray(), first, set()
        eoc = 0xFFF8 if self.fat16 else 0xFF8
        while 2 <= c < eoc and c not in seen and len(seen) <= self.nclusters:
            seen.add(c)
            o = self.data_off + (c - 2) * self.csize
            out += self.data[o:o + self.csize]
            if size is not None and len(out) >= size:
                break
            c = self._fat(c)
        return bytes(out[:size] if size is not None else out)

    def _walk(self, raw: bytes, prefix: str, seen: set):
        lfn: dict[int, str] = {}
        for o in range(0, len(raw) - 31, 32):
            e = raw[o:o + 32]
            if e[0] == 0:
                break
            if e[0] == 0xE5:
                lfn = {}
                continue
            attr = e[11]
            if attr == 0x0F:                             # long file name piece
                seq = e[0] & 0x1F
                part = (e[1:11] + e[14:26] + e[28:32]).decode("utf-16-le", "replace")
                lfn[seq] = part.split("\x00")[0]
                continue
            short = e[0:8].decode("latin-1").rstrip()
            if e[0] == 0x05:
                short = "\xe5" + short[1:]
            ext = e[8:11].decode("latin-1").rstrip()
            name = short + ("." + ext if ext else "")
            if lfn:
                name = "".join(lfn[k] for k in sorted(lfn)) or name
                lfn = {}
            if attr & 0x08:                              # volume label
                if not prefix and not attr & 0x10:
                    self.label = (short + ext).strip()
                continue
            if short in (".", ".."):
                continue
            first = _u16(e, 26)
            size = _u32(e, 28)
            meta = {"date": _dos_datetime(_u16(e, 24), _u16(e, 22)),
                    "attrs": "".join(f for f, bit in (("R", 1), ("H", 2), ("S", 4), ("A", 32)) if attr & bit),
                    "locked": bool(attr & 1), "short_name": short + ("." + ext if ext else "")}
            path = prefix + name
            if attr & 0x10:
                self._entries.append(Entry(path, 0, "folder", meta, None, is_dir=True))
                if first and first not in seen and len(seen) < 2000:
                    seen.add(first)
                    self._walk(self._chain(first), path + "/", seen)
            else:
                self._entries.append(Entry(path, size, "file", meta, first))

    def read(self, e: Entry) -> bytes:
        return self._chain(e.ref, e.size) if e.ref else b""


# ---------------------------------------------------------------- writing
_BAD83 = re.compile(r"[^A-Z0-9!#$%&'()\-@^_`{}~]")


def short_name(name: str, taken: set[str]) -> tuple[bytes, str]:
    name = name.strip().upper()
    stem, _, ext = name.rpartition(".") if "." in name.lstrip(".") else (name, "", "")
    stem = _BAD83.sub("_", stem.replace(" ", "")) or "FILE"
    ext = _BAD83.sub("_", ext.replace(" ", ""))[:3]
    base = stem[:8]
    k = 1
    while (base, ext) in taken or (base + "." + ext) in taken:
        tail = f"~{k}"
        base = stem[:8 - len(tail)] + tail
        k += 1
    taken.add((base, ext))
    return base.ljust(8).encode("latin-1") + ext.ljust(3).encode("latin-1"), base + ("." + ext if ext else "")


def _dos_now():
    t = time.localtime()
    return (((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday,
            (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2))


def _pc_boot_code() -> bytes:
    """'Non-system disk' message for anyone who tries to boot the floppy (code at 0x3E)."""
    msg = b"Not a system disk. Press a key to try again.\r\n\x00"
    code = bytearray()
    code += b"\xfa\x31\xc0\x8e\xd8\x8e\xd0\xbc\x00\x7c\xfb\xfc"       # cli; ds=ss=0; sp=7C00; sti; cld
    si_at = len(code)
    code += b"\xbe\x00\x00"                                           # mov si, msg
    loop = len(code)
    code += b"\xac\x08\xc0\x74\x06\xb4\x0e\xcd\x10"                   # lodsb; or al,al; jz done; int 10h
    code += bytes([0xEB, (loop - (len(code) + 2)) & 0xFF])            # jmp loop
    code += b"\x31\xc0\xcd\x16\xcd\x19"                               # done: wait for a key; int 19h (reboot)
    addr = 0x7C00 + 0x3E + len(code)
    code[si_at + 1:si_at + 3] = struct.pack("<H", addr)
    return bytes(code) + msg


def create_fat(files: list[NewFile], label: str, geom: str, atari: bool) -> bytes:
    if geom not in GEOMS:
        raise MediaError(f"Unknown disk size {geom}")
    tot, spc, rootn, media, spf, spt, heads = GEOMS[geom]
    bps = 512
    img = bytearray(tot * bps)
    b = img
    serial = random.getrandbits(32)
    if atari:                    # TOS layout, but with a PC jump + signature so PCs and PC tools can read it too
        b[0:8] = b"\xeb\x34\x90RRAG "
        b[8:11] = serial.to_bytes(4, "little")[:3]
        b[510:512] = b"\x55\xaa"
    else:
        b[0:3] = b"\xeb\x3c\x90"
        b[3:11] = b"MSDOS5.0"
    struct.pack_into("<HBHBHHBHHHII", b, 11, bps, spc, 1, 2, rootn, tot, media, spf, spt, heads, 0, 0)
    if not atari:
        vol = (label or "NO NAME").upper()[:11].ljust(11).encode("latin-1", "replace")
        b[0x24:0x3E] = bytes([0x00, 0x00, 0x29]) + serial.to_bytes(4, "little") + vol + b"FAT12   "
        code = _pc_boot_code()
        b[0x3E:0x3E + len(code)] = code
        b[510:512] = b"\x55\xaa"
    fat_off, root_off = bps, bps + 2 * spf * bps
    data_off = root_off + rootn * 32
    nclusters = (tot - data_off // bps) // spc
    csize = spc * bps
    fat = [0] * (nclusters + 2)
    fat[0], fat[1] = 0xF00 | media, 0xFFF
    nxt = [2]

    def alloc(nbytes: int) -> int:
        n = max(1, -(-nbytes // csize))
        if nxt[0] + n > nclusters + 2:
            raise MediaError("Disk full")
        first = nxt[0]
        for c in range(first, first + n):
            fat[c] = c + 1 if c + 1 < first + n else 0xFFF
        nxt[0] += n
        return first

    # build the folder tree from "a/b/name" paths
    tree: dict = {"files": [], "dirs": {}}
    for f in files:
        parts = [p for p in f.name.replace("\\", "/").split("/") if p]
        if not parts:
            raise MediaError("A file needs a name")
        node = tree
        for p in parts[:-1]:
            node = node["dirs"].setdefault(p.upper(), {"files": [], "dirs": {}, "name": p})
        node["files"].append((parts[-1], f.data, f))
    d_date, d_time = _dos_now()

    def entry(raw11: bytes, attr: int, cluster: int, size: int) -> bytes:
        return raw11 + bytes([attr]) + bytes(10) + struct.pack("<HHHI", d_time, d_date, cluster, size)

    def write_dir(node, cluster_self: int, cluster_parent: int) -> bytes:
        out = bytearray()
        if cluster_self:
            out += entry(b".          ", 0x10, cluster_self, 0) + entry(b"..         ", 0x10, cluster_parent, 0)
        taken: set = set()
        for key, sub in node["dirs"].items():
            raw, _ = short_name(sub["name"], taken)
            sub["raw"] = raw
        for name, data, f in node["files"]:
            raw, _ = short_name(name, taken)
            c = alloc(len(data)) if data else 0
            if data:
                o = data_off + (c - 2) * csize
                b[o:o + len(data)] = data
            attr = 0x20 | (0x01 if (f.kind or "").lower() in ("locked", "read-only") else 0)
            out += entry(raw, attr, c, len(data))
        for key, sub in node["dirs"].items():
            size = 32 * (2 + len(sub["files"]) + len(sub["dirs"]))
            c = alloc(size)
            out += entry(sub["raw"], 0x10, c, 0)
            body = write_dir(sub, c, cluster_self)
            o = data_off + (c - 2) * csize
            b[o:o + len(body)] = body
        return bytes(out)

    root = bytearray()
    if label:
        root += entry(label.upper()[:11].ljust(11).encode("latin-1", "replace"), 0x08, 0, 0)
    root += write_dir(tree, 0, 0)
    if len(root) > rootn * 32:
        raise MediaError(f"Too many files in the top folder (max {rootn - (1 if label else 0)}) - use folders")
    b[root_off:root_off + len(root)] = root
    fatb = bytearray(spf * bps)
    for n, v in enumerate(fat):
        o = n * 3 // 2
        if n & 1:
            fatb[o] = (fatb[o] & 0x0F) | ((v & 0x0F) << 4)
            fatb[o + 1] = (v >> 4) & 0xFF
        else:
            fatb[o] = v & 0xFF
            fatb[o + 1] = (fatb[o + 1] & 0xF0) | ((v >> 8) & 0x0F)
    for k in range(2):
        o = fat_off + k * spf * bps
        b[o:o + len(fatb)] = fatb
    if atari:                                   # never let TOS think the boot sector is executable
        while sum(struct.unpack(">256H", bytes(b[:512]))) & 0xFFFF == 0x1234:
            b[8] = (b[8] + 1) & 0xFF
    return bytes(img)


# ---------------------------------------------------------------- Atari MSA (compressed ST)
def msa_decode(m: bytes) -> bytes:
    if m[:2] != b"\x0e\x0f":
        raise MediaError("Not an MSA image")
    spt, sides, t0, t1 = struct.unpack(">HHHH", m[2:10])
    tsize = spt * 512
    out, pos = bytearray(), 10
    for _ in range((t1 - t0 + 1) * (sides + 1)):
        ln = struct.unpack(">H", m[pos:pos + 2])[0]
        pos += 2
        chunk = m[pos:pos + ln]
        pos += ln
        if ln == tsize:
            out += chunk
            continue
        i, tr = 0, bytearray()
        while i < len(chunk) and len(tr) < tsize:
            if chunk[i] == 0xE5 and i + 3 < len(chunk):
                tr += bytes([chunk[i + 1]]) * struct.unpack(">H", chunk[i + 2:i + 4])[0]
                i += 4
            else:
                tr.append(chunk[i])
                i += 1
        out += tr[:tsize].ljust(tsize, b"\x00")
    return bytes(out)


def msa_encode(raw: bytes, spt: int = 9, sides: int = 2) -> bytes:
    tsize = spt * 512
    tracks = len(raw) // (tsize * sides)
    out = bytearray(b"\x0e\x0f" + struct.pack(">HHHH", spt, sides - 1, 0, tracks - 1))
    for k in range(tracks * sides):
        tr = raw[k * tsize:(k + 1) * tsize]
        enc, i = bytearray(), 0
        while i < len(tr):
            j = i
            while j < len(tr) and tr[j] == tr[i] and j - i < 0xFFFF:
                j += 1
            run = j - i
            if run >= 5 or tr[i] == 0xE5:
                enc += bytes([0xE5, tr[i]]) + struct.pack(">H", run)
            else:
                enc += tr[i:j]
            i = j
        body = enc if len(enc) < tsize else tr
        out += struct.pack(">H", len(body)) + body
    return bytes(out)


# ---------------------------------------------------------------- detection / registration
def _sniff_fat(data: bytes, ext: str) -> int:
    if _bpb_ok(data):
        return 8 if data[510:512] == b"\x55\xaa" else 6
    try:
        FatImage._partition(data)
        return 4
    except MediaError:
        return 0


def _sniff_st(data: bytes, ext: str) -> int:
    if not _bpb_ok(data):
        return 0
    if _u16(data, 22) == 5 and data[21] in (0xF8, 0xF9):      # TOS formats floppies with 5-sector FATs
        return 9
    pc_style = data[510:512] == b"\x55\xaa" and data[0] == 0xEB
    return 4 if pc_style else 9


def _sniff_msa(data: bytes, ext: str) -> int:
    return 10 if data[:2] == b"\x0e\x0f" and len(data) > 10 and data[5] in (0, 1) else 0


def guess_dos(host_name: str, data: bytes) -> NewFile:
    return NewFile(host_name.replace("\\", "/").split("/")[-1].upper(), data)


_PC_SIZES = Option("size", "Disk size", [("1.44 MB (3.5\" HD)", "pc1440"), ("720 KB (3.5\" DD)", "pc720"),
                                         ("360 KB (5.25\" DD)", "pc360"), ("1.2 MB (5.25\" HD)", "pc1200"),
                                         ("2.88 MB (3.5\" ED)", "pc2880")])
_ST_SIZES = Option("size", "Disk size", [("720 KB double-sided", "st720"), ("360 KB single-sided", "st360"),
                                         ("800 KB (10 sectors/track)", "st800")])

register(MediaFormat(
    "img", "PC / DOS floppy (IMG)", (".img", ".ima", ".vfd", ".flp"), "disk",
    ("pc", "ibm", "dos", "ms-dos", "msdos", "x86", "8086", "286", "386", "486", "ibm pc", "pc xt", "pc at"),
    lambda d: FatImage(get_fmt("img"), d),
    lambda files, label, size="pc1440", **o: create_fat(files, label, size, atari=False),
    _sniff_fat, kinds=("file",), max_name=12, max_label=11, options=(_PC_SIZES,), dirs=True, guess=guess_dos,
    notes="FAT12 floppy. Names are 8.3 (long names are shortened); make folders with / in a name."))
register(MediaFormat(
    "st", "Atari ST floppy (ST)", (".st",), "disk",
    ("atari st", "atari ste", "atari tt", "atari falcon", "atari mega", "st", "ste", "mega st", "falcon", "tos"),
    lambda d: FatImage(get_fmt("st"), d),
    lambda files, label, size="st720", **o: create_fat(files, label, size, atari=True),
    _sniff_st, kinds=("file",), max_name=12, max_label=11, options=(_ST_SIZES,), dirs=True, guess=guess_dos,
    notes="TOS (FAT12) floppy, readable by PCs too. Names are 8.3; make folders with / in a name."))
register(MediaFormat(
    "msa", "Atari ST compressed floppy (MSA)", (".msa",), "disk",
    ("atari st", "atari ste", "st", "ste", "mega st", "falcon"),
    lambda d: _MsaImage(get_fmt("msa"), d),
    lambda files, label, size="st720", **o: msa_encode(create_fat(files, label, size, atari=True),
                                                       GEOMS[size][5], GEOMS[size][6]),
    _sniff_msa, kinds=("file",), max_name=12, max_label=11, options=(_ST_SIZES,), dirs=True, guess=guess_dos,
    notes="Magic Shadow Archiver image: an ST floppy, run-length compressed."))


class _MsaImage(FatImage):
    def __init__(self, fmt, data):
        super().__init__(fmt, msa_decode(data))


def get_fmt(fid: str) -> MediaFormat:
    from . import FORMATS
    return FORMATS[fid]
