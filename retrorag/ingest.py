"""Adding files to the library and extracting their text."""
from __future__ import annotations

import os
import re
import shutil
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
SKIP_DIRS = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".vs", ".idea", ".vscode", "_derived",
             "_transfer"}
SKIP_EXT = {".o", ".obj", ".exe", ".dll", ".so", ".dylib", ".a", ".lib", ".pdb", ".pyc", ".class", ".DS_Store"}
DERIVED_SUFFIXES = (".ocr.json",)

KINDS = ("text", "code", "pdf", "image", "binary")

Progress = Callable[[int, int, str], None]
Cancel = Callable[[], bool]


def classify(p: Path, head: bytes | None = None) -> str:
    """'text' / 'code' / 'pdf' / 'image' / 'binary'. head: the file's first bytes, when p isn't on disk."""
    ext, name = p.suffix.lower(), p.name.lower()
    _sniff_binary = (lambda _p: looks_binary(head)) if head is not None else _sniff_file
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


def _sniff_file(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return looks_binary(f.read(8192))
    except OSError:
        return True


def pretty_title(p: Path) -> str:
    stem = p.name
    for suf in (".gz", ".bz2", ".xz", ".zip"):
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
              archive_kinds: set[str] | None = None, log: Callable[[str], None] = print) -> list[int]:
    """Add files. category applies to all of them; None guesses each file's own (an archive: its contents').
    categories: per-file overrides, {str(path): category}. Archives (.zip, .tar.gz...) are unpacked; for one,
    BY_TYPE files each of its files under its own type's category. kinds: the file types to add (None = all);
    archive_kinds: the types to take out of archives (None = kinds)."""
    from . import archives
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
            k = archives.kind(src) if lib.settings.extract_archives else None
            if k == archives.ONE:                     # manual.txt.gz: add what's inside
                doc_id = _add_compressed(lib, system, cat, src, group_path, kinds, log)
                if doc_id:
                    added.append(doc_id)
                continue
            if k:
                if cat is None:
                    cat = guess_file(src)
                added += _add_archive(lib, system, cat, src, group_path,
                                      archive_kinds if archive_kinds is not None else kinds, log, cancel)
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


def _add_archive(lib, system, category, src, group_path, kinds, log, cancel) -> list[int]:
    """Unpack outside the library, then copy in the files that are added: systems/<system>/<category>/<archive
    name>/... (with BY_TYPE each file goes under its own type's category). Files left out (kinds) leave nothing
    behind."""
    import tempfile
    from . import archives
    group = safe_filename(archives.stem(src.name))
    rel_group = (Path(group_path) / group).as_posix() if group_path else group
    with tempfile.TemporaryDirectory(prefix="retrorag-unpack-") as tmp:
        out_dir = Path(tmp) / group
        n = len(archives.extract(src, out_dir, log, strip_top=archives.stem(src.name)))
        log(f"Unpacked {src.name}: {n} file(s)" + (" (each filed by its type)" if _auto(category) else ""))
        return add_folder(lib, system, None if _auto(category) else category, out_dir, copy=True,
                          group_root=rel_group, kinds=kinds, log=log, cancel=cancel)


