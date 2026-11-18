"""Binary files (ROMs, firmware, disk & tape images): hashes, format sniffing, and a text 'info card'.

The info card is markdown that gets indexed, so an AI can answer "which Kickstart ROM do I have?",
"what's on this D64?" or "what is the SHA1 of the 1541 ROM?" even though the file itself is binary.
"""
from __future__ import annotations

import hashlib
import re
import struct
import zlib
from pathlib import Path

FORMATS = {
    # disk images
    ".d64": "Commodore 1541 disk image", ".d71": "Commodore 1571 disk image", ".d81": "Commodore 1581 disk image",
    ".g64": "Commodore GCR disk image", ".t64": "Commodore 64 tape container", ".tap": "Tape image (C64 or ZX Spectrum)",
    ".prg": "Commodore program file", ".crt": "C64 cartridge image", ".p00": "PC64 program file",
    ".adf": "Amiga disk file", ".adz": "Amiga disk file (gzip)", ".dms": "Amiga DMS disk archive", ".ipf": "SPS/CAPS preservation disk image",
    ".hdf": "Hard disk file image", ".st": "Atari ST disk image", ".msa": "Atari ST MSA disk image", ".stx": "Atari ST Pasti image",
    ".atr": "Atari 8-bit disk image", ".xex": "Atari 8-bit executable", ".cas": "Cassette image (Atari/MSX/TRS-80)",
    ".dsk": "Disk image (Amstrad CPC / Apple II / MSX / TRS-80)", ".do": "Apple II DOS-order disk image", ".po": "Apple II ProDOS-order disk image",
    ".nib": "Apple II nibble image", ".woz": "Apple II WOZ flux image", ".2mg": "Apple II 2IMG image",
    ".tzx": "ZX Spectrum TZX tape image", ".z80": "ZX Spectrum snapshot", ".sna": "ZX Spectrum/CPC snapshot", ".trd": "TR-DOS disk image",
    ".scl": "TR-DOS archive", ".mgt": "MGT +D/DISCiPLE disk image", ".cdt": "Amstrad CPC tape image",
    ".uef": "Acorn tape/state image", ".ssd": "BBC Micro single-sided disk image", ".dsd": "BBC Micro double-sided disk image",
    ".adl": "Acorn ADFS disk image", ".img": "Raw disk image", ".ima": "Raw floppy image", ".imd": "ImageDisk floppy image",
    ".td0": "Teledisk floppy image", ".hfe": "HxC floppy emulator image", ".scp": "SuperCard Pro flux image",
    ".kryoflux": "KryoFlux stream", ".mfm": "MFM disk image", ".vhd": "Virtual hard disk",
    ".mdr": "ZX Microdrive cartridge image", ".ads": "Acorn ADFS S (160K) disk image", ".adm": "Acorn ADFS M (320K) disk image",
    ".vfd": "Virtual floppy disk image", ".flp": "Floppy disk image",
    ".iso": "CD/DVD image", ".cue": "CD cue sheet", ".bin": "Raw binary (ROM/firmware/CD track)", ".chd": "MAME compressed hunks of data",
    # ROMs
    ".rom": "ROM image", ".nes": "NES iNES ROM", ".fds": "Famicom Disk System image", ".sfc": "Super Nintendo ROM", ".smc": "Super Nintendo ROM (copier header)",
    ".gb": "Game Boy ROM", ".gbc": "Game Boy Color ROM", ".gba": "Game Boy Advance ROM", ".n64": "Nintendo 64 ROM", ".z64": "Nintendo 64 ROM",
    ".v64": "Nintendo 64 ROM (byte-swapped)", ".md": "Sega Mega Drive / Genesis ROM", ".gen": "Sega Mega Drive / Genesis ROM",
    ".smd": "Sega Mega Drive ROM (interleaved)", ".sms": "Sega Master System ROM", ".gg": "Sega Game Gear ROM", ".32x": "Sega 32X ROM",
    ".a26": "Atari 2600 ROM", ".a52": "Atari 5200 ROM", ".a78": "Atari 7800 ROM", ".lnx": "Atari Lynx ROM", ".j64": "Atari Jaguar ROM",
    ".col": "ColecoVision ROM", ".int": "Intellivision ROM", ".pce": "PC Engine ROM", ".ngp": "Neo Geo Pocket ROM", ".ws": "WonderSwan ROM",
    ".vec": "Vectrex ROM", ".hex": "Intel HEX firmware", ".srec": "Motorola S-record firmware", ".s19": "Motorola S-record firmware",
    ".jed": "JEDEC PLD fuse map", ".u1": "ROM dump", ".mx": "MSX ROM", ".mx1": "MSX ROM", ".mx2": "MSX ROM",
}


