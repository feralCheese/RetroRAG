"""The user's own notes and findings about a system or peripheral ("My Notes").

A note is an ordinary Markdown document in the system's "My Notes" category (systems/<slug>/my-notes/<title>.md),
so it's searchable, exported and given to the AI like everything else - and the document header tells the AI it's
the library owner's own first-hand finding.
"""
from __future__ import annotations

from .config import NOTES_CATEGORY
from .library import Document, Library, System, now_iso, safe_filename, slugify

AI_SOURCE = "Written by an AI in a chat (MCP)"


def is_note(d: Document | None) -> bool:
    return bool(d) and d.category == NOTES_CATEGORY and d.kind == "text" and d.ext in (".md", ".txt")


def add_note(lib: Library, system: System, title: str, text: str, tags: str = "", source: str = "") -> int:
    from .ingest import _unique_dest, add_file
    import hashlib
    title = (title or "").strip() or "Note"
    body = _body(text)
    same = lib.conn().execute("SELECT id FROM documents WHERE system_id=? AND sha1=?",
                              (system.id, hashlib.sha1(body.encode("utf-8")).hexdigest())).fetchone()
    if same:            # word for word the same as something it already has
        return same["id"]
    folder = lib.system_dir(system) / slugify(NOTES_CATEGORY)
    folder.mkdir(parents=True, exist_ok=True)
    path = _unique_dest(folder / (safe_filename(title)[:80] + ".md"))
    path.write_bytes(body.encode("utf-8"))
    doc_id = add_file(lib, system, NOTES_CATEGORY, path, copy=True, title=title, log=lambda m: None)
    if not doc_id:
        path.unlink(missing_ok=True)
        raise ValueError("Couldn't save the note")
    upd = {}
    if tags:
        upd["tags"] = tags
    if source:
        upd["source_url"] = source
    if upd:
        lib.update_document(doc_id, **upd)
    return doc_id


def update_note(lib: Library, doc_id: int, title: str, text: str, tags: str | None = None,
                system_id: int | None = None) -> None:
    from . import binaries
    d = lib.document(doc_id)
    if not d:
        raise ValueError("That note no longer exists")
    p = lib.abspath(d.path)
    p.write_bytes(_body(text).encode("utf-8"))
    h = binaries.file_hashes(p)
    upd = {"title": (title or "").strip() or d.title, "size": p.stat().st_size, "sha1": h["sha1"], "md5": h["md5"],
           "crc32": h["crc32"], "text_updated": now_iso()}
    if tags is not None:
        upd["tags"] = tags
    lib.update_document(doc_id, **upd)
    if system_id and system_id != d.system_id:
        lib.move_documents([doc_id], system_id, NOTES_CATEGORY, log=lambda m: None)


def read_note(lib: Library, d: Document) -> str:
    return lib.read_text(d)


def _body(text: str) -> str:
    return (text or "").replace("\r\n", "\n").rstrip() + "\n"
