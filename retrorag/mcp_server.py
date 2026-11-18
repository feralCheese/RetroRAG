"""MCP server exposing the library to Claude (Desktop / Code) and any other MCP client.

    python -m retrorag mcp --library "C:/path/to/library"            (stdio, for Claude Desktop / Claude Code)
    python -m retrorag mcp --library ... --http --port 8766          (streamable HTTP at http://host:port/mcp)
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from .library import Library
from . import search as S

INSTRUCTIONS = """RetroRAG is a reference library of documentation for retro computers and consoles: user guides,
programming references, schematics, pinouts, chip datasheets, service manuals, emulator source code, firmware/ROM
info cards and disk/tape image listings. Use `search_docs` first (optionally filtered by system or category), then
`read_document` with a page range to read more around a hit. Systems can have variants (e.g. ZX Spectrum ->
48K, 128K): filtering by a variant also returns the documents common to its whole family. Hits list `related` documents (pinouts, schematics,
datasheets, the source code implementing a chip, errata, newer revisions) - follow them with `get_related` /
`read_document` when the question spans several documents. Cite results as 'System / Document, page N'.
Text from scanned documents is OCR output and may contain recognition errors.

Disk and tape images (D64, D81, ADF, ST, MSA, IMG, SSD, DSD, ADL, TAP, TZX, MDR) can be opened and made:
`list_image_contents` shows an image's directory, `read_image_file` reads a file from it (BASIC programs come back
as listings), `add_image_files_to_library` adds its files as documents, and `create_image` builds a new image from
text, BASIC source, base64 data or library documents (see `list_media_formats` for each system's formats)."""


def build_server(library: str | Path):
    from mcp.server.mcpserver import MCPServer

    lib = Library(library)
    mcp = MCPServer(name="retrorag", title="RetroRAG retro computing library", instructions=INSTRUCTIONS)

    @mcp.tool()
    def list_systems() -> list[dict]:
        """List the retro systems in the library with their aliases and document counts."""
        lib.reload_settings()
        out = []
        for s in lib.systems():
            n = lib.conn().execute("SELECT category, COUNT(*) FROM documents WHERE system_id=? GROUP BY category",
                                   (s.id,)).fetchall()
            parent = lib.system(s.parent_id) if s.parent_id else None
            out.append({"name": s.name, "aliases": s.aliases, "manufacturer": s.manufacturer, "year": s.year,
                        "kind": s.kind, "cpu": s.cpu, "notes": s.notes,
                        "variant_of": parent.name if parent else None,
                        "variants": [c.name for c in lib.children(s.id)],
                        "documents_by_category": {r[0]: r[1] for r in n}})
        return out

    @mcp.tool()
    def search_docs(query: str, system: str | None = None, category: str | None = None,
                    limit: int = 8, mode: str = "hybrid") -> list[dict]:
        """Search the retro documentation library.

        Args:
            query: natural-language question or keywords, e.g. "SID filter cutoff register" or "$D011".
            system: optional system name or alias to restrict to (see list_systems), e.g. "C64".
            category: optional category, e.g. "Programming Reference", "Schematics", "Pinouts", "Chip Datasheets",
                      "User Guides & Manuals", "Emulator Source", "Firmware & ROMs", "Disk & Tape Images".
            limit: number of passages to return (1-30).
            mode: "hybrid" (default), "keyword" (exact terms, good for register names / addresses) or "semantic".
        """
        lib.reload_settings()
        res = S.search(lib, query, k=max(1, min(limit, 30)), system=system, category=category, mode=mode)
        return [{"citation": r.citation(), "doc_id": r.doc_id, "chunk_id": r.chunk_id, "page": r.page,
                 "section": r.heading, "category": r.category, "kind": r.kind, "text": r.text,
                 **({"related": r.related} if r.related else {})} for r in res]

    @mcp.tool()
    def list_documents(system: str | None = None, category: str | None = None, limit: int = 200) -> list[dict]:
        """List documents, optionally filtered by system and/or category."""
        names = {s.id: s.name for s in lib.systems()}
        if system:
            s = lib.find_system(system)
            if not s:
                return [{"error": f"No system matching '{system}'"}]
            docs = [d for sid in lib.system_scope(s.id) for d in lib.documents(sid, category)]
        else:
            docs = lib.documents(None, category)
        docs = docs[: max(1, min(limit, 2000))]
        return [{"doc_id": d.id, "title": d.title, "system": names.get(d.system_id), "category": d.category,
                 "kind": d.kind, "pages": d.pages, "file": Path(d.path).name, "folder": d.group_path,
                 "ocr": d.text_status in ("ocr", "reviewed"), "sha1": d.sha1 if d.kind == "binary" else None}
                for d in docs]

    @mcp.tool()
    def read_document(doc_id: int, page_from: int | None = None, page_to: int | None = None,
                      max_chars: int = 30000) -> str:
        """Read the text of a document (whole, or a page range for PDFs/scans). Use the doc_id from search results."""
        d = lib.document(doc_id)
        if not d:
            return f"No document with id {doc_id}"
        s = lib.system(d.system_id)
        head = f"# {d.title}\nSystem: {s.name if s else '?'} | Category: {d.category} | Pages: {d.pages or '-'}\n"
        from .relations import describe, related
        links = related(lib, doc_id)
        if links:
            head += "Related documents:\n" + describe(links) + "\n"
        head += "\n"
        return head + S.document_text(lib, doc_id, page_from, page_to, max_chars=max(1000, min(max_chars, 100000)))

    @mcp.tool()
    def get_related(doc_id: int) -> list[dict]:
        """Documents linked to this one: its pinout/schematic, datasheet, the source code that implements it,
        errata, newer or older revisions, chapters... Each entry says how it relates (from this document's view)."""
        from .relations import related
        if not lib.document(doc_id):
            return [{"error": f"No document with id {doc_id}"}]
        return [{k: v for k, v in l.items() if k not in ("rel_id", "origin")} for l in related(lib, doc_id)]

    # ------------------------------------------------------------------ disk / tape images
    from . import media, media_ops

    def _image_doc(doc_id: int):
        d = lib.document(doc_id)
        if not d:
            raise ValueError(f"No document with id {doc_id}")
        return d, media_ops.open_doc(lib, d)

    @mcp.tool()
    def list_media_formats(system: str | None = None) -> dict:
        """Disk and tape image formats RetroRAG can read and create, with their file types, name lengths,
        address fields and options. With a system name, also says which formats that system uses (chosen in
        the app, or suggested from its name) - use one of those when creating an image for it."""
        out = {"formats": [f.describe() for f in media.all_formats()]}
        if system:
            s = lib.find_system(system)
            if not s:
                return {"error": f"No system matching '{system}'"}
            ids, how = lib.system_formats(s.id)
            out.update(system=s.name, system_formats=ids, system_formats_are=how)
        return out

    @mcp.tool()
    def list_image_contents(doc_id: int) -> dict:
        """The directory of a disk or tape image in the library (doc_id from search_docs / list_documents):
        file names, types (PRG/SEQ, Program/Bytes, BASIC ...), sizes, load / exec addresses, disk name and free
        space."""
        try:
            d, img = _image_doc(doc_id)
        except (ValueError, media.MediaError) as e:
            return {"error": str(e)}
        return {"doc_id": d.id, "title": d.title, **media_ops.image_info(img)}

    @mcp.tool()
    def read_image_file(doc_id: int, name: str, encoding: str = "text", max_chars: int = 60000) -> dict:
        """Read one file from a disk or tape image.

        Args:
            doc_id: the image document.
            name: the file's name as list_image_contents shows it (folders joined with /).
            encoding: "text" (default) - BASIC programs come back as a listing, text files as text, anything
                      else as a hex dump; or "base64" for the exact bytes.
            max_chars: limit on the returned text.
        """
        try:
            d, img = _image_doc(doc_id)
            e = img.find(name)
        except (ValueError, media.MediaError) as ex:
            return {"error": str(ex)}
        info = media_ops.entry_info(img, e)
        if encoding == "base64":
            import base64
            data = img.read(e)[:4 << 20]
            return {**info, "encoding": "base64", "data": base64.b64encode(data).decode("ascii")}
        how, text = media.text_view(img, e, limit=max(1000, min(max_chars, 400000)))
        return {**info, "shown_as": how, "text": text[:max(1000, min(max_chars, 400000))]}

    @mcp.tool()
    def add_image_files_to_library(doc_id: int, names: list[str] | None = None) -> dict:
        """Add files from a disk/tape image to the library as their own documents (BASIC programs as listings,
        text as text), linked to the image. names: which files (default: all)."""
        d = lib.document(doc_id)
        if not d:
            return {"error": f"No document with id {doc_id}"}
        try:
            ids = media_ops.add_entries_to_library(lib, d, names or None, log=lambda m: None)
        except media.MediaError as e:
            return {"error": str(e)}
        _index(ids)
        return {"added": [{"doc_id": i, "title": lib.document(i).title} for i in ids]}

    def _index(ids):
        try:
            from .indexer import build_index
            build_index(lib, list(ids), log=lambda m: None)
        except Exception:
            pass                       # the app indexes it later

    @mcp.tool()
    def create_image(system: str, format: str, files: list[dict], label: str = "", title: str | None = None,
                     options: dict | None = None) -> dict:
        """Create a disk or tape image and add it to the library (category "Disk & Tape Images").

        Args:
            system: the system it's for (name or alias); see list_media_formats(system) for its formats.
            format: format id, e.g. "d64", "d81", "adf", "st", "msa", "img", "ssd", "dsd", "adl", "tap", "tzx", "mdr".
            files: one entry per file:
                {"name": "HELLO", "text": "10 PRINT \"HI\"\n20 GOTO 10", "basic": true}   - BASIC source, tokenised
                    for the machine (Commodore: write in CAPITALS; Spectrum: 48K keywords; BBC BASIC)
                {"name": "README", "text": "..."}            - a text file (converted to the machine's character set)
                {"name": "CODE", "base64": "...", "type": "Bytes", "load": "$8000"}   - exact bytes
                {"doc_id": 123}                             - a file already in the library
                Optional: "type" (see list_media_formats: PRG/SEQ/USR, Program/Bytes, BASIC ...), "load" / "exec"
                addresses ("$C000", "&1900", "0xC000" or a number), "autostart" (Spectrum BASIC line).
                Folders (ADF, ST, IMG, ADL): use "/" in the name, e.g. "s/startup-sequence".
            label: disk / tape name.
            title: document title in the library (default: the label).
            options: format options, e.g. {"size": "pc720"} for img, {"fs": "ffs", "bootable": true} for adf,
                     {"boot": 3} for ssd/dsd/adl (see list_media_formats).
        Returns the new document's id and where the image file is.
        """
        s = lib.find_system(system)
        if not s:
            return {"error": f"No system matching '{system}'"}
        try:
            fmt = media.get(format)
            nfs, sources = media_ops.files_from_specs(lib, fmt, files)
            doc_id = media_ops.create_image_doc(lib, s, fmt.id, nfs, label=label, title=title, options=options or {},
                                                source_doc_ids=sources, log=lambda m: None)
        except (media.MediaError, ValueError, TypeError) as e:
            return {"error": str(e)}
        _index([doc_id])
        d = lib.document(doc_id)
        img = media_ops.open_doc(lib, d)
        return {"doc_id": doc_id, "title": d.title, "system": s.name, "path": str(lib.abspath(d.path)),
                **media_ops.image_info(img)}

    @mcp.tool()
    def get_context(chunk_id: int, before: int = 1, after: int = 1) -> str:
        """Return a search hit together with the passages immediately before/after it."""
        return S.neighbours(lib, chunk_id, max(0, min(before, 5)), max(0, min(after, 5)))

    return mcp


def main(library: str, http: bool = False, host: str | None = None, port: int | None = None) -> None:
    # stdout is the MCP channel in stdio mode: log to stderr only
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    if http:
        import uvicorn
        from .api_server import BearerAuth, mcp_http_app
        st = Library(library).settings
        host, port = host or st.api_host, port or st.mcp_port
        app = mcp_http_app(library, host)
        if st.api_token:
            app = BearerAuth(app, st.api_token)
        print(f"RetroRAG MCP (streamable HTTP) on http://{host}:{port}/mcp", file=sys.stderr)
        uvicorn.run(app, host=host, port=port, log_level="warning")
    else:
        build_server(library).run("stdio")