def file_hashes(p: Path) -> dict[str, str]:
    sha1, md5, crc = hashlib.sha1(), hashlib.md5(), 0
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            sha1.update(chunk); md5.update(chunk); crc = zlib.crc32(chunk, crc)
    return {"sha1": sha1.hexdigest(), "md5": md5.hexdigest(), "crc32": f"{crc & 0xFFFFFFFF:08x}"}


# ---------------------------------------------------------------- PETSCII / Commodore
def petscii(b: bytes) -> str:
    out = []
    for c in b:
        if c == 0xA0:
            break
        if 0x41 <= c <= 0x5A or 0x20 <= c <= 0x40 or c in (0x5B, 0x5D):
            out.append(chr(c))
        elif 0xC1 <= c <= 0xDA:
            out.append(chr(c - 0x80))
        else:
            out.append("?")
    return "".join(out)


def _d64_offset(track: int, sector: int) -> int:
    off = 0
    for t in range(1, track):
        off += 21 if t <= 17 else 19 if t <= 24 else 18 if t <= 30 else 17
    return (off + sector) * 256


def d64_listing(data: bytes) -> list[str]:
    if len(data) not in (174848, 175531, 196608, 197376):
        return []
    bam = _d64_offset(18, 0)
    name = petscii(data[bam + 0x90: bam + 0xA0])
    disk_id = petscii(data[bam + 0xA2: bam + 0xA7])
    lines = [f'0 "{name:<16}" {disk_id}']
    types = ["DEL", "SEQ", "PRG", "USR", "REL"]
    t, s, seen, free = 18, 1, set(), 0
    for tr in range(1, 36):
        if tr != 18:
            free += data[bam + 4 * tr]
    while t and (t, s) not in seen and len(seen) < 40:
        seen.add((t, s))
        off = _d64_offset(t, s)
        sec = data[off: off + 256]
        if len(sec) < 256:
            break
        for e in range(8):
            ent = sec[e * 32: e * 32 + 32]
            ftype = ent[2]
            if ftype == 0:
                continue
            fname = petscii(ent[5:21])
            blocks = struct.unpack("<H", ent[30:32])[0]
            tname = types[ftype & 7] if (ftype & 7) < len(types) else "???"
            lines.append(f'{blocks:<5}"{fname}"{" " * max(1, 17 - len(fname))}{tname}{"" if ftype & 0x80 else "*"}{"<" if ftype & 0x40 else ""}')
        t, s = sec[0], sec[1]
    lines.append(f"{free} BLOCKS FREE.")
    return lines


def zx_tap_listing(data: bytes) -> list[str]:
    out, pos = [], 0
    kinds = {0: "Program", 1: "Number array", 2: "Character array", 3: "Bytes"}
    while pos + 2 <= len(data) and len(out) < 200:
        ln = struct.unpack("<H", data[pos:pos + 2])[0]
        blk = data[pos + 2: pos + 2 + ln]
        if ln == 0 or len(blk) < ln:
            break
        if ln == 19 and blk[0] == 0x00:
            name = blk[2:12].decode("latin-1").rstrip()
            dlen, p1 = struct.unpack("<HH", blk[12:16])
            extra = f" autostart line {p1}" if blk[1] == 0 and p1 < 32768 else f" load address {p1}" if blk[1] == 3 else ""
            out.append(f'{kinds.get(blk[1], "?")}: "{name}" ({dlen} bytes){extra}')
        pos += 2 + ln
    return out if out and pos == len(data) else []


def tzx_info(data: bytes) -> list[str]:
    if not data.startswith(b"ZXTape!\x1a"):
        return []
    out = [f"TZX version {data[8]}.{data[9]}"]
    # Pull out standard-speed header blocks (ID 0x10) for file names
    pos = 10
    while pos < len(data) and len(out) < 200:
        bid = data[pos]
        if bid == 0x10 and pos + 5 <= len(data):
            ln = struct.unpack("<H", data[pos + 3:pos + 5])[0]
            blk = data[pos + 5: pos + 5 + ln]
            if ln == 19 and blk[:1] == b"\x00":
                out.append(f'Header: "{blk[2:12].decode("latin-1").rstrip()}"')
            pos += 5 + ln
        elif bid == 0x30 and pos + 2 <= len(data):   # text description
            ln = data[pos + 1]
            out.append("Text: " + data[pos + 2: pos + 2 + ln].decode("latin-1", "replace"))
            pos += 2 + ln
        else:
            break   # other block types: stop rather than mis-parse
    return out


