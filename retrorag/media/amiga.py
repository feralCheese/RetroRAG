"""Amiga disks (.adf): AmigaDOS OFS and FFS (plus international / dir-cache variants for reading), DD and HD.

New disks are OFS by default so Kickstart 1.x machines can read them; FFS is an option for Kickstart 2.0 and up.
Folders come from "/" in a file's name, e.g. "s/startup-sequence".
"""
from __future__ import annotations

import struct
import time

from . import Entry, Image, MediaError, MediaFormat, NewFile, Option, register

BS = 512
ST_ROOT, ST_USERDIR, ST_FILE, ST_LINKFILE, ST_LINKDIR, ST_SOFTLINK = 1, 2, -3, -4, 4, 3
T_HEADER, T_DATA, T_LIST = 2, 8, 16
SIZES = {901120: "DD (880 KB)", 1802240: "HD (1.76 MB)"}
FS_NAMES = {0: "OFS", 1: "FFS", 2: "OFS international", 3: "FFS international", 4: "OFS dir-cache",
            5: "FFS dir-cache", 6: "OFS long names", 7: "FFS long names"}
BOOT_CODE = bytes.fromhex("43fa00184eaeffa04a80670a20402068001670004e7570ff60fa") + b"dos.library\x00"
EPOCH = 252460800          # 1978-01-01 in Unix time


def _l(b, o):
    return struct.unpack_from(">I", b, o)[0]


def _sl(b, o):
    return struct.unpack_from(">i", b, o)[0]


def _bcpl(b, o, maxlen=30):
    n = min(b[o], maxlen)
    return b[o + 1:o + 1 + n].decode("latin-1")


def _amiga_date(days, mins, ticks) -> str:
    if not days and not mins:
        return ""
    t = EPOCH + days * 86400 + mins * 60 + ticks // 50
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(t))


def _protect(p: int) -> str:
    # bits 0-3 (D E W R) are set when the action is NOT allowed; H S P A are set when present
    flags = "".join(c for c, bit in (("h", 7), ("s", 6), ("p", 5), ("a", 4)) if p & (1 << bit))
    flags += "".join(c if not p & (1 << bit) else "-" for c, bit in (("r", 3), ("w", 2), ("e", 1), ("d", 0)))
    return flags


def boot_checksum(boot: bytes) -> int:
    s = 0
    for i in range(256):
        if i == 1:
            continue
        s += _l(boot, i * 4)
        if s > 0xFFFFFFFF:
            s = (s & 0xFFFFFFFF) + 1
    return ~s & 0xFFFFFFFF


def block_checksum(blk: bytes, off: int = 20) -> int:
    s = 0
    for i in range(128):
        if i * 4 != off:
            s += _l(blk, i * 4)
    return (-s) & 0xFFFFFFFF


