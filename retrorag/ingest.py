"""Adding files to the library and extracting their text."""
from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path
from typing import Callable, Iterable

from . import binaries
from .library import Document, Library, System, now_iso, safe_filename, slugify
from .textutil import html_to_text, looks_binary, page_marker, read_text_file, rtf_to_markdown

TEXT_EXT = {".txt", ".md", ".markdown", ".rst", ".asc", ".ascii", ".nfo", ".diz", ".doc.txt", ".csv", ".tsv",
            ".json", ".xml", ".ini", ".cfg", ".conf", ".yaml", ".yml", ".tex", ".1st", ".me", ".readme", ".faq"}
HTML_EXT = {".htm", ".html", ".xhtml"}
RTF_EXT = {".rtf"}
from .officedocs import OFFICE_EXT, office_to_markdown  # noqa: E402  (Word / Excel, pure Python)
CONVERTED_EXT = HTML_EXT | RTF_EXT | OFFICE_EXT  # formats with markup/binary layout: indexed via a converted .md
CODE_EXT = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".inl", ".ipp", ".m", ".mm",
            ".asm", ".s", ".a65", ".a80", ".z80asm", ".68k", ".x68", ".i", ".inc", ".mac", ".src", ".lst", ".sym",
            ".bas", ".bbc", ".pas", ".p", ".for", ".f", ".f77", ".ada", ".adb", ".ads", ".lisp", ".lsp", ".fth", ".4th",
            ".py", ".js", ".ts", ".java", ".cs", ".rs", ".go", ".lua", ".pl", ".pm", ".rb", ".php", ".tcl",
            ".sh", ".bash", ".bat", ".cmd", ".ps1", ".mk", ".mak", ".cmake", ".am", ".ac", ".in", ".lay", ".hlsl",
            ".glsl", ".v", ".vhd", ".vhdl", ".sv", ".lua", ".nut", ".def", ".y", ".l", ".rc", ".pro", ".qrc"}
CODE_NAMES = {"makefile", "cmakelists.txt", "readme", "license", "copying", "changelog", "authors", "news",
              "install", "todo", "configure", "dockerfile", "kconfig"}
PDF_EXT = {".pdf"}
IMAGE_EXT = {".gif", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".jfif", ".pbm", ".pgm", ".ppm"}
ARCHIVE_EXT = {".zip"}
SKIP_DIRS = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".vs", ".idea", ".vscode", "_derived",
             "_transfer"}
SKIP_EXT = {".o", ".obj", ".exe", ".dll", ".so", ".dylib", ".a", ".lib", ".pdb", ".pyc", ".class", ".DS_Store"}
DERIVED_SUFFIXES = (".ocr.json",)

KINDS = ("text", "code", "pdf", "image", "binary")

Progress = Callable[[int, int, str], None]
Cancel = Callable[[], bool]


def classify(p: Path) -> str:
    ext, name = p.suffix.lower(), p.name.lower()
    if ext in PDF_EXT:
        return "pdf"
    if ext in IMAGE_EXT:
        return "image"
    if ext in RTF_EXT or ext in OFFICE_EXT:
        return "text"
    if ext in HTML_EXT or ext in TEXT_EXT:
        return "text" if not _sniff_binary(p) else "binary"
    if ext in CODE_EXT or name in CODE_NAMES:
        return "code" if not _sniff_binary(p) else "binary"
    if ext in binaries.FORMATS:
        return "binary"
    return "binary" if _sniff_binary(p) else "text"