def _add_compressed(lib, system, category, src, group_path, kinds, log) -> int | None:
    """A single compressed file (manual.txt.gz, game.d64.gz): unpacked outside the library, and what's inside is
    added under its own name (manual.txt) - always copied in, as an archive's files are."""
    import tempfile
    from . import archives
    from .categorize import guess_file
    with tempfile.TemporaryDirectory(prefix="retrorag-unpack-") as tmp:
        files = archives.extract(src, Path(tmp), log, k=archives.ONE)
        if not files:
            return None
        f = files[0]
        log(f"Unpacked {src.name} -> {f.name}")
        return add_file(lib, system, guess_file(f) if _auto(category) else category, f, copy=True,
                        group_path=group_path, kinds=kinds, log=log)


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
    category None (or BY_TYPE): each file under its own type's category. Compressed files (.txt.gz, .d64.gz) are
    unpacked; archives inside the folder (.zip, .tar.gz) are added as they are."""
    from . import archives
    from .categorize import guess_file
    unpack = lib.settings.extract_archives
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
            if unpack and archives.kind_by_name(f.name) == archives.ONE and archives.kind(f) == archives.ONE:
                doc_id = _add_compressed(lib, system, None if auto else category, f, gp, kinds, log)
            else:
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


_STRAY = re.compile(r"(?<!\S)[B-HJ-Zb-z](?!\S)")        # one-letter 'words' other than a / A / I
_WORD = re.compile(r"[A-Za-z]+")


def _spacing(txt: str) -> tuple[float, float, float]:
    """(share of tokens that are stray single letters, mean word length, share of words 16+ letters long)."""
    toks, words = txt.split(), _WORD.findall(txt)
    if not toks or not words:
        return 0.0, 0.0, 0.0
    return (len(_STRAY.findall(txt)) / len(toks), sum(map(len, words)) / len(words),
            sum(len(w) >= 16 for w in words) / len(words))


def pdf_page_text(page) -> str:
    """A PDF page's own text layer, in reading order.
    - A page stored rotated (a book scanned upside down and turned with /Rotate, a landscape table) is read the
      way it's shown: sorted on the unrotated page, its lines and words come out backwards or jumbled.
    - MuPDF adds a space wherever two letters sit apart, so letter-spaced text (Acrobat ClearScan, justified
      lines) comes out as 'c h e m i s t'. A page full of stray letters is read again without the added spaces,
      and that's kept when the words still stand apart (the file has its own spaces) - otherwise the first
      reading is (it has no spaces of its own, and the words would run together)."""
    import pymupdf
    if page.rotation:
        try:
            page.remove_rotation()           # in memory only: the file is never saved
        except Exception:
            pass
    txt = page.get_text("text", sort=True)
    stray = _spacing(txt)[0]
    if stray > 0.05 and len(txt.split()) >= 20:
        tight = page.get_text("text", sort=True, flags=pymupdf.TEXTFLAGS_TEXT | pymupdf.TEXT_INHIBIT_SPACES)
        t_stray, t_mean, t_long = _spacing(tight)
        if t_stray < stray / 3 and t_mean <= 7 and t_long <= 0.03:
            return tight
    return txt


OCR_MAKERS = ("scan", "capture", "ocr", "finereader", "abbyy", "omnipage", "readiris", "tesseract", "clearscan")


def _is_scan(page) -> bool:
    """A page that is a picture of paper: an image (or tiles of one) covering most of it."""
    area = abs(page.rect) or 1.0
    covered = 0.0
    try:
        for img in page.get_images(full=True):
            for r in page.get_image_rects(img[0]):
                covered += min(abs(r), area)
    except Exception:
        return False
    return covered > 0.6 * area


def _made_by(meta: dict, scans: bool) -> str:
    """The program that made a PDF's text: for scans, the one that did the OCR (ScanSnap, Acrobat Paper Capture,
    FineReader...) when the PDF says so."""
    names = [s.strip() for s in (meta.get("creator") or "", meta.get("producer") or "") if s and s.strip()]
    if scans:
        names = [s for s in names if any(k in s.lower() for k in OCR_MAKERS)] or names
    return names[0][:80] if names else ""


def layer_summary(doc, min_chars: int) -> dict:
    """Where an open PDF's own text is: {"text": pages with text, "scans": [pages that are scans carrying
    text - OCR'd by whatever made the PDF], "by": that program}."""
    text, scans = 0, []
    for i, page in enumerate(doc):
        if len(page.get_text("text").strip()) >= min_chars:
            text += 1
            if _is_scan(page):
                scans.append(i + 1)
    return {"text": text, "scans": scans, "by": _made_by(doc.metadata or {}, bool(scans))}


