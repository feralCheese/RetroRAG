"""Disk and tape images in the library: list, read, extract, add their files, and create new images.

Shared by the app, the MCP server, the REST API and the command line, so they all behave the same.
"""
from __future__ import annotations

import base64
import re
import tempfile
from pathlib import Path
from typing import Callable

from . import media
from .library import Document, Library, System, safe_filename
from .media import Entry, Image, MediaError, NewFile

IMAGE_CATEGORY = "Disk & Tape Images"
_cache: dict = {}


# ---------------------------------------------------------------- opening
def doc_format(lib: Library, d: Document | None) -> media.MediaFormat | None:
    """The disk/tape format of a library document, or None if it isn't an image RetroRAG can read."""
    if d is None or d.kind != "binary" or d.ext.lower() not in media.media_exts() | {".md"}:
        return None
    key = (d.id, d.sha1)
    if key not in _cache:
        try:
            p = lib.abspath(d.path)
            _cache[key] = media.detect(p.read_bytes(), d.ext) if p.stat().st_size <= 8 << 20 else None
        except OSError:
            _cache[key] = None
    return _cache[key]


def open_doc(lib: Library, d: Document) -> Image:
    fmt = doc_format(lib, d)
    if fmt is None:
        raise MediaError(f"'{d.title}' isn't a disk or tape image RetroRAG can read")
    return fmt.open(lib.abspath(d.path).read_bytes())


def entry_info(img: Image, e: Entry) -> dict:
    out = {"name": e.name, "type": "folder" if e.is_dir else e.kind, "size": e.size}
    for k in ("load", "exec", "start"):
        if e.meta.get(k) is not None:
            out[k] = media.fmt_addr(e.meta[k], img.fmt.hex_prefix)
    for k in ("autostart", "locked", "blocks", "date", "protect", "attrs", "comment"):
        if e.meta.get(k) not in (None, "", False):
            out[k] = e.meta[k]
    return out


def image_info(img: Image) -> dict:
    return {"format": img.fmt.id, "format_name": img.fmt.name, "label": img.label, "info": img.info,
            "files": [entry_info(img, e) for e in img.entries()]}


# ---------------------------------------------------------------- getting files out
def base_name(img: Image, e: Entry) -> str:
    """The file's own name without folders, drive or the DFS '$.' directory."""
    name = e.name
    if img.fmt.id in ("ssd", "dsd"):
        name = re.sub(r"^:\d\.", "", name)
        return name[2:] if name.startswith("$.") else name
    return name.split("/")[-1]


def host_name(img: Image, e: Entry) -> str:
    """A sensible file name on the PC for a file from an image."""
    f, name = img.fmt, e.name
    if f.id in ("ssd", "dsd"):
        name = base_name(img, e)
    if f.basic == "cbm" and e.kind in ("PRG", "SEQ", "USR", "REL", "DEL"):
        name = f"{name}.{e.kind.lower()}"
    elif f.basic == "zx":
        if e.kind == "Program":
            name += ".tap"
        elif e.meta.get("screen"):
            name += ".scr"
        else:
            name += ".bin"
    return media.safe_host_name(name)


def export_bytes(img: Image, e: Entry) -> bytes:
    """The file as it is usually kept on a PC (a Spectrum BASIC program goes in a one-file .tap so its autostart
    line survives)."""
    data = img.read(e)
    if img.fmt.basic == "zx" and e.kind == "Program":
        return media.create("tap", [NewFile(e.name, data, "Program", exec=e.meta.get("autostart"))])
    return data


def readable_copy(img: Image, e: Entry) -> tuple[str, bytes]:
    """For adding to the library: BASIC programs become their listing (.bas) and text files plain text (.txt), so
    they're readable and searchable; anything else is the file itself."""
    try:
        how, text = media.text_view(img, e)
    except Exception:
        how, text = "", ""
    stem = media.safe_host_name(base_name(img, e))
    if how.endswith("BASIC listing"):
        return f"{stem}.bas", text.encode("utf-8")
    if how == "text":
        return (stem if re.search(r"\.(txt|doc|me|nfo|asc|1st|md)$", stem, re.I) else stem + ".txt"), text.encode("utf-8")
    return host_name(img, e).split("/")[-1], export_bytes(img, e)


