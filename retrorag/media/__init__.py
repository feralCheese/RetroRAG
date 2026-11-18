"""Disk and tape images: read the directory, pull files out, and build new images from files.

Every format is a MediaFormat in FORMATS. Typical use:

    img = media.open_image(path)               # works out the format from the contents (and extension)
    for e in img.entries(): print(e.name, e.kind, e.size)
    data = img.read(e)                         # the file's bytes, as the machine saved them
    raw = media.create("d64", [media.NewFile("HELLO", prg)], label="MY DISK")

Pure Python, no emulator or external tool needed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


class MediaError(ValueError):
    """Bad or unsupported image, or a file that won't fit."""


@dataclass
class Entry:
    name: str                      # name on the image; folders joined with "/"
    size: int                      # bytes of file data
    kind: str = ""                 # format's own type: PRG, SEQ, Program, Bytes, file ...
    meta: dict = field(default_factory=dict)   # load / exec / start / autostart / locked / blocks ...
    ref: Any = None                # format-private handle used by Image.read
    is_dir: bool = False

    def describe(self, prefix: str = "$", kind: bool = True) -> str:
        bits = [self.kind] if self.kind and kind else []
        for k in ("load", "exec", "start"):
            if self.meta.get(k) is not None:
                bits.append(f"{k} {fmt_addr(self.meta[k], prefix)}")
        if self.meta.get("autostart") is not None:
            bits.append(f"autostart line {self.meta['autostart']}")
        if self.meta.get("locked"):
            bits.append("locked")
        return ", ".join(bits)


@dataclass
class NewFile:
    name: str
    data: bytes
    kind: str = ""                 # "" = work it out (e.g. PRG / Program / Bytes / BASIC text)
    load: int | None = None        # load / start address where the format stores one
    exec: int | None = None        # execution address (Acorn) or autostart line (Spectrum BASIC)


def fmt_addr(v: int, prefix: str = "$") -> str:
    """$C000 (Commodore / Spectrum style) or &FF1900 (Acorn style)."""
    return f"{prefix}{v:06X}" if v > 0xFFFF else f"{prefix}{v:04X}"


class Image:
    """An opened image. Subclasses fill self._entries and implement read()."""
    fmt: "MediaFormat"

    def __init__(self, fmt: "MediaFormat", data: bytes):
        self.fmt, self.data = fmt, data
        self.label = ""
        self.info: list[str] = []           # header facts: free space, boot option, file system ...
        self._entries: list[Entry] = []

    def entries(self) -> list[Entry]:
        return self._entries

    def files(self) -> list[Entry]:
        return [e for e in self._entries if not e.is_dir]

    def find(self, name: str) -> Entry:
        want = name.strip().strip("/")
        for e in self._entries:
            if e.name == want:
                return e
        low = want.lower()
        for e in self._entries:
            if e.name.lower() == low:
                return e
        for e in self._entries:                 # DFS "$.NAME" vs "NAME", Spectrum names with spaces ...
            if e.name.lower().split(".", 1)[-1].strip() == low or e.name.lower().strip() == low.strip():
                return e
        raise MediaError(f"No file called {name!r} on this {self.fmt.medium}")

    def read(self, e: Entry) -> bytes:
        raise NotImplementedError


@dataclass
class Option:
    key: str
    label: str
    choices: list[tuple[str, Any]]       # (label, value); first is the default
    help: str = ""


@dataclass
class MediaFormat:
    id: str
    name: str
    exts: tuple[str, ...]
    medium: str                          # "disk" or "tape"
    systems: tuple[str, ...]             # words in a system's name that suggest this format
    opener: Callable[[bytes], Image]
    creator: Callable[..., bytes] | None
    sniff: Callable[[bytes, str], int]   # 0 = not this format; higher = more certain
    kinds: tuple[str, ...] = ()          # file types a user can pick when creating
    max_name: int = 16
    max_label: int = 16
    addresses: str = ""                  # "load+exec" / "load" / "start+autostart" / "" : what the user may set
    options: tuple[Option, ...] = ()
    notes: str = ""
    dirs: bool = False                   # can hold folders
    basic: str = ""                      # BASIC dialect for text programs: cbm / zx / bbc
    guess: Callable[[str, bytes], NewFile] | None = None    # PC file -> name / type / addresses on the image
    hex_prefix: str = "$"                # how the machine writes hex: $C000 or &1900

    @property
    def ext(self) -> str:
        return self.exts[0]

    @property
    def writable(self) -> bool:
        return self.creator is not None

    def open(self, data: bytes) -> Image:
        return self.opener(data)

    def create(self, files: list[NewFile], label: str = "", **opts) -> bytes:
        if not self.creator:
            raise MediaError(f"{self.name}: creating images isn't supported")
        for o in self.options:
            opts.setdefault(o.key, o.choices[0][1])
        return self.creator(files, label, **opts)

    def describe(self) -> dict:
        return {"id": self.id, "name": self.name, "extensions": list(self.exts), "medium": self.medium,
                "can_create": self.writable, "file_types": list(self.kinds), "max_name_length": self.max_name,
                "addresses": self.addresses, "folders": self.dirs, "basic_dialect": self.basic or None,
                "options": {o.key: {"label": o.label, "choices": [v for _, v in o.choices], "default": o.choices[0][1],
                                    "help": o.help} for o in self.options},
                "notes": self.notes}