class AdfImage(Image):
    def __init__(self, fmt: MediaFormat, data: bytes):
        super().__init__(fmt, data)
        if len(data) not in SIZES or not data.startswith(b"DOS"):
            raise MediaError("Not an AmigaDOS disk (ADF)")
        self.nblocks = len(data) // BS
        self.flags = data[3]
        root = _l(data, 8)
        if not 2 <= root < self.nblocks or self._sec_type(root) != ST_ROOT:
            root = self.nblocks // 2
        self.root = root
        r = self.block(root)
        self.label = _bcpl(r, 0x1B0)
        bootable = boot_checksum(data[:1024]) == _l(data, 4) and any(data[12:1024])
        free = self._free_blocks(r)
        self.info = [f"AmigaDOS {FS_NAMES.get(self.flags & 7, 'DOS%d' % self.flags)}, {SIZES[len(data)]}",
                     "Bootable" if bootable else "Not bootable",
                     (f"{free} blocks free ({free * (488 if not self.flags & 1 else 512) // 1024} KB)"
                      if free is not None else "Free space unknown (bitmap invalid)"),
                     f"Created {_amiga_date(*struct.unpack_from('>III', r, 0x1E4))}"]
        self._walk(root, "", set())

    def block(self, n: int) -> bytes:
        if not 0 <= n < self.nblocks:
            raise MediaError(f"Bad block number {n}")
        return self.data[n * BS:(n + 1) * BS]

    def _sec_type(self, n: int) -> int:
        return _sl(self.data, n * BS + 0x1FC)

    def _free_blocks(self, r: bytes) -> int | None:
        if _l(r, 0x138) != 0xFFFFFFFF:
            return None
        free = 0
        bits_left = self.nblocks - 2
        for k in range(25):
            bm = _l(r, 0x13C + 4 * k)
            if not bm or bits_left <= 0:
                break
            blk = self.block(bm)
            for i in range(1, 128):
                if bits_left <= 0:
                    break
                v = _l(blk, i * 4)
                n = min(32, bits_left)
                free += bin(v & ((1 << n) - 1)).count("1")
                bits_left -= n
        return free

    def _walk(self, dirblk: int, prefix: str, seen: set):
        d = self.block(dirblk)
        kids = []
        for i in range(72):
            n = _l(d, 0x18 + 4 * i)
            hops = 0
            while n and n not in seen and hops < 1000:
                seen.add(n)
                try:
                    h = self.block(n)
                except MediaError:
                    break
                kids.append((n, h))
                n = _l(h, 0x1F0)
                hops += 1
        for n, h in sorted(kids, key=lambda k: _bcpl(k[1], 0x1B0).lower()):
            name = _bcpl(h, 0x1B0)
            st = _sl(h, 0x1FC)
            meta = {"protect": _protect(_l(h, 0x140)), "date": _amiga_date(*struct.unpack_from(">III", h, 0x1A4)),
                    "block": n}
            clen = h[0x148]
            if clen:
                meta["comment"] = h[0x149:0x149 + min(clen, 79)].decode("latin-1")
            path = prefix + name
            if st in (ST_USERDIR, ST_LINKDIR):
                self._entries.append(Entry(path, 0, "folder", meta, n, is_dir=True))
                if st == ST_USERDIR:
                    self._walk(n, path + "/", seen)
            elif st in (ST_FILE, ST_LINKFILE):
                if st == ST_LINKFILE:
                    n = _l(h, 0x1D4) or n           # hard link: the real file's header
                    h = self.block(n)
                self._entries.append(Entry(path, _l(h, 0x144), "file", meta, n))
            elif st == ST_SOFTLINK:
                meta["target"] = h[0x18:0x18 + 288].split(b"\x00")[0].decode("latin-1")
                self._entries.append(Entry(path, 0, "link", meta, None))

    def read(self, e: Entry) -> bytes:
        if e.ref is None or e.is_dir:
            return b""
        h = self.block(e.ref)
        size = _l(h, 0x144)
        blocks = []
        cur, hops = h, 0
        while True:
            cnt = min(_l(cur, 8), 72)
            blocks += [_l(cur, 0x18 + 4 * (71 - i)) for i in range(cnt)]
            ext = _l(cur, 0x1F8)
            if not ext or hops > 2000:
                break
            cur = self.block(ext)
            hops += 1
        ffs = self.flags & 1
        out = bytearray()
        for b in blocks:
            blk = self.block(b)
            if ffs:
                out += blk
            else:
                out += blk[0x18:0x18 + min(_l(blk, 0x0C), 488)]
            if len(out) >= size:
                break
        return bytes(out[:size])


# ---------------------------------------------------------------- writing
def _hash(name: str, intl: bool) -> int:
    h = len(name)
    for ch in name:
        c = ord(ch)
        if 97 <= c <= 122 or (intl and 224 <= c <= 254 and c != 247):
            c -= 32
        h = (h * 13 + c) & 0x7FF
    return h % 72


