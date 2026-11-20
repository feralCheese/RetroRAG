"""Archives RetroRAG unpacks when files are added or uploaded: .zip, and tarballs (.tar, .tar.gz / .tgz,
.tar.bz2 / .tbz2, .tar.xz / .txz) - the usual way emulator and system source code is released.

Only ordinary files are taken out. Folders are recreated from the files' paths; links, devices and anything whose
path would land outside the target folder (absolute paths, '..') are skipped.
"""
from __future__ import annotations

import io
import shutil
import tarfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator

ZIP, TAR = "zip", "tar"
TAR_SUFFIXES = (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tbz", ".tar.xz", ".txz", ".tar")
READ_ERRORS = (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError, ValueError)


def kind_by_name(name: str) -> str | None:
    n = name.lower()
    if n.endswith(".zip"):
        return ZIP
    if n.endswith(TAR_SUFFIXES):
        return TAR
    return None


def kind(path: Path) -> str | None:
    """'zip' / 'tar' for an archive RetroRAG can unpack (checked by name and contents), else None."""
    path = Path(path)
    k = kind_by_name(path.name)
    try:
        if k == ZIP and zipfile.is_zipfile(path):
            return ZIP
        if k == TAR and path.is_file() and tarfile.is_tarfile(path):
            return TAR
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
    except READ_ERRORS:
        pass
    return None


def stem(name: str) -> str:
    """'vice-3.9.tar.gz' -> 'vice-3.9', 'disks.zip' -> 'disks'."""
    n = name.lower()
    for suf in (".zip",) + TAR_SUFFIXES:
        if n.endswith(suf) and len(name) > len(suf):
            return name[: -len(suf)]
    return name


def label(k: str | None) -> str:
    return {ZIP: "ZIP", TAR: "TAR"}.get(k or "", "Archive")


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


@contextmanager
def members(src: Path | bytes, k: str | None = None) -> Iterator[Iterator[Member]]:
    """The ordinary files in an archive (a path, or its bytes), in archive order:
        with members(path) as ms:
            for m in ms: ... m.open().read() ...
    A tarball is read front to back, so open each member while it's the current one."""
    fileobj = io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else None
    if k is None:
        k = kind_of_bytes("x.zip", src) or kind_of_bytes("x.tar", src) if fileobj else kind(src)
    if k == ZIP:
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
            strip_top: str | None = None) -> list[Path]:
    """Unpack the ordinary files into out_dir (made if needed) -> the files written. max_files / max_bytes raise
    ValueError when the archive holds more than that. strip_top: a top folder of this name is left out (out_dir
    is already named after the archive, and vice-3.9.tar.gz holds vice-3.9/...: don't nest it twice)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    root = out_dir.resolve()
    written: list[Path] = []
    total = unsafe = 0
    with members(src, k) as ms:
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
            total += m.size
            if max_bytes is not None and total > max_bytes:
                raise ValueError(f"the archive unpacks to more than {max_bytes >> 20} MB")
            target.parent.mkdir(parents=True, exist_ok=True)
            with m.open() as fsrc, open(target, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)
            written.append(target)
    if unsafe:
        log(f"Skipped {unsafe} archive member(s) whose path pointed outside the archive's folder")
    return written