FORMATS: dict[str, MediaFormat] = {}


def register(f: MediaFormat) -> MediaFormat:
    FORMATS[f.id] = f
    return f


def get(fmt_id: str) -> MediaFormat:
    _load()
    key = fmt_id.lower().lstrip(".")
    if key in FORMATS:
        return FORMATS[key]
    for f in FORMATS.values():
        if "." + key in f.exts:
            return f
    raise MediaError(f"Unknown disk/tape format {fmt_id!r}. Known: {', '.join(FORMATS)}")


def all_formats() -> list[MediaFormat]:
    _load()
    return list(FORMATS.values())


_loaded = False


def _load():
    global _loaded
    if not _loaded:
        _loaded = True
        from . import acorn, amiga, cbm, fat, spectrum  # noqa: F401  (each registers its formats)


def media_exts() -> set[str]:
    return {e for f in all_formats() for e in f.exts}


def detect(data: bytes, ext: str = "") -> MediaFormat | None:
    ext = (ext or "").lower()
    best, score = None, 0
    for f in all_formats():
        try:
            s = f.sniff(data, ext)
        except Exception:
            s = 0
        if s and ext in f.exts:
            s += 5
        if s > score:
            best, score = f, s
    return best


def open_image(path_or_data, ext: str = "", fmt: str | None = None) -> Image:
    if isinstance(path_or_data, (str, Path)):
        p = Path(path_or_data)
        data, ext = p.read_bytes(), ext or p.suffix
    else:
        data = bytes(path_or_data)
    f = get(fmt) if fmt else detect(data, ext)
    if f is None:
        raise MediaError("Not a disk or tape image RetroRAG can read")
    return f.open(data)


def create(fmt_id: str, files: list[NewFile], label: str = "", **opts) -> bytes:
    return get(fmt_id).create(files, label, **opts)


# ---------------------------------------------------------------- systems -> formats
def suggest_for_system(*names: str) -> list[str]:
    """Formats a system probably uses, from its name / manufacturer (e.g. 'Commodore 64' -> d64, d81)."""
    text = " " + " ".join(n for n in names if n).lower() + " "
    text = re.sub(r"[^a-z0-9+/]+", " ", text)
    out = []
    for f in all_formats():
        for w in f.systems:
            if (" " + w + " ") in text or (len(w) > 3 and w in text):
                out.append(f.id)
                break
    return out


# ---------------------------------------------------------------- reading files for people / AI
def text_view(img: Image, e: Entry, limit: int = 200_000) -> tuple[str, str]:
    """-> (how it was shown, text): a BASIC listing, plain text, or a hex dump."""
    from . import basic
    data = img.read(e)
    hint = img.fmt.basic
    listing = None
    if hint == "zx" and e.kind == "Program":
        listing = basic.zx_list(data)
        if listing:
            return "Sinclair BASIC listing", listing
    if hint == "cbm" or (hint == "" and len(data) > 2 and data[0] == 1):
        listing = basic.cbm_list(data)
        if listing:
            return "Commodore BASIC listing", listing
    if hint == "bbc" or (data[:1] == b"\x0d" and data[-2:] == b"\x0d\xff"):
        listing = basic.bbc_list(data)
        if listing:
            return "BBC BASIC listing", listing
    if is_text(data):
        return "text", decode_text(data, img.fmt)[:limit]
    return "hex dump", hex_dump(data, base=e.meta.get("load") or e.meta.get("start") or 0, limit=limit)


def is_text(data: bytes) -> bool:
    if not data:
        return True
    sample = data[:4096]
    if b"\x00" in sample.rstrip(b"\x00"):
        return False
    good = sum(1 for c in sample if 32 <= c < 127 or c in (9, 10, 13) or c >= 0xC1 and c <= 0xDA)
    return good / len(sample) > 0.9


