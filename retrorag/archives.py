"""Archives RetroRAG unpacks when files are added or uploaded: .zip, and tarballs (.tar, .tar.gz / .tgz,
.tar.bz2 / .tbz2, .tar.xz / .txz) - the usual way emulator and system source code is released - and single
compressed files (.gz, .bz2, .xz: manual.txt.gz, game.d64.gz), the usual way old FTP sites like FUNET stored things.

Only ordinary files are taken out. Folders are recreated from the files' paths; links, devices and anything whose
path would land outside the target folder (absolute paths, '..') are skipped.
"""
from __future__ import annotations

import bz2
import gzip
import io
import lzma
import tarfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator

ZIP, TAR, ONE = "zip", "tar", "one"         # ONE: a single compressed file (manual.txt.gz), not a tarball
TAR_SUFFIXES = (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tbz", ".tar.xz", ".txz", ".tar")
COMPRESSED = {".gz": ("gzip", b"\x1f\x8b", gzip.open), ".bz2": ("bzip2", b"BZh", bz2.open),
              ".xz": ("xz", b"\xfd7zXZ\x00", lzma.open)}
READ_ERRORS = (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError, ValueError, lzma.LZMAError)


def _compressed_suffix(name: str) -> str | None:
    n = name.lower()
    return next((s for s in COMPRESSED if n.endswith(s) and len(n) > len(s)), None)


def _compression(head: bytes) -> str | None:
    """'.gz' / '.bz2' / '.xz' from a file's first bytes, else None."""
    return next((s for s, (_, magic, _o) in COMPRESSED.items() if head.startswith(magic)), None)


def kind_by_name(name: str) -> str | None:
    n = name.lower()
    if n.endswith(".zip"):
        return ZIP
    if n.endswith(TAR_SUFFIXES):
        return TAR
    if _compressed_suffix(n):
        return ONE
    return None


def kind(path: Path) -> str | None:
    """'zip' / 'tar' / 'one' for an archive or compressed file RetroRAG can unpack (checked by name and
    contents), else None."""
    path = Path(path)
    k = kind_by_name(path.name)
    try:
        if k == ZIP and zipfile.is_zipfile(path):
            return ZIP
        if k == TAR and path.is_file() and tarfile.is_tarfile(path):
            return TAR
        if k == ONE and path.is_file():
            with open(path, "rb") as f:
                return ONE if _compression(f.read(8)) else None
    except READ_ERRORS:
        pass
    return None


def kind_of_bytes(name: str, data: bytes) -> str | None:
    k = kind_by_name(name)
    try:
        if k == ZIP and zipfile.is_zipfile(io.BytesIO(data)):
            return ZIP
        if k == TAR and tarfile.is_tarfile(io.BytesIO(data)):
            return TAR
        if k == ONE and _compression(data[:8]):
            return ONE
    except READ_ERRORS:
        pass
    return None


def stem(name: str) -> str:
    """'vice-3.9.tar.gz' -> 'vice-3.9', 'disks.zip' -> 'disks', 'manual.txt.gz' -> 'manual.txt'."""
    n = name.lower()
    for suf in (".zip",) + TAR_SUFFIXES + tuple(COMPRESSED):
        if n.endswith(suf) and len(name) > len(suf):
            return name[: -len(suf)]
    return name


def label(k: str | None, name: str = "") -> str:
    """'ZIP' / 'TAR' / 'gzip' (for a compressed file, named by its compression)."""
    if k == ONE:
        suf = _compressed_suffix(name)
        return COMPRESSED[suf][0] if suf else "compressed"
    return {ZIP: "ZIP", TAR: "TAR"}.get(k or "", "Archive")


def decompress(data: bytes, max_bytes: int | None = None) -> bytes:
    """A compressed file's bytes (.gz / .bz2 / .xz) -> what's inside. ValueError if it holds more than max_bytes."""
    suf = _compression(data[:8])
    if not suf:
        raise ValueError("not a .gz, .bz2 or .xz file")
    with COMPRESSED[suf][2](io.BytesIO(data)) as f:
        out = f.read(-1 if max_bytes is None else max_bytes + 1)
    if max_bytes is not None and len(out) > max_bytes:
        raise ValueError(f"it unpacks to more than {max_bytes >> 20} MB")
    return out


@dataclass
class Member:
    name: str                       # path inside the archive, '/'-separated
    size: int
    open: Callable[[], object]      # -> a readable binary file


def _safe_parts(name: str) -> tuple[str, ...] | None:
    """The path's parts, or None if it would leave the target folder (absolute, drive letter, '..')."""
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or any(part in ("..", "") or ":" in part for part in p.parts):
        return None
    return p.parts or None


def _one(src: Path | bytes, fileobj: io.BytesIO | None, name: str | None) -> Member:
    """A compressed file's one member: what's inside, named like it without the .gz / .bz2 / .xz."""
    if fileobj is not None:
        head, tail = bytes(src[:8]), bytes(src[-4:])
    else:
        with open(src, "rb") as f:
            head = f.read(8)
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4))
            tail = f.read(4)
    suf = _compression(head)
    if not suf:
        raise ValueError("not a .gz, .bz2 or .xz file")
    name = name or (Path(src).name if fileobj is None else "file" + suf)
    inner = stem(name) if _compressed_suffix(name) else name
    size = int.from_bytes(tail, "little") if suf == ".gz" and len(tail) == 4 else 0   # gzip keeps it (mod 4 GB)

    def opener():
        if fileobj is not None:
            fileobj.seek(0)
        return COMPRESSED[suf][2](fileobj if fileobj is not None else src)
    return Member(inner, size, opener)


