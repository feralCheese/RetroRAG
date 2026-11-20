"""Guess a category for each file being added, from its type and name, and - for pictures, archives and
unknown binaries - a look inside.

Used by the Add files dialog (one guess per file, shown so it can be changed), by archives whose files are filed
by their own types, by uploads from an AI chat and by the command line. A guess only ever picks the starting
category; the user can always change it.
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Callable

MANUALS = "User Guides & Manuals"
PROGRAMMING = "Programming Reference"
SCHEMATICS = "Schematics"
PINOUTS = "Pinouts"
DATASHEETS = "Chip Datasheets"
SERVICE = "Service & Repair"
BOOKS = "Books & Magazines"
PHOTOS = "Photos"
SOURCE = "Source"
ROMS = "Firmware & ROMs"
DISKS = "Disk & Tape Images"
OTHER = "Other"

# An archive (or folder) whose files are each filed under their own category instead of all under one
BY_TYPE = "By file type"

SNIFF_LIMIT = 8 << 20          # read whole files up to this size to recognise disk images by their contents
PICTURE_LIMIT = 40 << 20       # look at pictures up to this size to tell photos from drawings
ARCHIVE_LOOKS = 40             # in an archive, look inside at most this many pictures / unknown files

# Words in a file name that say what a document is (checked in this order: the first match wins)
NAME_HINTS = [
    (SERVICE, r"service|repair|troubleshoot|diagnos|fault[ _-]?find"),
    (DATASHEETS, r"data[ _-]?sheet"),
    (PINOUTS, r"pin[ _-]?outs?|pin[ _-]?assignment"),
    (SCHEMATICS, r"schem|circuit[ _-]?diagram|wiring[ _-]?diagram|\bsch\b|\bcircuit\b"),
    (PROGRAMMING, r"programm|tech(nical)?[ _-]?ref|hardware[ _-]?ref|reference[ _-]?manual|rom[ _-]?kernel"),
    (BOOKS, r"magazine|newsletter"),
]
# File names cameras and phones give their pictures
CAMERA_NAME = re.compile(r"^(img|dsc|dscn|dscf|pxl|p|imag|photo|pic|gopr|dji|sam)[_-]?\d{3,}", re.I)
PHOTO_WORDS = re.compile(r"photo", re.I)

# Readme-type files that come with a download: they don't say what the download is
INCIDENTAL_EXT = {".txt", ".nfo", ".diz", ".1st", ".me", ".readme", ".htm", ".html", ".url", ".ini", ".cfg"}
INCIDENTAL_NAMES = {"readme", "read.me", "file_id.diz", "license", "copying", "changelog", "authors", "install",
                    "news", "todo", "thanks", "credits"}


def _ext_sets():
    from . import binaries, media
    rom = {e for e, desc in binaries.FORMATS.items()
           if re.search(r"\bROM\b|firmware|fuse map|cartridge|ROM dump", desc)}
    disk = (set(binaries.FORMATS) | media.media_exts()) - rom
    return rom, disk


_SETS: tuple[set, set] | None = None


def rom_exts() -> set[str]:
    global _SETS
    _SETS = _SETS or _ext_sets()
    return _SETS[0]


def disk_exts() -> set[str]:
    global _SETS
    _SETS = _SETS or _ext_sets()
    return _SETS[1]


def name_hint(name: str) -> str | None:
    """A category the file name itself gives away ("A500 schematic.pdf", "6526 datasheet.pdf"), or None."""
    stem = Path(name).stem
    # split camelCase and digits from words so "520steServiceManual" reads as "520ste Service Manual"
    words = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", stem).lower()
    words = re.sub(r"[_.,()\[\]-]+", " ", words)
    for cat, pat in NAME_HINTS:
        if re.search(pat, words):
            return cat
    return None


# ----------------------------------------------------------------------------------------------- pictures
def looks_like_photo(img) -> bool:
    """True for a photo (a camera or phone picture of a board, a machine, a box), False for a scan of a printed
    page or a drawing. Pages are mostly one paper tone; photos are spread over many tones and colours."""
    import numpy as np
    try:
        sub = img.getexif().get_ifd(0x8769)              # Exif: exposure time, f-number, focal length, ISO
        if any(k in sub for k in (0x829A, 0x829D, 0x920A, 0x8827)):
            return True
    except Exception:
        pass
    try:
        img.draft("RGB", (512, 512))                     # JPEG: decode at reduced size (fast)
    except Exception:
        pass
    t = img.convert("RGB")
    t.thumbnail((256, 256))
    L = np.asarray(t.convert("L")).astype(np.int16)
    if L.size < 64:
        return False
    sat = float(np.asarray(t.convert("HSV"))[..., 1].mean() / 255)
    white = float(np.mean(L > 200))
    mid = float(np.mean((L > 60) & (L < 200)))
    near_paper = float(np.mean(np.abs(L - int(np.median(L))) <= 20))
    soft = float(np.mean((lambda dx: (dx > 4) & (dx <= 30))(np.abs(np.diff(L, axis=1)))))
    return white < 0.25 and mid > 0.4 and near_paper < 0.6 and (sat > 0.3 or soft > 0.28)


def _picture_category(name: str, data: Callable[[], bytes | None]) -> str:
    hint = name_hint(name)
    if hint:
        return hint
    stem = Path(name).stem
    if CAMERA_NAME.match(stem) or PHOTO_WORDS.search(re.sub(r"[_.-]+", " ", stem)):
        return PHOTOS
    raw = data()
    if raw:
        try:
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as im:
                return PHOTOS if looks_like_photo(im) else SCHEMATICS
        except Exception:
            pass
    return SCHEMATICS


# ----------------------------------------------------------------------------------------------- one file
def guess_member(name: str, size: int, head: bytes, data: Callable[[], bytes | None], look: bool = True) -> str:
    """Category for one file, given its name, size, first 8 KB and a way to read it all (None when too big)."""
    from .ingest import CODE_EXT, CODE_NAMES, HTML_EXT, IMAGE_EXT, OFFICE_EXT, PDF_EXT, RTF_EXT, TEXT_EXT
    from .textutil import looks_binary
    p = Path(name)
    ext, lname = p.suffix.lower(), p.name.lower()
    if ext in PDF_EXT:
        return name_hint(name) or MANUALS
    if ext in IMAGE_EXT:
        return _picture_category(name, data if look else (lambda: None))
    binary = looks_binary(head)
    if ext in RTF_EXT or ext in OFFICE_EXT or ((ext in HTML_EXT or ext in TEXT_EXT) and not binary):
        return name_hint(name) or MANUALS
    if (ext in CODE_EXT or lname in CODE_NAMES) and not binary:
        return SOURCE
    if ext in rom_exts():
        return ROMS
    if ext in disk_exts():
        return DISKS
    if not binary:
        return name_hint(name) or MANUALS
    if look and size <= SNIFF_LIMIT:
        raw = data()
        if raw:
            from . import media
            try:
                if media.detect(raw, ext):
                    return DISKS
            except Exception:
                pass
    return OTHER


def guess_file(path: Path) -> str:
    """Category for a file or folder about to be added."""
    path = Path(path)
    if path.is_dir():
        return SOURCE
    from . import archives
    if archives.kind(path):
        return guess_archive(path)
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError:
        return OTHER

    def data():
        limit = PICTURE_LIMIT if path.suffix.lower() in _image_exts() else SNIFF_LIMIT
        return path.read_bytes() if size <= limit else None
    return guess_member(path.name, size, head, data)


def _image_exts():
    from .ingest import IMAGE_EXT
    return IMAGE_EXT


def incidental(name: str) -> bool:
    p = Path(name)
    return p.suffix.lower() in INCIDENTAL_EXT or p.name.lower() in INCIDENTAL_NAMES or p.stem.lower() in INCIDENTAL_NAMES


# ----------------------------------------------------------------------------------------------- archives
def archive_members(path: Path) -> list[tuple[str, str]]:
    """(member name, guessed category) for each file in an archive (.zip, .tar.gz...)."""
    from . import archives
    out, looked = [], 0
    with archives.members(path) as ms:
        for m in ms:
            name = m.name
            if Path(name).name.startswith("."):
                continue
            ext = Path(name).suffix.lower()
            needs_look = ext in _image_exts() or ext not in (rom_exts() | disk_exts())
            look = needs_look and looked < ARCHIVE_LOOKS
            looked += int(look)
            limit = PICTURE_LIMIT if ext in _image_exts() else SNIFF_LIMIT
            try:
                with m.open() as f:          # read it now: a tarball is read front to back
                    head = f.read(8192)
                    whole = None
                    if look and m.size <= limit:     # (a .gz / .xz doesn't say its size: stop at the limit)
                        whole = head + f.read(limit + 1 - len(head))
                        whole = whole if len(whole) <= limit else None
            except Exception:
                out.append((name, OTHER))
                continue
            out.append((name, OTHER if archives.kind_by_name(name) else
                        guess_member(name, m.size, head, lambda whole=whole: whole, look)))
    return out


def summarise(cats: list[tuple[str, str]]) -> str:
    """One category for a set of files when they clearly belong together, else BY_TYPE. Readme-type files don't
    count; one odd file in a big set (a logo in a source tree) doesn't either."""
    if not cats:
        return OTHER
    main = [c for n, c in cats if not incidental(n)] or [c for _, c in cats]
    counts: dict[str, int] = {}
    for c in main:
        counts[c] = counts.get(c, 0) + 1
    top, n = max(counts.items(), key=lambda kv: kv[1])
    return top if n == len(main) or (len(main) >= 10 and n >= 0.8 * len(main)) else BY_TYPE