def text_layer_info(d: Document) -> dict:
    import json
    try:
        v = json.loads(d.text_layer or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def text_layer_state(lib: Library, d: Document) -> dict:
    """A PDF's own text for the document page: its info, plus "pending": the scanned pages still using the
    PDF's own OCR text (not read again by Tesseract since, nor edited by hand - "edited")."""
    info = text_layer_info(d)
    scans = info.get("scans") or []
    done: set[int] = set()
    if scans:
        from .ocr import load_ocr_json
        done = {int(k) for k in load_ocr_json(lib, d).get("pages", {}) if str(k).isdigit()}
    skip = done | set(info.get("edited") or [])
    return {**info, "pending": [p for p in scans if p not in skip], "redone": len([p for p in scans if p in done])}


def layer_pages_to_ocr(lib: Library, d: Document) -> tuple[list[int], list[int]]:
    """The scanned pages still using the PDF's own OCR text, for Tesseract to read: -> (pages, edited pages left
    alone because their text was changed in the editor)."""
    import pymupdf
    from .textutil import split_pages
    pending = text_layer_state(lib, d)["pending"]
    md, src = lib.derived_md_path(d), lib.abspath(d.path)
    bodies = {n: b for n, b in split_pages(md.read_text("utf-8")) if n is not None} if md.exists() else {}
    norm = lambda s: " ".join(s.split())
    pages, edited = [], []
    with pymupdf.open(src) as doc:
        for p in pending:
            if p in bodies and 1 <= p <= doc.page_count:
                page = doc[p - 1]
                old = norm(page.get_text("text", sort=True))      # read before or after the reading was fixed
                if norm(bodies[p]) not in (old, norm(pdf_page_text(page))):
                    edited.append(p)
                    continue
            pages.append(p)
    return pages, edited


def reread_text_layer(lib: Library, d: Document) -> int:
    """Read a PDF's text-layer pages again with pdf_page_text, for text read before it handled rotated and
    letter-spaced pages. Only pages still exactly as that first reading left them are replaced: OCR'd pages,
    proofreading and other edits are kept. -> the number of pages replaced."""
    import pymupdf
    from .textutil import join_pages, split_pages
    md, src = lib.derived_md_path(d), lib.abspath(d.path)
    if d.kind != "pdf" or not md.exists() or not src.exists():
        return 0
    pages = split_pages(md.read_text("utf-8"))
    norm = lambda s: " ".join(s.split())
    min_chars = lib.settings.pdf_min_text_chars
    changed = 0
    with pymupdf.open(src) as doc:
        for i, (n, body) in enumerate(pages):
            if n is None or not 1 <= n <= doc.page_count:
                continue
            page = doc[n - 1]
            old = page.get_text("text", sort=True)          # how text layers used to be read
            if len(old.strip()) < min_chars or norm(body) != norm(old):
                continue                                     # no text layer (OCR'd), or edited since
            new = pdf_page_text(page).strip()
            if norm(new) != norm(old):
                pages[i] = (n, new)
                changed += 1
    if changed:
        js = lib.ocr_json_path(d)
        mtime = md.stat().st_mtime
        md.write_text(join_pages(pages), "utf-8")
        if js.exists():          # not an edit: keep 'edited since OCR' (ocr.has_manual_edits) as it was
            os.utime(md, (mtime, mtime))
        lib.update_document(d.id, text_updated=now_iso())
    return changed


def _extract_pdf(lib: Library, d: Document, src: Path) -> dict:
    import json
    import pymupdf
    min_chars = lib.settings.pdf_min_text_chars
    parts, scanned, own, scans = [], 0, 0, []
    with pymupdf.open(src) as doc:
        n = doc.page_count
        for i, page in enumerate(doc):
            is_scan = _is_scan(page)
            txt = pdf_page_text(page).strip()
            if len(txt) < min_chars:
                scanned += 1
                txt = "<!-- no text layer: needs OCR -->"
            else:
                own += 1
                if is_scan:
                    scans.append(i + 1)
            parts.append(f"{page_marker(i + 1)}\n{txt}\n")
        layer = {"text": own, "scans": scans, "by": _made_by(doc.metadata or {}, bool(scans))}
    md = lib.derived_md_path(d)
    md.write_text(f"# {d.title}\n\n" + "\n".join(parts), "utf-8")
    status = "extracted" if scanned == 0 else ("partial" if scanned < n else "none")
    return {"text_path": lib.relpath(md), "text_status": status, "needs_ocr": int(scanned > 0), "pages": n,
            "text_layer": json.dumps(layer)}


def refresh_info_cards(lib: Library, d: Document) -> None:
    """Binary info cards embed title/notes; regenerate when those change."""
    if d.kind == "binary":
        extract(lib, d)