def decode_text(data: bytes, fmt: "MediaFormat | None" = None) -> str:
    data = data.rstrip(b"\x00\x1a")
    if fmt is not None and fmt.basic == "cbm":            # PETSCII: swap cases, CR line ends
        out = []
        for c in data:
            if c == 0x0D:
                out.append("\n")
            elif 0x41 <= c <= 0x5A:
                out.append(chr(c + 32))
            elif 0xC1 <= c <= 0xDA:
                out.append(chr(c - 0x80))
            elif 0x20 <= c < 0x7F:
                out.append(chr(c))
        return "".join(out)
    text = data.decode("latin-1")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def hex_dump(data: bytes, base: int = 0, limit: int = 200_000) -> str:
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hx = " ".join(f"{b:02X}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{base + off:06X}  {hx:<47}  {asc}")
        if len(lines) * 76 > limit:
            lines.append(f"... ({len(data) - off - 16} more bytes)")
            break
    return "\n".join(lines)


def medium_word(f: MediaFormat) -> str:
    return "Cartridge" if f.id == "mdr" else "Disk" if f.medium == "disk" else "Tape"


def listing_markdown(img: Image, max_programs: int = 40, max_chars: int = 60_000) -> str:
    """Markdown for the library index: the directory plus BASIC listings and short text files."""
    f = img.fmt
    lines = [f"## {medium_word(f)} contents ({f.name})", ""]
    if img.label:
        lines.append(f"- **{medium_word(f)} name:** {img.label}")
    lines += [f"- {i}" for i in img.info]
    files = img.files()
    lines += ["", "| Name | Type | Size | Details |", "|---|---|---|---|"]
    for e in img.entries():
        name = e.name.replace("|", "\\|") + ("/" if e.is_dir else "")
        lines.append(f"| {name} | {'folder' if e.is_dir else e.kind} | {'' if e.is_dir else e.size} | "
                     f"{'' if e.is_dir else e.describe(f.hex_prefix, kind=False).replace('|', '/')} |")
    if not img.entries():
        lines.append("| (empty) | | | |")
    used, shown = sum(len(x) for x in lines), 0
    for e in files:
        if shown >= max_programs or used > max_chars:
            break
        try:
            how, text = text_view(img, e, limit=max_chars - used)
        except Exception:
            continue
        if how == "hex dump" or not text.strip():
            continue
        block = text if len(text) < 12_000 else text[:12_000] + "\n... (cut)"
        lines += ["", f"### {e.name} ({how})", "", "```", block.rstrip(), "```"]
        used += len(block)
        shown += 1
    return "\n".join(lines) + "\n"


def extract_all(img: Image, dest: Path, entries: list[Entry] | None = None, inf: bool | None = None) -> list[Path]:
    """Write files out to a folder. Acorn formats also get .inf files (load / exec addresses), the usual way of
    keeping them."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    inf = img.fmt.id in ("ssd", "dsd", "adl") if inf is None else inf
    out = []
    for e in entries or img.files():
        if e.is_dir:
            continue
        rel = safe_host_name(e.name)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(img.read(e))
        out.append(target)
        if inf:
            load, exe = e.meta.get("load") or 0, e.meta.get("exec") or 0
            (target.parent / (target.name + ".inf")).write_text(
                f"{e.name.split('/')[-1]} {load:06X} {exe:06X} {e.size:06X}" + (" L" if e.meta.get("locked") else "")
                + "\n", "ascii", errors="replace")
    return out


_BAD = re.compile(r'[<>:"\\|?*\x00-\x1f]')


def safe_host_name(name: str) -> str:
    parts = []
    for part in name.split("/"):
        p = _BAD.sub("_", part).strip().rstrip(".") or "_"
        if p.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?i)(COM|LPT)\d", p):
            p += "_"
        parts.append(p)
    return "/".join(parts)


def read_inf(path: Path) -> tuple[int | None, int | None]:
    """Load / exec addresses from an Acorn-style .inf file next to path (if there is one)."""
    inf = Path(str(path) + ".inf")
    if not inf.exists():
        return None, None
    try:
        bits = inf.read_text("latin-1").split()
        return int(bits[1], 16), int(bits[2], 16)
    except Exception:
        return None, None


def prepare(fmt: MediaFormat, host_name: str, data: bytes) -> NewFile:
    """A file from the PC -> a NewFile with a sensible name, type and addresses for this format."""
    if fmt.guess:
        return fmt.guess(host_name, data)
    return NewFile(Path(host_name).name, data)


def program_from_text(fmt: MediaFormat, text: str, start: int | None = None) -> tuple[bytes, str]:
    """BASIC source -> a tokenised program for this format's machine. -> (bytes, kind)."""
    from . import basic
    if fmt.basic == "cbm":
        return basic.cbm_tokenise(text, load=start or 0x0801, v2_only=(start or 0x0801) in (0x0801, 0x0401, 0x1201)), "PRG"
    if fmt.basic == "zx":
        return basic.zx_tokenise(text), "Program"
    if fmt.basic == "bbc":
        return basic.bbc_tokenise(text), "BASIC"
    raise MediaError(f"{fmt.name}: no BASIC tokeniser for this machine - add the program as a text file instead")