def _now():
    t = int(time.time()) + time.localtime().tm_gmtoff - EPOCH          # AmigaDOS keeps local time
    return t // 86400, (t % 86400) // 60, (t % 60) * 50


def _put_name(b: bytearray, name: str):
    raw = name.encode("latin-1", "replace")[:30]
    b[0x1B0] = len(raw)
    b[0x1B1:0x1B1 + len(raw)] = raw


def create_adf(files: list[NewFile], label: str, fs: str = "ofs", bootable: bool = False, density: str = "dd") -> bytes:
    nblocks = 3520 if density == "hd" else 1760
    root = nblocks // 2
    ffs = fs == "ffs"
    blocks: dict[int, bytearray] = {}
    used = {0, 1, root, root + 1}
    cursor = [root + 2]
    days, mins, ticks = _now()

    def alloc() -> int:
        for _ in range(nblocks):
            n = cursor[0]
            cursor[0] = n + 1 if n + 1 < nblocks else 2
            if n not in used:
                used.add(n)
                return n
        raise MediaError("Disk full")

    def header(n: int, st: int, name: str, parent: int) -> bytearray:
        b = bytearray(BS)
        struct.pack_into(">II", b, 0, T_HEADER, n)
        struct.pack_into(">III", b, 0x1A4, days, mins, ticks)
        _put_name(b, name)
        struct.pack_into(">I", b, 0x1F4, parent)
        struct.pack_into(">i", b, 0x1FC, st)
        blocks[n] = b
        return b

    rootb = bytearray(BS)
    struct.pack_into(">IIIII", rootb, 0, T_HEADER, 0, 0, 72, 0)
    struct.pack_into(">II", rootb, 0x138, 0xFFFFFFFF, root + 1)
    struct.pack_into(">III", rootb, 0x1A4, days, mins, ticks)
    _put_name(rootb, (label or "RetroRAG")[:30])
    struct.pack_into(">IIIIII", rootb, 0x1D8, days, mins, ticks, days, mins, ticks)
    struct.pack_into(">i", rootb, 0x1FC, ST_ROOT)
    blocks[root] = rootb
    chains: dict[int, dict[int, list[int]]] = {root: {}}     # dir block -> hash slot -> header blocks
    dirs: dict[str, int] = {"": root}
    names: dict[int, set] = {root: set()}

    def add_to_dir(d: int, name: str, n: int):
        low = name.lower()
        if low in names[d]:
            raise MediaError(f"Two entries called {name!r} in the same folder")
        names[d].add(low)
        chains[d].setdefault(_hash(name, False), []).append(n)

    def ensure_dir(path: str) -> int:
        if path in dirs:
            return dirs[path]
        parent_path, _, name = path.rpartition("/")
        parent = ensure_dir(parent_path)
        n = alloc()
        header(n, ST_USERDIR, name, parent)
        chains[n], names[n] = {}, set()
        add_to_dir(parent, name, n)
        dirs[path] = n
        return n

    for f in files:
        parts = [p for p in f.name.replace("\\", "/").split("/") if p]
        if not parts:
            raise MediaError("A file needs a name")
        for p in parts:
            if len(p) > 30 or ":" in p:
                raise MediaError(f"AmigaDOS names are up to 30 characters, without ':' ({p!r})")
        parent = ensure_dir("/".join(parts[:-1]))
        data = bytes(f.data)
        hn = alloc()
        h = header(hn, ST_FILE, parts[-1], parent)
        struct.pack_into(">I", h, 0x144, len(data))
        add_to_dir(parent, parts[-1], hn)
        per = 512 if ffs else 488
        nblk = -(-len(data) // per)
        data_blocks = []
        for i in range(nblk):
            dn = alloc()
            data_blocks.append(dn)
        for i, dn in enumerate(data_blocks):
            chunk = data[i * per:(i + 1) * per]
            b = bytearray(BS)
            if ffs:
                b[:len(chunk)] = chunk
            else:
                nxt = data_blocks[i + 1] if i + 1 < nblk else 0
                struct.pack_into(">IIIII", b, 0, T_DATA, hn, i + 1, len(chunk), nxt)
                b[0x18:0x18 + len(chunk)] = chunk
                struct.pack_into(">I", b, 20, block_checksum(b))
            blocks[dn] = b
        # pointer tables: 72 per block, the file header first, then extension blocks
        cur, k = h, 0
        if data_blocks:
            struct.pack_into(">I", h, 0x10, data_blocks[0])
        while True:
            part = data_blocks[k:k + 72]
            struct.pack_into(">I", cur, 8, len(part))
            for i, dn in enumerate(part):
                struct.pack_into(">I", cur, 0x18 + 4 * (71 - i), dn)
            k += 72
            if k >= len(data_blocks):
                break
            en = alloc()
            ext = bytearray(BS)
            struct.pack_into(">II", ext, 0, T_LIST, en)
            struct.pack_into(">I", ext, 0x1F4, hn)
            struct.pack_into(">i", ext, 0x1FC, ST_FILE)
            blocks[en] = ext
            struct.pack_into(">I", cur, 0x1F8, en)
            cur = ext

    # hash tables and chains, then checksums
    for d, slots in chains.items():
        db = blocks[d]
        for slot, members in slots.items():
            struct.pack_into(">I", db, 0x18 + 4 * slot, members[0])
            for a, b in zip(members, members[1:]):
                struct.pack_into(">I", blocks[a], 0x1F0, b)
    for n, b in blocks.items():
        if _l(b, 0) in (T_HEADER, T_LIST):
            struct.pack_into(">I", b, 20, block_checksum(b))
    # bitmap: bit set = block free, starting from block 2
    bm = bytearray(BS)
    for blk in range(2, nblocks):
        if blk not in used:
            i = blk - 2
            o = 4 + (i // 32) * 4
            struct.pack_into(">I", bm, o, _l(bm, o) | (1 << (i % 32)))
    struct.pack_into(">I", bm, 0, block_checksum(bm, 0))
    blocks[root + 1] = bm

    img = bytearray(nblocks * BS)
    boot = bytearray(1024)
    boot[0:4] = b"DOS" + bytes([1 if ffs else 0])
    struct.pack_into(">I", boot, 8, root)
    if bootable:
        boot[12:12 + len(BOOT_CODE)] = BOOT_CODE
        struct.pack_into(">I", boot, 4, boot_checksum(boot))
    img[0:1024] = boot
    for n, b in blocks.items():
        img[n * BS:(n + 1) * BS] = b
    return bytes(img)


def _sniff(data: bytes, ext: str) -> int:
    return 10 if len(data) in SIZES and data.startswith(b"DOS") and data[3] < 8 else 0


def guess_amiga(host_name: str, data: bytes) -> NewFile:
    return NewFile(host_name.replace("\\", "/").split("/")[-1][:30], data)


register(MediaFormat(
    "adf", "Amiga disk (ADF)", (".adf",), "disk", ("amiga", "a500", "a600", "a1200", "a2000", "a3000", "a4000", "cdtv"),
    lambda d: AdfImage(_fmt(), d), create_adf, _sniff, kinds=("file",), max_name=30, max_label=30,
    options=(Option("fs", "File system", [("OFS (Kickstart 1.x and up)", "ofs"), ("FFS (Kickstart 2.0 and up)", "ffs")]),
             Option("bootable", "Bootable", [("No", False), ("Yes - boots to a CLI", True)],
                    "Writes the standard AmigaDOS boot block; add s/startup-sequence to run something"),
             Option("density", "Density", [("DD 880 KB", "dd"), ("HD 1.76 MB", "hd")])),
    dirs=True, guess=guess_amiga,
    notes="Names up to 30 characters; folders with / in a name (e.g. s/startup-sequence)."))


def _fmt():
    from . import FORMATS
    return FORMATS["adf"]