def guess_archive(path: Path) -> str:
    from .archives import READ_ERRORS
    try:
        return summarise(archive_members(path))
    except READ_ERRORS:
        return OTHER


SHORT = {MANUALS: "document", PROGRAMMING: "reference", SCHEMATICS: "drawing", PINOUTS: "pinout",
         DATASHEETS: "datasheet", SERVICE: "service doc", BOOKS: "magazine", PHOTOS: "photo",
         SOURCE: "source file", ROMS: "ROM", DISKS: "disk/tape image", OTHER: "other file"}


def describe_archive(cats: list[tuple[str, str]], label: str = "ZIP") -> str:
    """'ZIP: 1 disk/tape image' / 'TAR: 12 files - 8 source files, 3 photos, 1 other file'."""
    if not cats:
        return f"{label} (empty)"
    counts: dict[str, int] = {}
    for _, c in cats:
        counts[c] = counts.get(c, 0) + 1
    parts = [f"{n} {SHORT.get(c, c)}{'s' if n > 1 else ''}" for c, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    head = f"{label}: {len(cats)} files - " if len(parts) > 1 else f"{label}: "
    return head + ", ".join(parts[:4]) + (", …" if len(parts) > 4 else "")


def inspect_file(path: Path) -> tuple[str, str]:
    """(guessed category, short type description) for one row of the Add files dialog."""
    from . import archives
    path = Path(path)
    k = archives.kind(path)
    if k == archives.ONE:                       # manual.txt.gz: described by what's inside
        try:
            cats = archive_members(path)
            with archives.members(path, k) as ms:
                with next(iter(ms)).open() as f:
                    head = f.read(8192)
        except archives.READ_ERRORS:
            return OTHER, f"{archives.label(k, path.name).capitalize()} file (can't be read)"
        return cats[0][1], f"{describe_file(Path(cats[0][0]), head)}, {archives.label(k, path.name)}-compressed"
    if k:
        try:
            cats = archive_members(path)
        except archives.READ_ERRORS:
            return OTHER, f"{archives.label(k)} (can't be read)"
        return summarise(cats), describe_archive(cats, archives.label(k))
    return guess_file(path), describe_file(path)


def describe_file(path: Path, head: bytes | None = None) -> str:
    """Short type description for the Add files dialog. head: the file's first bytes, when path isn't on disk."""
    from . import binaries
    from .ingest import IMAGE_EXT, OFFICE_EXT
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        return "PDF"
    if ext in IMAGE_EXT:
        return f"Picture ({ext[1:].upper()})"
    if ext in OFFICE_EXT:
        return "Word / Excel document"
    if ext in binaries.FORMATS:
        return binaries.FORMATS[ext]
    from . import ingest
    kind = ingest.classify(path, head)
    return {"text": "Text", "code": "Source code", "binary": "Binary file"}.get(kind, kind)