def _sniff_binary(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return looks_binary(f.read(8192))
    except OSError:
        return True


def pretty_title(p: Path) -> str:
    stem = p.name
    for suf in (".gz", ".zip"):
        if stem.lower().endswith(suf):
            stem = stem[: -len(suf)]
    stem = Path(stem).stem if Path(stem).suffix.lower() not in ("",) else stem
    t = stem.replace("_", " ").strip()
    return t[:1].upper() + t[1:] if t else p.name


def _unique_dest(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, suf, n = dest.stem, dest.suffix, 2
    while True:
        cand = dest.with_name(f"{stem} ({n}){suf}")
        if not cand.exists():
            return cand
        n += 1


# ======================================================================== add files
def _auto(category: str | None) -> bool:
    """None (or BY_TYPE) as a category: guess each file's category from its own type (categorize.py)."""
    from .categorize import BY_TYPE
    return category is None or category == BY_TYPE


def add_files(lib: Library, system: System, category: str | None, paths: Iterable[Path], *, copy: bool = True,
              group_path: str = "", progress: Progress | None = None, cancel: Cancel | None = None,
              kinds: set[str] | None = None, categories: dict[str, str] | None = None,
              log: Callable[[str], None] = print) -> list[int]:
    """Add files. category applies to all of them; None guesses each file's own (a zip: its contents').
    categories: per-file overrides, {str(path): category}. For a zip, BY_TYPE files each of its files under its
    own type's category."""
    from .categorize import guess_file
    paths = [Path(p) for p in paths]
    added: list[int] = []
    for i, src in enumerate(paths):
        if cancel and cancel():
            log("Cancelled.")
            break
        if progress:
            progress(i, len(paths), src.name)
        try:
            cat = (categories or {}).get(str(src), category)
            if src.suffix.lower() in ARCHIVE_EXT and lib.settings.extract_archives and zipfile.is_zipfile(src):
                if cat is None:
                    cat = guess_file(src)
                added += _add_archive(lib, system, cat, src, group_path, kinds, log, cancel)
                continue
            if _auto(cat):
                cat = guess_file(src)
            doc_id = add_file(lib, system, cat, src, copy=copy, group_path=group_path, kinds=kinds, log=log)
            if doc_id:
                added.append(doc_id)
        except Exception as e:  # keep going; one bad file shouldn't stop a batch
            log(f"ERROR adding {src.name}: {e}")
    if progress:
        progress(len(paths), len(paths), "done")
    return added


def add_file(lib: Library, system: System, category: str, src: Path, *, copy: bool = True,
             group_path: str = "", kinds: set[str] | None = None, title: str | None = None,
             log: Callable[[str], None] = print) -> int | None:
    from .config import canonical_category
    category = canonical_category(category)
    src = Path(src)
    if not src.is_file() or src.name.endswith(DERIVED_SUFFIXES) or src.suffix in SKIP_EXT:
        return None
    kind = classify(src)
    if kinds is not None and kind not in kinds:
        return None
    hashes = binaries.file_hashes(src)
    dup = lib.conn().execute("SELECT id, title FROM documents WHERE system_id=? AND sha1=?",
                             (system.id, hashes["sha1"])).fetchone()
    if dup:
        log(f"Skipped {src.name}: identical to existing '{dup['title']}'")
        return None
    in_transfer = (lib.root / "_transfer").resolve() in src.resolve().parents   # uploads: temporary, so copy
    if copy and lib.root in src.resolve().parents and not in_transfer:
        stored = lib.relpath(src)            # already inside the library (re-import / extracted archive)
    elif copy:
        dest_dir = lib.system_dir(system) / slugify(category) / group_path
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = _unique_dest(dest_dir / safe_filename(src.name))
        shutil.copy2(src, dest)
        stored = lib.relpath(dest)
    else:
        stored = str(src.resolve())
    doc_id = lib.insert_document(
        system_id=system.id, category=category, title=title or pretty_title(src), path=stored, text_path="",
        kind=kind, ext=src.suffix.lower(), size=src.stat().st_size, group_path=group_path.replace("\\", "/"),
        sha1=hashes["sha1"], md5=hashes["md5"], crc32=hashes["crc32"])
    d = lib.document(doc_id)
    if not adopt_existing_text(lib, d, log):
        extract(lib, d, log=log)
    return doc_id


def adopt_existing_text(lib: Library, d: Document, log: Callable[[str], None] = print) -> bool:
    """If OCR output already sits next to the file (e.g. re-importing a system whose files were kept), reuse it
    instead of re-extracting - so proofreading work is never thrown away."""
    if d.kind not in ("pdf", "image"):
        return False
    md, js = lib.derived_md_path(d), lib.ocr_json_path(d)
    if not (md.exists() and js.exists()):
        return False
    from .textutil import count_markers
    text = md.read_text("utf-8")
    pages = 0
    try:
        if d.kind == "pdf":
            import pymupdf
            with pymupdf.open(lib.abspath(d.path)) as doc:
                pages = doc.page_count
        else:
            from PIL import Image
            with Image.open(lib.abspath(d.path)) as im:
                pages = getattr(im, "n_frames", 1)
    except Exception:
        pass
    lib.update_document(d.id, text_path=lib.relpath(md), text_status="ocr", pages=pages,
                        needs_ocr=int("<!-- no text layer" in text), flagged=count_markers(text))
    log(f"Re-used existing OCR text for {d.title}")
    return True


# ======================================================================== portability helpers
MANIFEST = "system.json"


def write_manifest(lib: Library, s: System) -> Path:
    """Save system + document metadata (titles, categories, tags, notes...) inside the system's folder, so the
    folder is self-describing: it can be re-imported later or into another library on another PC."""
    import json
    sdir = lib.system_dir(s)
    sdir.mkdir(parents=True, exist_ok=True)
    docs = {}
    for d in lib.documents(s.id):
        p = lib.abspath(d.path)
        try:
            key = p.resolve().relative_to(sdir.resolve()).as_posix()
        except ValueError:
            key = str(p)       # linked in place
        docs[key] = {k: getattr(d, k) for k in ("title", "category", "tags", "notes", "source_url", "group_path",
                                                 "text_status")}
        if d.link_id and (l := lib.link(d.link_id)):
            docs[key]["link_url"] = l.url
    meta = {k: getattr(s, k) for k in ("name", "manufacturer", "year", "kind", "cpu", "aliases", "notes")}
    if s.parent_id and (par := lib.system(s.parent_id)):
        meta["variant_of"] = par.name
    extra = {}
    hosts = [h.name for h in lib.hosts(s.id)]
    if hosts:
        extra["works_with"] = hosts
    links = [{k: getattr(l, k) for k in ("url", "title", "note", "mode", "category", "max_pages", "include_files",
                                         "branch", "refresh_days", "status", "status_msg", "last_fetched")}
             for l in lib.links(s.id)]
    if links:
        extra["links"] = links
    out = sdir / MANIFEST
    out.write_text(json.dumps({"system": meta, "documents": docs, **extra}, indent=1), "utf-8")
    return out


def restore_links(lib: Library, s: System, manifest: dict) -> dict[str, int]:
    """Bring back a removed system's links and 'works with' machines from its manifest. -> {url: link id}"""
    out: dict[str, int] = {}
    have = {l.url: l.id for l in lib.links(s.id)}
    for l in manifest.get("links", []):
        url = l.get("url")
        if not url:
            continue
        if url in have:
            out[url] = have[url]
            continue
        try:
            new = lib.add_link(s.id, url, **{k: v for k, v in l.items() if k != "url"})
            out[url] = new.id
        except ValueError:
            pass
    hosts = [h.id for n in manifest.get("works_with", []) if (h := lib.find_system(n))]
    if hosts:
        lib.set_hosts(s.id, sorted({h.id for h in lib.hosts(s.id)} | set(hosts)))
    return out


def read_manifest(folder: Path) -> dict:
    import json
    f = Path(folder) / MANIFEST
    if f.exists():
        try:
            return json.loads(f.read_text("utf-8"))
        except Exception:
            pass
    return {}


def import_system_folder(lib: Library, s: System, folder: Path | None = None, progress: Progress | None = None,
                         cancel: Cancel | None = None, log: Callable[[str], None] = print) -> list[int]:
    """Import a system folder laid out as <category-slug>/<group...>/<file> (what RetroRAG itself writes).
    Used to re-attach files kept after removing a system, or to import a system folder copied from another PC.
    Existing OCR text and the system.json manifest (titles, tags, notes) are reused."""
    from .config import CATEGORIES
    folder = Path(folder or lib.system_dir(s))
    manifest = read_manifest(folder)
    mdocs = manifest.get("documents", {})
    by_slug = {slugify(c): c for c in CATEGORIES}
    files = [f for f in iter_folder(folder) if f.name != MANIFEST]
    names = {f.as_posix().lower() for f in files}
    files = [f for f in files if not (f.suffix.lower() == ".md" and f.as_posix().lower()[:-3] in names)]
    target_root = lib.system_dir(s)
    link_ids = restore_links(lib, s, manifest)
    added = []
    for i, f in enumerate(files):
        if cancel and cancel():
            break
        if progress and i % 5 == 0:
            progress(i, len(files), f.name)
        rel = f.relative_to(folder)
        meta = mdocs.get(rel.as_posix(), {})
        parts = rel.parts
        category = meta.get("category") or by_slug.get(parts[0], "Other") if len(parts) > 1 else meta.get("category", "Other")
        group = meta.get("group_path") if meta else "/".join(parts[1:-1])
        src = f
        if folder.resolve() != target_root.resolve():   # importing from elsewhere: copy into this library
            dest = target_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copy2(f, dest)
            for suffix in (".md", ".ocr.json"):
                side = f.with_name(f.name + suffix)
                if side.exists() and not dest.with_name(dest.name + suffix).exists():
                    shutil.copy2(side, dest.with_name(dest.name + suffix))
            src = dest
        try:
            doc_id = add_file(lib, s, category, src, copy=True, group_path=group or "", title=meta.get("title"), log=log)
        except Exception as e:
            log(f"ERROR importing {rel}: {e}")
            continue
        if doc_id:
            upd = {k: meta[k] for k in ("tags", "notes", "source_url") if meta.get(k)}
            if meta.get("link_url"):
                lid = link_ids.get(meta["link_url"])
                if lid:
                    upd["link_id"] = lid
            if meta.get("text_status") == "reviewed":
                upd["text_status"] = "reviewed"
            if upd:
                lib.update_document(doc_id, **upd)
            added.append(doc_id)
    log(f"Imported {len(added)} document(s) into {s.name}")
    return added


def consolidate_linked(lib: Library, log: Callable[[str], None] = print) -> int:
    """Copy every linked-in-place (absolute path) original into the library so the library folder is
    fully self-contained and can be moved to another PC."""
    n = 0
    for d in lib.documents(where="(path LIKE '_:%' OR path LIKE '/%' OR path LIKE '\\\\%')"):
        src = Path(d.path)
        if not src.exists():
            log(f"Missing linked file, skipped: {src}")
            continue
        s = lib.system(d.system_id)
        old_md, old_js = lib.derived_md_path(d), lib.ocr_json_path(d)
        dest_dir = lib.system_dir(s) / slugify(d.category) / d.group_path
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = _unique_dest(dest_dir / safe_filename(src.name))
        shutil.copy2(src, dest)
        upd = {"path": lib.relpath(dest)}
        for old, suffix in ((old_md, ".md"), (old_js, ".ocr.json")):
            if old.exists():
                new = dest.with_name(dest.name + suffix)
                shutil.move(str(old), new)
                if suffix == ".md":
                    upd["text_path"] = lib.relpath(new)
        if d.text_path == d.path:
            upd["text_path"] = upd["path"]
        lib.update_document(d.id, **upd)
        n += 1
    log(f"Copied {n} linked file(s) into the library.")
    return n


def _extract_zip(src: Path, out_dir: Path, log) -> None:
    root = out_dir.resolve()
    with zipfile.ZipFile(src) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            target = (out_dir / info.filename).resolve()
            if root not in target.parents:          # zip-slip guard
                log(f"Skipped unsafe archive member {info.filename}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as fsrc, open(target, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)


def _add_archive(lib, system, category, src, group_path, kinds, log, cancel) -> list[int]:
    group = safe_filename(src.stem)
    rel_group = (Path(group_path) / group).as_posix() if group_path else group
    if _auto(category):
        # each file under its own type's category: unpack outside the library, then copy each file to
        # systems/<system>/<its category>/<zip name>/...
        import tempfile
        with tempfile.TemporaryDirectory(prefix="retrorag-zip-") as tmp:
            out_dir = Path(tmp) / group
            out_dir.mkdir()
            _extract_zip(src, out_dir, log)
            log(f"Extracted {src.name} (each file filed by its type)")
            return add_folder(lib, system, None, out_dir, copy=True, group_root=rel_group, kinds=kinds, log=log,
                              cancel=cancel)
    out_dir = lib.system_dir(system) / slugify(category) / group_path / group
    out_dir = _unique_dest(out_dir)
    out_dir.mkdir(parents=True)
    _extract_zip(src, out_dir, log)
    log(f"Extracted {src.name} -> {lib.relpath(out_dir)}")
    rel_group = (Path(group_path) / out_dir.name).as_posix() if group_path else out_dir.name
    return add_folder(lib, system, category, out_dir, copy=True, group_root=rel_group, kinds=kinds, log=log, cancel=cancel)


def iter_folder(folder: Path) -> list[Path]:
    files = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            if fn.startswith(".") or fn.endswith(DERIVED_SUFFIXES) or Path(fn).suffix.lower() in SKIP_EXT:
                continue
            files.append(Path(dirpath) / fn)
    return files


def add_folder(lib: Library, system: System, category: str | None, folder: Path, *, copy: bool = True,
               group_root: str | None = None, kinds: set[str] | None = None, progress: Progress | None = None,
               cancel: Cancel | None = None, log: Callable[[str], None] = print) -> list[int]:
    """Add a whole tree (e.g. an emulator's source). Folder structure is kept as each document's group_path.
    category None (or BY_TYPE): each file under its own type's category."""
    from .categorize import guess_file
    auto = _auto(category)
    folder = Path(folder)
    group_root = group_root if group_root is not None else safe_filename(folder.name)
    files = iter_folder(folder)
    # Derived text (.md next to an original we also add) would be double-counted
    names = {f.as_posix().lower() for f in files}
    files = [f for f in files if not (f.suffix.lower() == ".md" and f.as_posix().lower()[:-3] in names)]
    log(f"Scanning {folder}: {len(files)} files")
    added = []
    for i, f in enumerate(files):
        if cancel and cancel():
            log("Cancelled.")
            break
        if progress and (i % 5 == 0 or i == len(files) - 1):
            progress(i, len(files), f.name)
        rel_parent = f.parent.relative_to(folder).as_posix()
        gp = group_root if rel_parent == "." else f"{group_root}/{rel_parent}"
        try:
            doc_id = add_file(lib, system, guess_file(f) if auto else category, f, copy=copy, group_path=gp,
                              kinds=kinds, log=log)
            if doc_id:
                added.append(doc_id)
        except Exception as e:
            log(f"ERROR adding {f}: {e}")
    if progress:
        progress(len(files), len(files), "done")
    log(f"Added {len(added)} documents from {folder.name}")
    return added


# ======================================================================== extraction
def extract(lib: Library, d: Document, log: Callable[[str], None] = print) -> None:
    """(Re)build the text for a document from its original (does not touch OCR output for images)."""
    src = lib.abspath(d.path)
    s = lib.system(d.system_id)
    upd: dict = {"text_updated": now_iso()}
    if d.kind == "code" or (d.kind == "text" and d.ext not in CONVERTED_EXT):
        upd.update(text_path=d.path, text_status="extracted", needs_ocr=0)
    elif d.kind == "text":   # html / rtf: convert to readable markdown (editable copy next to the original)
        md = lib.derived_md_path(d)
        if d.ext in RTF_EXT:
            body = rtf_to_markdown(src.read_bytes())
            md.write_text(f"# {d.title}\n\n" + body, "utf-8")
        elif d.ext in OFFICE_EXT:
            md.write_text(f"# {d.title}\n\n" + office_to_markdown(src), "utf-8")
        else:
            md.write_text(html_to_text(read_text_file(src)), "utf-8")
        upd.update(text_path=lib.relpath(md), text_status="extracted", needs_ocr=0)
    elif d.kind == "pdf":
        upd.update(_extract_pdf(lib, d, src))
    elif d.kind == "image":
        try:
            from PIL import Image
            with Image.open(src) as im:
                upd["pages"] = getattr(im, "n_frames", 1)
        except Exception as e:
            log(f"Could not open image {src.name}: {e}")
        from .categorize import PHOTOS
        # photos aren't queued for OCR (a board photo gives a few chip numbers at best, mostly junk); the AI can
        # look at them instead (view_page), and Run OCR still works on one when asked
        upd.update(needs_ocr=int(d.category != PHOTOS), text_status="none")
    else:  # binary
        md = lib.derived_md_path(d)
        md.write_text(binaries.info_card(src, d.title, s.name if s else "", d.category,
                                         {"sha1": d.sha1, "md5": d.md5, "crc32": d.crc32}, d.notes,
                                         lib.settings.strings_min_len), "utf-8")
        upd.update(text_path=lib.relpath(md), text_status="info", needs_ocr=0)
    lib.update_document(d.id, **upd)


def scanned_pages(pdf_path: Path, min_chars: int) -> list[int]:
    import pymupdf
    out = []
    with pymupdf.open(pdf_path) as doc:
        for i, page in enumerate(doc):
            if len(page.get_text("text").strip()) < min_chars:
                out.append(i + 1)
    return out


def _extract_pdf(lib: Library, d: Document, src: Path) -> dict:
    import pymupdf
    min_chars = lib.settings.pdf_min_text_chars
    parts, scanned = [], 0
    with pymupdf.open(src) as doc:
        n = doc.page_count
        for i, page in enumerate(doc):
            txt = page.get_text("text", sort=True).strip()
            if len(txt) < min_chars:
                scanned += 1
                txt = "<!-- no text layer: needs OCR -->"
            parts.append(f"{page_marker(i + 1)}\n{txt}\n")
    md = lib.derived_md_path(d)
    md.write_text(f"# {d.title}\n\n" + "\n".join(parts), "utf-8")
    status = "extracted" if scanned == 0 else ("partial" if scanned < n else "none")
    return {"text_path": lib.relpath(md), "text_status": status, "needs_ocr": int(scanned > 0), "pages": n}


def refresh_info_cards(lib: Library, d: Document) -> None:
    """Binary info cards embed title/notes; regenerate when those change."""
    if d.kind == "binary":
        extract(lib, d)
