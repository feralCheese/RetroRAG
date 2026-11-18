"""Acorn disks: DFS (.ssd single-sided, .dsd double-sided interleaved) and ADFS old map (.adl / .adm / .ads).

Files keep their load and execution addresses (written to .inf files when extracted). DFS names are
"D.NAME" (one-letter directory, default $); ADFS has real folders, written here with "/".
"""
from __future__ import annotations

import random
import re

from . import Entry, Image, MediaError, MediaFormat, NewFile, Option, register
from .basic import BBC_EXEC, BBC_LOAD, bbc_is_basic, bbc_tokenise

SEC = 256
BOOT_OPTS = {0: "none", 1: "*LOAD $.!BOOT", 2: "*RUN $.!BOOT", 3: "*EXEC $.!BOOT"}


def _addr18(lo16: int, hi2: int) -> int:
    v = lo16 | hi2 << 16
    return 0xFF0000 | lo16 if hi2 == 3 else v


def _kind(data: bytes, load: int, exe: int) -> str:
    if bbc_is_basic(data) and data[:1] == b"\x0d":
        return "BASIC"
    return "file"


# ============================================================================ DFS
class DfsImage(Image):
    def __init__(self, fmt: MediaFormat, data: bytes, double: bool):
        super().__init__(fmt, data)
        self.double = double
        info, labels = [], []
        for side in ((0, 1) if double else (0,)):
            try:
                cat = self._catalogue(side)
            except MediaError:
                if side == 0:
                    raise
                continue
            title, opt, total, entries, used = cat
            labels.append(title)
            prefix = "" if side == 0 else ":2."
            info.append(f"{'Side ' + str(side * 2) + ': ' if double else ''}{len(entries)} files, "
                        f"{total - used} of {total} sectors free, boot option {opt} ({BOOT_OPTS[opt]})")
            for e in entries:
                e.name = prefix + e.name
                self._entries.append(e)
        self.label = labels[0] if labels else ""
        self.info = info

    def sector(self, side: int, n: int) -> bytes:
        track, s = divmod(n, 10)
        o = ((track * 2 + side) if self.double else track) * 2560 + s * SEC
        return self.data[o:o + SEC].ljust(SEC, b"\x00")

    def _catalogue(self, side: int):
        s0, s1 = self.sector(side, 0), self.sector(side, 1)
        if s1[5] % 8 or s1[5] > 248:
            raise MediaError("Not a DFS catalogue")
        total = s1[7] | (s1[6] & 3) << 8
        title = (s0[0:8] + s1[0:4]).split(b"\x00")[0].decode("latin-1").strip()
        opt = (s1[6] >> 4) & 3
        entries, used = [], 2
        for i in range(s1[5] // 8):
            n = s0[8 + 8 * i:16 + 8 * i]
            m = s1[8 + 8 * i:16 + 8 * i]
            name = bytes(c & 0x7F for c in n[:7]).decode("latin-1").rstrip(" \x00")
            d = chr(n[7] & 0x7F)
            hi = m[6]
            load = _addr18(m[0] | m[1] << 8, (hi >> 2) & 3)
            exe = _addr18(m[2] | m[3] << 8, (hi >> 6) & 3)
            length = m[4] | m[5] << 8 | ((hi >> 4) & 3) << 16
            start = m[7] | (hi & 3) << 8
            used = max(used, start + -(-length // SEC))
            data = b"".join(self.sector(side, start + k) for k in range(-(-length // SEC)))[:length]
            entries.append(Entry(f"{d}.{name}", length, _kind(data, load, exe),
                                 {"load": load, "exec": exe, "locked": bool(n[7] & 0x80), "sector": start}, data))
        entries.sort(key=lambda e: e.meta["sector"])
        return title, opt, total, entries, used

    def read(self, e: Entry) -> bytes:
        return e.ref


def _split_dfs_name(name: str) -> tuple[str, str]:
    name = name.strip()
    m = re.match(r"^(?::\d\.)?([^.])\.(.+)$", name)
    d, n = (m.group(1), m.group(2)) if m else ("$", name)
    n = re.sub(r'[\s.:"#*]', "_", n)[:7]
    if not n:
        raise MediaError("A file needs a name")
    return d, n


def _enc18(v: int) -> tuple[int, int]:
    if v >= 0x40000:                     # &FFxxxx and friends: the I/O processor
        return v & 0xFFFF, 3
    return v & 0xFFFF, (v >> 16) & 3


def _addresses(f: NewFile) -> tuple[int, int]:
    load, exe = f.load, f.exec
    if (f.kind or "").upper() == "BASIC" or (load is None and bbc_is_basic(f.data) and f.data[:1] == b"\x0d"):
        load = BBC_LOAD if load is None else load
        exe = BBC_EXEC if exe is None else exe
    return load or 0, exe or 0


def _dfs_side(files: list[NewFile], label: str, total: int, boot: int) -> tuple[bytes, list[NewFile]]:
    """One DFS side; returns its sectors and the files that didn't fit."""
    side = bytearray(total * SEC)
    placed, rest, nxt = [], [], 2
    seen = set()
    for f in files:
        d, n = _split_dfs_name(f.name)
        key = (d.upper(), n.upper())
        if key in seen:
            raise MediaError(f"Two files called {d}.{n}")
        need = -(-len(f.data) // SEC)
        if len(placed) >= 31 or nxt + need > total:
            rest.append(f)
            continue
        seen.add(key)
        load, exe = _addresses(f)
        side[nxt * SEC:nxt * SEC + len(f.data)] = f.data
        placed.append((d, n, load, exe, len(f.data), nxt, (f.kind or "").lower() == "locked"))
        nxt += need
    s0, s1 = bytearray(SEC), bytearray(SEC)
    t = (label or "").encode("latin-1", "replace")[:12].ljust(12, b" ")
    s0[0:8], s1[0:4] = t[:8], t[8:12]
    s1[5] = len(placed) * 8
    s1[6] = (boot & 3) << 4 | (total >> 8) & 3
    s1[7] = total & 0xFF
    for i, (d, n, load, exe, length, start, locked) in enumerate(sorted(placed, key=lambda p: -p[5])):
        s0[8 + 8 * i:15 + 8 * i] = n.encode("latin-1", "replace").ljust(7, b" ")
        s0[15 + 8 * i] = ord(d) | (0x80 if locked else 0)
        l16, lh = _enc18(load)
        e16, eh = _enc18(exe)
        s1[8 + 8 * i:16 + 8 * i] = bytes([l16 & 0xFF, l16 >> 8, e16 & 0xFF, e16 >> 8, length & 0xFF,
                                         (length >> 8) & 0xFF,
                                         (eh << 6) | (((length >> 16) & 3) << 4) | (lh << 2) | (start >> 8) & 3,
                                         start & 0xFF])
    side[0:SEC], side[SEC:2 * SEC] = s0, s1
    return bytes(side), rest


def create_dfs(files: list[NewFile], label: str, double: bool = False, tracks: int = 80, boot: int = 0) -> bytes:
    total = tracks * 10
    side0, rest = _dfs_side(files, label, total, boot)
    if not double:
        if rest:
            raise MediaError(f"Doesn't fit on one side ({len(rest)} file(s) left over: DFS holds 31 files / "
                             f"{total * SEC // 1024} KB) - use a double-sided .dsd")
        return side0
    side1, rest = _dfs_side(rest, label, total, 0)
    if rest:
        raise MediaError(f"Doesn't fit on the disk ({len(rest)} file(s) left over)")
    out = bytearray()
    for t in range(tracks):
        out += side0[t * 2560:(t + 1) * 2560] + side1[t * 2560:(t + 1) * 2560]
    return bytes(out)


def _sniff_dfs(data: bytes, ext: str, double: bool) -> int:
    if len(data) < 512 or data[0x201:0x205] == b"Hugo":
        return 0
    s1 = data[256:512]
    if s1[5] % 8 or s1[5] > 248:
        return 0
    total = s1[7] | (s1[6] & 3) << 8
    if total < 2 or total > 800:
        return 0
    for i in range(s1[5] // 8):
        m = s1[8 + 8 * i:16 + 8 * i]
        start = m[7] | (m[6] & 3) << 8
        length = m[4] | m[5] << 8 | ((m[6] >> 4) & 3) << 16
        nm = data[8 + 8 * i:15 + 8 * i]
        if start < 2 or start + length // SEC > total or not all(32 <= (c & 0x7F) < 127 for c in nm):
            return 0
    big = len(data) > 204800
    base = 6 if total in (400, 800) else 3
    if double != big and ext not in (".ssd", ".dsd"):
        base -= 2
    return base


# ============================================================================ ADFS (old map, 'Hugo' directories)
ADFS_SIZES = {"S": 640, "M": 1280, "L": 2560}
_ATTRS = "RWLDE"


def adfs_checksum(sec: bytes) -> int:
    total = 255
    for i in range(254, -1, -1):
        if total > 255:
            total = (total + 1) & 255
        total += sec[i]
    return total & 255


class AdfsImage(Image):
    def __init__(self, fmt: MediaFormat, data: bytes):
        super().__init__(fmt, data)
        if data[0x201:0x205] != b"Hugo":
            raise MediaError("Not an ADFS (old map) disk")
        fs0, fs1 = data[0:256], data[256:512]
        total = fs0[0xFC] | fs0[0xFD] << 8 | fs0[0xFE] << 16
        nfree = fs1[0xFE] // 3
        free = sum(fs1[3 * i] | fs1[3 * i + 1] << 8 | fs1[3 * i + 2] << 16 for i in range(min(nfree, 82)))
        root = data[0x200:0x700]
        self.label = root[0x4D9:0x4EC].split(b"\x0d")[0].split(b"\x00")[0].decode("latin-1").strip()
        ok = adfs_checksum(fs0) == fs0[0xFF] and adfs_checksum(fs1) == fs1[0xFF]
        self.info = [f"ADFS {'L' if total >= 2560 else 'M' if total >= 1280 else 'S'} ({total * SEC // 1024} KB), "
                     f"{free * SEC // 1024} KB free", f"Boot option {fs1[0xFD]} ({BOOT_OPTS.get(fs1[0xFD] & 3)})",
                     f"Disk ID &{fs1[0xFB] | fs1[0xFC] << 8:04X}" + ("" if ok else ", free space map checksum wrong")]
        self._walk(2, "", set())

    def _walk(self, sector: int, prefix: str, seen: set):
        if sector in seen or len(seen) > 500:
            return
        seen.add(sector)
        d = self.data[sector * SEC:sector * SEC + 0x500]
        if d[1:5] != b"Hugo":
            return
        for k in range(47):
            e = d[5 + 26 * k:5 + 26 * k + 26]
            if len(e) < 26 or e[0] == 0:
                break
            chars = []
            for c in e[:10]:
                c &= 0x7F
                if c in (0x0D, 0x00):
                    break
                chars.append(chr(c))
            name = "".join(chars).strip()
            attrs = "".join(a for i, a in enumerate(_ATTRS) if e[i] & 0x80)
            load = int.from_bytes(e[10:14], "little")
            exe = int.from_bytes(e[14:18], "little")
            length = int.from_bytes(e[18:22], "little")
            start = int.from_bytes(e[22:25], "little")
            meta = {"attrs": attrs, "locked": "L" in attrs, "sector": start}
            path = prefix + name
            if "D" in attrs:
                self._entries.append(Entry(path, 0, "folder", meta, None, is_dir=True))
                self._walk(start, path + "/", seen)
            else:
                data = self.data[start * SEC:start * SEC + length]
                meta.update(load=load, exec=exe)
                self._entries.append(Entry(path, length, _kind(data, load, exe), meta, data))

    def read(self, e: Entry) -> bytes:
        return e.ref or b""


def _adfs_name(n: str) -> str:
    n = re.sub(r'[\s.:*#$&@^%\\"|]', "_", n.strip())[:10]
    if not n:
        raise MediaError("A file needs a name")
    return n


def _adfs_entry(name: str, attrs: str, load: int, exe: int, length: int, start: int) -> bytes:
    raw = bytearray(name.encode("latin-1", "replace")[:10])
    if len(raw) < 10:
        raw += b"\x0d" * (10 - len(raw))
    for i, a in enumerate(_ATTRS):
        if a in attrs:
            raw[i] |= 0x80
    return bytes(raw) + load.to_bytes(4, "little") + exe.to_bytes(4, "little") + length.to_bytes(4, "little") \
        + start.to_bytes(3, "little") + b"\x00"


def create_adfs(files: list[NewFile], label: str, size: str = "L", boot: int = 0) -> bytes:
    total = ADFS_SIZES.get(size.upper())
    if not total:
        raise MediaError(f"ADFS size must be S, M or L (not {size})")
    img = bytearray(total * SEC)
    nxt = [7]

    def alloc(nbytes: int) -> int:
        n = -(-nbytes // SEC)
        if nxt[0] + n > total:
            raise MediaError("Disk full")
        s = nxt[0]
        nxt[0] += n
        return s

    tree: dict = {"files": [], "dirs": {}, "name": "$"}
    for f in files:
        parts = [p for p in re.split(r"[/\\.]", f.name) if p]     # "/" or ADFS's own "." ($.GAMES.ELITE)
        if parts and parts[0] in ("$", ":0", ":4"):
            parts = parts[1:]
        if not parts:
            raise MediaError("A file needs a name")
        node = tree
        for p in parts[:-1]:
            p = _adfs_name(p)
            node = node["dirs"].setdefault(p.upper(), {"files": [], "dirs": {}, "name": p})
        node["files"].append((parts[-1], f))

    def write_dir(node, at: int, parent: int, title: str):
        entries = []
        for leaf, f in node["files"]:
            n = _adfs_name(leaf)
            load, exe = _addresses(f)
            start = alloc(len(f.data)) if f.data else 0
            img[start * SEC:start * SEC + len(f.data)] = f.data
            locked = (f.kind or "").lower() == "locked"
            entries.append((n, "WR" + ("L" if locked else ""), load, exe, len(f.data), start))
        for sub in node["dirs"].values():
            start = alloc(0x500)
            write_dir(sub, start, at, sub["name"])
            entries.append((sub["name"], "DLR", 0, 0, 0x500, start))
        if len(entries) > 47:
            raise MediaError(f"Too many entries in {node['name']} (ADFS holds 47 per folder)")
        lows = [e[0].lower() for e in entries]
        if len(set(lows)) != len(lows):
            raise MediaError(f"Two entries with the same name in {node['name']}")
        d = bytearray(0x500)
        d[0], d[1:5] = 0, b"Hugo"
        for k, e in enumerate(sorted(entries, key=lambda e: e[0].lower())):
            d[5 + 26 * k:5 + 26 * k + 26] = _adfs_entry(*e)
        nm = node["name"].encode("latin-1")[:10]
        d[0x4CC:0x4D6] = (nm + b"\x0d" * 10)[:10]
        d[0x4D6:0x4D9] = parent.to_bytes(3, "little")
        tt = title.encode("latin-1", "replace")[:19]
        d[0x4D9:0x4EC] = (tt + b"\x0d" + b"\x00" * 19)[:19]
        d[0x4FA], d[0x4FB:0x4FF], d[0x4FF] = 0, b"Hugo", 0
        img[at * SEC:at * SEC + 0x500] = d

    write_dir(tree, 2, 2, label or "$")
    fs0, fs1 = bytearray(SEC), bytearray(SEC)
    if nxt[0] < total:
        fs0[0:3] = nxt[0].to_bytes(3, "little")
        fs1[0:3] = (total - nxt[0]).to_bytes(3, "little")
        fs1[0xFE] = 3
    fs0[0xFC:0xFF] = total.to_bytes(3, "little")
    fs1[0xFB:0xFD] = random.getrandbits(16).to_bytes(2, "little")
    fs1[0xFD] = boot & 3
    fs0[0xFF], fs1[0xFF] = adfs_checksum(fs0), adfs_checksum(fs1)
    img[0:SEC], img[SEC:2 * SEC] = fs0, fs1
    return bytes(img)


def _sniff_adfs(data: bytes, ext: str) -> int:
    return 10 if len(data) >= 0x700 and data[0x201:0x205] == b"Hugo" and data[0x6FB:0x6FF] == b"Hugo" else 0


# ============================================================================ helpers / registration
def guess_acorn(host_name: str, data: bytes) -> NewFile:
    """PC file -> Acorn file: picks up a .inf file's addresses later (the UI passes them); BBC BASIC text is
    tokenised so CHAIN works."""
    leaf = host_name.replace("\\", "/").split("/")[-1]
    stem, _, ext = leaf.rpartition(".")
    if not stem:
        stem, ext = leaf, ""
    if ext.lower() in ("bas", "bbc") and data[:1] != b"\x0d":
        try:
            return NewFile(stem[:10], bbc_tokenise(data.decode("latin-1")), "BASIC", BBC_LOAD, BBC_EXEC)
        except ValueError:
            pass
    if bbc_is_basic(data) and data[:1] == b"\x0d":
        return NewFile(stem[:10], data, "BASIC", BBC_LOAD, BBC_EXEC)
    name = stem if ext.lower() in ("inf", "bin", "txt", "") else leaf.replace(".", "_")
    return NewFile(name[:10], data.replace(b"\r\n", b"\r").replace(b"\n", b"\r") if _is_text(data) else data)


def _is_text(data: bytes) -> bool:
    return bool(data) and all(32 <= c < 127 or c in (9, 10, 13) for c in data[:4096])


_BOOT = Option("boot", "Boot option (*OPT 4)", [("0 - nothing", 0), ("1 - *LOAD !BOOT", 1), ("2 - *RUN !BOOT", 2),
                                                 ("3 - *EXEC !BOOT", 3)],
               "What SHIFT+BREAK does; name a file !BOOT to use it")
_TRACKS = Option("tracks", "Tracks", [("80 tracks", 80), ("40 tracks", 40)])
_ACORN = ("bbc", "acorn", "electron", "bbc micro", "bbc master", "master 128", "master compact", "beeb", "atom")

register(MediaFormat(
    "ssd", "Acorn DFS single-sided disk (SSD)", (".ssd",), "disk", _ACORN,
    lambda d: DfsImage(_fmt("ssd"), d, False),
    lambda files, label, tracks=80, boot=0, **o: create_dfs(files, label, False, tracks, boot),
    lambda d, e: _sniff_dfs(d, e, False), kinds=("file", "BASIC"), max_name=9, max_label=12, addresses="load+exec",
    options=(_TRACKS, _BOOT), basic="bbc", guess=guess_acorn, hex_prefix="&",
    notes="Up to 31 files, names like $.GAME (7 characters after the directory letter). BASIC programs get "
          "load &FF1900 / exec &FF8023."))
register(MediaFormat(
    "dsd", "Acorn DFS double-sided disk (DSD)", (".dsd",), "disk", _ACORN,
    lambda d: DfsImage(_fmt("dsd"), d, True),
    lambda files, label, tracks=80, boot=0, **o: create_dfs(files, label, True, tracks, boot),
    lambda d, e: _sniff_dfs(d, e, True), kinds=("file", "BASIC"), max_name=9, max_label=12, addresses="load+exec",
    options=(_TRACKS, _BOOT), basic="bbc", guess=guess_acorn, hex_prefix="&",
    notes="Two DFS sides (drives 0 and 2), 31 files each; files fill side 0 first."))
register(MediaFormat(
    "adl", "Acorn ADFS disk (ADL / ADM / ADS)", (".adl", ".adm", ".ads"), "disk",
    ("bbc master", "master 128", "master compact", "electron", "adfs", "archimedes", "acorn", "bbc"),
    lambda d: AdfsImage(_fmt("adl"), d),
    lambda files, label, size="L", boot=0, **o: create_adfs(files, label, size, boot),
    _sniff_adfs, kinds=("file", "BASIC"), max_name=10, max_label=19, addresses="load+exec",
    options=(Option("size", "Size", [("L - 640 KB (.adl)", "L"), ("M - 320 KB (.adm)", "M"), ("S - 160 KB (.ads)", "S")]),
             _BOOT),
    dirs=True, basic="bbc", guess=guess_acorn, hex_prefix="&",
    notes="ADFS old map: folders (47 entries each), names up to 10 characters."))


def _fmt(fid: str) -> MediaFormat:
    from . import FORMATS
    return FORMATS[fid]