@contextmanager
def members(src: Path | bytes, k: str | None = None, name: str | None = None) -> Iterator[Iterator[Member]]:
    """The ordinary files in an archive (a path, or its bytes), in archive order:
        with members(path) as ms:
            for m in ms: ... m.open().read() ...
    A tarball is read front to back, so open each member while it's the current one. A compressed file (.gz,
    .bz2, .xz) has one member; name: the file's name, when src is its bytes."""
    fileobj = io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else None
    if k is None:
        k = (kind_of_bytes("x.zip", src) or kind_of_bytes("x.tar", src) or
             (ONE if _compression(bytes(src[:8])) else None)) if fileobj else kind(src)
    if k == ONE:
        yield iter([_one(src, fileobj, name)])
    elif k == ZIP:
        with zipfile.ZipFile(fileobj or src) as z:
            yield (Member(i.filename, i.file_size, lambda i=i: z.open(i))
                   for i in z.infolist() if not i.is_dir())
    elif k == TAR:
        with tarfile.open(fileobj=fileobj) if fileobj else tarfile.open(src) as t:
            yield (Member(m.name, m.size, lambda m=m: t.extractfile(m)) for m in t if m.isfile())
    else:
        raise ValueError(f"not an archive RetroRAG can unpack: {getattr(src, 'name', 'data')}")


def extract(src: Path | bytes, out_dir: Path, log: Callable[[str], None] = print, *, k: str | None = None,
            max_files: int | None = None, max_bytes: int | None = None, skip: Callable[[str], bool] | None = None,
            strip_top: str | None = None, name: str | None = None) -> list[Path]:
    """Unpack the ordinary files into out_dir (made if needed) -> the files written. max_files / max_bytes raise
    ValueError when the archive holds more than that. strip_top: a top folder of this name is left out (out_dir
    is already named after the archive, and vice-3.9.tar.gz holds vice-3.9/...: don't nest it twice)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    root = out_dir.resolve()
    written: list[Path] = []
    total = unsafe = 0
    with members(src, k, name) as ms:
        for m in ms:
            if skip and skip(m.name):
                continue
            parts = _safe_parts(m.name)
            if parts and strip_top and len(parts) > 1 and parts[0].lower() == strip_top.lower():
                parts = parts[1:]
            target = (out_dir.joinpath(*parts)).resolve() if parts else None
            if target is None or root not in target.parents:
                unsafe += 1
                continue
            if max_files is not None and len(written) >= max_files:
                raise ValueError(f"the archive has more than {max_files} files")
            too_big = ValueError(f"the archive unpacks to more than {(max_bytes or 0) >> 20} MB")
            if max_bytes is not None and total + m.size > max_bytes:
                raise too_big
            target.parent.mkdir(parents=True, exist_ok=True)
            with m.open() as fsrc, open(target, "wb") as fdst:
                # count what really comes out: a header's size can lie, and a .gz / .xz doesn't have one
                while chunk := fsrc.read(1 << 20):
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        fdst.close()
                        target.unlink(missing_ok=True)
                        raise too_big
                    fdst.write(chunk)
            written.append(target)
    if unsafe:
        log(f"Skipped {unsafe} archive member(s) whose path pointed outside the archive's folder")
    return written