def adf_info(data: bytes) -> list[str]:
    if len(data) not in (901120, 1802240) or not data.startswith(b"DOS"):
        return []
    root = (len(data) // 512 // 2) * 512
    nlen = data[root + 432]
    name = data[root + 433: root + 433 + min(nlen, 30)].decode("latin-1", "replace")
    fs = {0: "OFS", 1: "FFS", 2: "OFS-INTL", 3: "FFS-INTL", 4: "OFS-DC", 5: "FFS-DC"}.get(data[3], f"DOS{data[3]}")
    return [f"AmigaDOS {fs} volume: {name}", "Bootable" if any(data[12:1024]) else "Not bootable (empty bootblock)"]


def rom_header_info(data: bytes, ext: str) -> list[str]:
    out = []
    if data.startswith(b"NES\x1a") and len(data) >= 16:
        mapper = (data[6] >> 4) | (data[7] & 0xF0)
        out += [f"iNES header: PRG ROM {data[4] * 16} KB, CHR ROM {data[5] * 8} KB, mapper {mapper}",
                "Mirroring: " + ("vertical" if data[6] & 1 else "horizontal") + (", battery" if data[6] & 2 else "")]
    if ext in (".gb", ".gbc") and len(data) > 0x150:
        title = data[0x134:0x143].split(b"\x00")[0].decode("ascii", "replace")
        out.append(f"Game Boy title: {title}  (CGB flag 0x{data[0x143]:02x}, cartridge type 0x{data[0x147]:02x})")
    if len(data) > 0x200 and data[0x100:0x104] == b"SEGA":
        out.append("Sega header: " + data[0x100:0x110].decode("latin-1").strip())
        out.append("Domestic name: " + data[0x120:0x150].decode("latin-1").strip())
        out.append("Overseas name: " + data[0x150:0x180].decode("latin-1").strip())
    return out


def printable_strings(data: bytes, min_len: int = 6, limit: int = 400) -> list[str]:
    out, seen = [], set()
    for m in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, data[: 8 << 20]):
        s = m.group().decode("ascii").strip()
        if len(s) >= min_len and s not in seen and len(set(s)) > 2:
            seen.add(s); out.append(s)
            if len(out) >= limit:
                break
    return out


def info_card(p: Path, title: str, system_name: str, category: str, hashes: dict[str, str],
              notes: str = "", strings_min: int = 6) -> str:
    data = p.read_bytes() if p.stat().st_size <= 64 << 20 else p.open("rb").read(8 << 20)
    ext = p.suffix.lower()
    size = p.stat().st_size
    lines = [f"# {title}", "",
             f"- **System:** {system_name}",
             f"- **Category:** {category}",
             f"- **File:** {p.name}",
             f"- **Format:** {FORMATS.get(ext, 'Binary file')}",
             f"- **Size:** {size:,} bytes ({size / 1024:.1f} KB, 0x{size:X})",
             f"- **CRC32:** {hashes.get('crc32', '')}",
             f"- **MD5:** {hashes.get('md5', '')}",
             f"- **SHA1:** {hashes.get('sha1', '')}"]
    if notes.strip():
        lines += ["", "## Notes", "", notes.strip()]
    sections: list[tuple[str, list[str]]] = []
    media_md = ""
    try:
        from . import media
        fmt = media.detect(data, ext)
        if fmt is not None:
            img = fmt.open(data)
            lines[5] = f"- **Format:** {fmt.name}"
            media_md = media.listing_markdown(img)
    except Exception as e:                    # a damaged image still gets the rest of its card
        media_md = f"## Disk / tape contents\n\nCould not read the image: {e}\n"
    if not media_md:
        if ext in (".d64",):
            sections.append(("Directory", d64_listing(data)))
        if ext == ".tap":
            sections.append(("Tape contents", zx_tap_listing(data)))
        if ext in (".tzx", ".cdt"):
            sections.append(("Tape contents", tzx_info(data)))
        if ext == ".adf":
            sections.append(("Disk", adf_info(data)))
    sections.append(("Header", rom_header_info(data, ext)))
    is_image = ext in {".d64", ".d71", ".d81", ".g64", ".adf", ".ipf", ".st", ".msa", ".atr", ".dsk", ".scp", ".hfe", ".woz", ".iso", ".chd"}
    if not is_image and not media_md:
        sections.append(("Embedded text strings", printable_strings(data, strings_min)))
    for head, body in sections:
        if body:
            fence = head in ("Directory", "Embedded text strings")
            lines += ["", f"## {head}", ""]
            lines += (["```"] + body + ["```"]) if fence else [f"- {b}" for b in body]
    if media_md:
        lines += ["", media_md.rstrip()]
    return "\n".join(lines) + "\n"