def extract_to_folder(img: Image, names: list[str] | None, dest: Path) -> list[Path]:
    """Write files out exactly as stored (Acorn files also get .inf files with their addresses)."""
    entries = [img.find(n) for n in names] if names else img.files()
    dest = Path(dest)
    out = []
    acorn = img.fmt.id in ("ssd", "dsd", "adl")
    for e in entries:
        if e.is_dir:
            continue
        rel = host_name(img, e) if img.fmt.id not in ("img", "st", "msa", "adf", "adl") else media.safe_host_name(e.name)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(export_bytes(img, e))
        out.append(target)
        if acorn:
            load, exe = e.meta.get("load") or 0, e.meta.get("exec") or 0
            leaf = e.name.split("/")[-1]
            leaf = re.sub(r"^:\d\.", "", leaf)
            Path(str(target) + ".inf").write_text(
                f"{leaf} {load:06X} {exe:06X} {e.size:06X}" + (" Locked" if e.meta.get("locked") else "") + "\n",
                "latin-1", errors="replace")
    return out


def add_entries_to_library(lib: Library, d: Document, names: list[str] | None = None,
                           log: Callable[[str], None] = print) -> list[int]:
    """Add files from an image as documents of the same system, in a folder named after the image, each linked
    to the image ('on disk / tape image')."""
    from . import ingest
    from .relations import add_relation
    img = open_doc(lib, d)
    entries = [img.find(n) for n in names] if names else img.files()
    s = lib.system(d.system_id)
    group = "/".join(x for x in (d.group_path, safe_filename(d.title)) if x)
    ids = []
    with tempfile.TemporaryDirectory(prefix="retrorag-img-") as tmp:
        for e in entries:
            if e.is_dir:
                continue
            fname, data = readable_copy(img, e)
            p = Path(tmp) / f"{len(ids)}" / safe_filename(fname)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            sub = "/".join(x for x in [group] + e.name.split("/")[:-1] if x) if img.fmt.dirs else group
            title = base_name(img, e)
            new_id = ingest.add_file(lib, s, d.category, p, copy=True, group_path=sub, title=title, log=log)
            if new_id is None:                         # identical file already in the library
                row = lib.conn().execute("SELECT id FROM documents WHERE system_id=? AND sha1=?",
                                         (s.id, media_sha1(data))).fetchone()
                new_id = row["id"] if row else None
            if new_id:
                try:
                    add_relation(lib, new_id, d.id, "on_image", note=e.name if e.name != title else "")
                except Exception:
                    pass
                ids.append(new_id)
    return ids


def media_sha1(data: bytes) -> str:
    import hashlib
    return hashlib.sha1(data).hexdigest()


# ---------------------------------------------------------------- making new images
def parse_number(v, hex_default: bool = True) -> int | None:
    """$C000 / &1900 / 0xC000 / C000 (hex unless hex_default is False) / 49152 / an int."""
    if v is None or v == "":
        return None
    if isinstance(v, int):
        return v
    s = str(v).strip().replace("_", "")
    if not s:
        return None
    try:
        if s[0] in "$&#":
            return int(s[1:], 16)
        if s.lower().startswith("0x"):
            return int(s, 16)
        if hex_default:
            return int(s, 16)
        return int(s, 10)
    except ValueError:
        raise MediaError(f"Not a number: {v!r}")


def text_to_bytes(fmt: media.MediaFormat, text: str) -> bytes:
    """Plain text -> a text file in the machine's own conventions (PETSCII, CR or CR/LF line ends)."""
    from .media.cbm import ascii_to_petscii
    raw = text.replace("\r\n", "\n").encode("latin-1", "replace")
    if fmt.basic == "cbm":
        return ascii_to_petscii(raw)
    if fmt.basic in ("bbc", "zx"):
        return raw.replace(b"\n", b"\r")
    if fmt.id in ("img", "st", "msa"):
        return raw.replace(b"\n", b"\r\n")
    return raw


def new_file(fmt: media.MediaFormat, name: str, *, data: bytes | None = None, text: str | None = None,
             basic: bool = False, host_name: str | None = None, kind: str = "", load=None, exec_=None,
             autostart=None) -> NewFile:
    """One file for a new image, from bytes, text or BASIC source; type and addresses guessed unless given."""
    if basic:
        if text is None:
            raise MediaError(f"{name}: BASIC needs the program text")
        start = parse_number(load) if fmt.basic == "cbm" else None
        prog, k = media.program_from_text(fmt, text, start)
        nf = NewFile(name, prog, k)
        if fmt.basic == "zx":
            nf.exec = parse_number(autostart, False) if autostart is not None else int.from_bytes(prog[:2], "big")
        elif fmt.basic == "bbc":
            from .media.basic import BBC_EXEC, BBC_LOAD
            nf.load, nf.exec = BBC_LOAD, BBC_EXEC
        return nf
    if data is None:
        if text is None:
            raise MediaError(f"{name}: no data")
        data = text_to_bytes(fmt, text)
        nf = NewFile(name, data, "SEQ" if fmt.basic == "cbm" else "")
    else:
        nf = media.prepare(fmt, host_name or name, data)
        nf.name = name or nf.name
    if kind:
        nf.kind = _norm_kind(fmt, kind)
    if load not in (None, ""):
        nf.load = parse_number(load)
        if fmt.basic == "cbm" and nf.kind in ("", "PRG"):
            pass                                   # create_cbm prepends it
    if exec_ not in (None, ""):
        nf.exec = parse_number(exec_)
    if autostart not in (None, ""):
        nf.exec = parse_number(autostart, False)
    return nf


def _norm_kind(fmt: media.MediaFormat, kind: str) -> str:
    k = kind.strip()
    for opt in fmt.kinds:
        if opt.lower() == k.lower():
            return opt
    alias = {"code": "Bytes", "basic": "Program" if fmt.basic == "zx" else "BASIC" if fmt.basic == "bbc" else "PRG"}
    if k.lower() in alias and alias[k.lower()] in fmt.kinds:
        return alias[k.lower()]
    if not fmt.kinds or fmt.kinds == ("file",):
        return ""
    raise MediaError(f"File type for {fmt.name} must be one of: {', '.join(fmt.kinds)} (not {kind!r})")


def create_image_doc(lib: Library, system: System, fmt_id: str, files: list[NewFile], label: str = "",
                     title: str | None = None, category: str = IMAGE_CATEGORY, options: dict | None = None,
                     source_doc_ids: list[int] | None = None, group_path: str = "",
                     log: Callable[[str], None] = print) -> int:
    """Build an image and add it to the library. Returns the new document's id."""
    from . import ingest
    from .relations import add_relation
    fmt = media.get(fmt_id)
    if not files:
        raise MediaError("Add at least one file")
    raw = fmt.create(files, label, **(options or {}))
    title = (title or label or "New " + fmt.medium).strip()
    ext = fmt.ext
    if fmt.id == "adl" and (options or {}).get("size", "L").upper() in ("M", "S"):
        ext = ".ad" + options["size"].lower()
    with tempfile.TemporaryDirectory(prefix="retrorag-new-") as tmp:
        p = Path(tmp) / (safe_filename(title) + ext)
        p.write_bytes(raw)
        doc_id = ingest.add_file(lib, system, category, p, copy=True, group_path=group_path, title=title, log=log)
    if doc_id is None:
        row = lib.conn().execute("SELECT id FROM documents WHERE system_id=? AND sha1=?",
                                 (system.id, media_sha1(raw))).fetchone()
        if not row:
            raise MediaError("Could not add the image to the library")
        doc_id = row["id"]
    for src in source_doc_ids or []:
        try:
            add_relation(lib, src, doc_id, "on_image")
        except Exception:
            pass
    log(f"Created {fmt.name} '{title}' with {len(files)} file(s)")
    return doc_id


def files_from_specs(lib: Library, fmt: media.MediaFormat, specs: list[dict]) -> tuple[list[NewFile], list[int]]:
    """JSON file specs (from the AI / REST) -> NewFiles. Each spec: name + one of text / base64 / doc_id, and
    optionally type, basic, load, exec, autostart."""
    out, sources = [], []
    for i, sp in enumerate(specs):
        name = str(sp.get("name") or "").strip()
        basic = bool(sp.get("basic"))
        kind = str(sp.get("type") or "")
        if kind.lower() == "basic" and fmt.basic and "text" in sp:
            basic = True
        if sp.get("upload"):                          # a file the user uploaded (see transfer.py)
            from . import transfer
            p = transfer.resolve(lib, str(sp["upload"]))
            data = p.read_bytes()
            nf = new_file(fmt, name, data=data, host_name=p.name, kind=kind, load=sp.get("load"),
                          exec_=sp.get("exec"), autostart=sp.get("autostart"))
            if not name:
                nf.name = media.prepare(fmt, p.name, data).name
        elif sp.get("doc_id") is not None:
            d = lib.document(int(sp["doc_id"]))
            if not d:
                raise MediaError(f"File {i + 1}: no document with id {sp['doc_id']}")
            data = lib.abspath(d.path).read_bytes()
            nf = new_file(fmt, name, data=data, host_name=Path(d.path).name, kind=kind, load=sp.get("load"),
                          exec_=sp.get("exec"), autostart=sp.get("autostart"))
            if not name:
                nf.name = media.prepare(fmt, Path(d.path).name, data).name
            sources.append(d.id)
        elif sp.get("base64") is not None:
            if not name:
                raise MediaError(f"File {i + 1}: needs a name")
            nf = new_file(fmt, name, data=base64.b64decode(sp["base64"]), kind=kind, load=sp.get("load"),
                          exec_=sp.get("exec"), autostart=sp.get("autostart"))
        elif sp.get("text") is not None:
            if not name:
                raise MediaError(f"File {i + 1}: needs a name")
            nf = new_file(fmt, name, text=str(sp["text"]), basic=basic, kind="" if basic else kind,
                          load=sp.get("load"), exec_=sp.get("exec"), autostart=sp.get("autostart"))
        else:
            raise MediaError(f"File {i + 1}: give text, base64, upload or doc_id")
        out.append(nf)
    return out, sources


# ---------------------------------------------------------------- without the library (uploads / downloads)
def open_source(lib: Library, doc_id: int | None = None, upload: str | None = None) -> tuple[Image, str, Document | None]:
    """An image from the library (doc_id) or from an upload ('<id>/<file>') -> (image, title, document or None)."""
    if doc_id is not None:
        d = lib.document(int(doc_id))
        if not d:
            raise MediaError(f"No document with id {doc_id}")
        return open_doc(lib, d), d.title, d
    if upload:
        from . import transfer
        p = transfer.resolve(lib, upload)
        try:
            img = media.open_image(p.read_bytes(), p.suffix)
        except MediaError:
            raise MediaError(f"{p.name} isn't a disk or tape image RetroRAG can read "
                             f"({', '.join(sorted(media.media_exts()))})")
        return img, p.stem, None
    raise MediaError("Say which image: doc_id (in the library) or upload (from list_uploads)")


def extract_to_area(lib: Library, img: Image, names: list[str] | None, title: str) -> dict:
    """Extract files into a new download area, plus one zip of them all. -> links / paths."""
    from . import transfer
    tid, folder = transfer.new_area(lib, "output")
    stem = safe_filename(title) or "files"
    files = extract_to_folder(img, names, folder / stem)
    import zipfile
    zpath = folder / f"{stem}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.relative_to(folder).as_posix())
            inf = Path(str(f) + ".inf")
            if inf.exists():
                zf.write(inf, inf.relative_to(folder).as_posix())
    base = transfer.base_url(lib)
    out = {"all_files_zip": transfer.file_info(lib, zpath, base),
           "files": [transfer.file_info(lib, f, base) for f in files]}
    if base is None:
        out["note"] = transfer.not_running_note(lib)
    return out


def image_to_area(lib: Library, fmt_id: str, files: list[NewFile], label: str = "", title: str | None = None,
                  options: dict | None = None) -> dict:
    """Build an image and offer it as a download, without adding it to the library."""
    from . import transfer
    fmt = media.get(fmt_id)
    if not files:
        raise MediaError("Add at least one file")
    raw = fmt.create(files, label, **(options or {}))
    title = (title or label or "new " + fmt.medium).strip()
    ext = fmt.ext
    if fmt.id == "adl" and (options or {}).get("size", "L").upper() in ("M", "S"):
        ext = ".ad" + options["size"].lower()
    tid, _ = transfer.new_area(lib, "output")
    p = transfer.save_output(lib, tid, safe_filename(title) + ext, raw)
    out = {"image": transfer.file_info(lib, p), **image_info(fmt.open(raw))}
    if "download_url" not in out["image"]:
        out["note"] = transfer.not_running_note(lib)
    return out


def add_upload_to_library(lib: Library, ref: str, system: System, category: str | None = None,
                          title: str | None = None, log: Callable[[str], None] = print) -> list[int]:
    """Add an uploaded file - or every file of an upload, given just its id - to the library."""
    from . import ingest, transfer
    ref = (ref or "").strip().strip("/")
    if "/" in ref:
        paths = [transfer.resolve(lib, ref)]
    else:
        root = transfer.area(lib, ref)
        paths = sorted(p for p in root.rglob("*") if p.is_file() and p.name != ".meta.json")
    ids = []
    for p in paths:
        cat = category or (IMAGE_CATEGORY if media.detect(p.read_bytes(), p.suffix) else "Other")
        i = ingest.add_file(lib, system, cat, p, copy=True, title=title if len(paths) == 1 else None, log=log)
        if i is None:
            row = lib.conn().execute("SELECT id FROM documents WHERE system_id=? AND sha1=?",
                                     (system.id, media_sha1(p.read_bytes()))).fetchone()
            i = row["id"] if row else None
        if i:
            ids.append(i)
    return ids
