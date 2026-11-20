"""MCP server exposing the library to Claude (Desktop / Code) and any other MCP client.

    python -m retrorag mcp --library "C:/path/to/library"            (stdio, for Claude Desktop / Claude Code)
    python -m retrorag mcp --library ... --http --port 8766          (streamable HTTP at http://host:port/mcp)
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from mcp.server.mcpserver import Image
from mcp_types import TextContent

from .library import Library
from . import search as S

INSTRUCTIONS = """RetroRAG is a reference library of documentation for retro computers and consoles: user guides,
programming references, schematics, pinouts, chip datasheets, service manuals, source code (emulators, system software), firmware/ROM
info cards and disk/tape image listings. Use `search_docs` first (optionally filtered by system or category), then
`read_document` with a page range to read more around a hit. Systems can have variants (e.g. ZX Spectrum ->
48K, 128K): filtering by a variant also returns the documents common to its whole family. Hits list `related` documents (pinouts, schematics,
datasheets, the source code implementing a chip, errata, newer revisions) - follow them with `get_related` /
`read_document` when the question spans several documents. Cite results as 'System / Document, page N'.
Text from scanned documents is OCR output and may contain recognition errors.
Documents of kind "pdf" or "image" can be looked at with `view_page` (if you can see images): use it for what OCR
can't give - schematics, pinout diagrams, photos of boards (chip markings, jumpers, switches), tables and layouts -
or to check doubtful OCR text. Zoom in with `region` to read small print. The "Photos" category holds the owner's
photos of machines and boards; they have no OCR text, so look at them.

Peripherals and expansions (disk drives, second processors, accelerator / RAM / IDE boards...) are systems of
their own, listed with the machines they work with. Searching a machine also searches its peripherals; those hits
carry `peripheral_for`, and their CPU, OS and memory map are the peripheral's own, not the host's (a 1541 drive's
6502 is not the C64's 6510). Use include_peripherals=false to search the machine alone.
"My Notes" documents are the library owner's own first-hand findings - give them weight, and say when an answer
comes from them. Save a new note with `add_note` only when the user asks you to note something down.
`list_systems` also gives each system's links (web pages, docs sites, GitHub / GitLab repositories the owner saved).
Copied ones are already in the library and searchable. A link with status "gone" is no longer online.

Disk and tape images (D64, D81, ADF, ST, MSA, IMG, SSD, DSD, ADL, TAP, TZX, MDR):
- An image in the library is given by its doc_id; one the user has uploaded by its `upload` reference.
- Files the user attached in the chat can NOT be passed to these tools directly. Call `get_upload_link`, give the
  user the link (they drop the file on that page in their browser; zips are unpacked), then `list_uploads`.
  If you can run code that has the file, you can PUT it to the link yourself. Small files can go through
  `upload_file` (base64) instead.
- `list_image_contents` and `read_image_file` show what's on an image (BASIC programs come back as listings).
- `extract_image_files` gives download links for the files on an image (one zip, plus each file), without adding
  anything to the library. `add_file_to_library` adds an uploaded file; `add_image_files_to_library` adds the files
  on a library image as documents.
- `create_image` builds a new image from BASIC source, text, base64 data, uploads or library documents, and either
  adds it to the library or just returns a download link (add_to_library=false). `list_media_formats` shows each
  system's formats.
- Give the user the download links you get back."""


def build_server(library: str | Path):
    from mcp.server.mcpserver import MCPServer

    lib = Library(library)
    mcp = MCPServer(name="retrorag", title="RetroRAG retro computing library", instructions=INSTRUCTIONS)

    @mcp.tool()
    def list_systems() -> list[dict]:
        """List the retro systems in the library: aliases, document counts, variants, the peripherals and
        expansions that work with each (and, for a peripheral, the machines it works with), and saved links."""
        from .webcopy import describe_link
        lib.reload_settings()
        out = []
        for s in lib.systems():
            n = lib.conn().execute("SELECT category, COUNT(*) FROM documents WHERE system_id=? GROUP BY category",
                                   (s.id,)).fetchall()
            parent = lib.system(s.parent_id) if s.parent_id else None
            row = {"name": s.name, "aliases": s.aliases, "manufacturer": s.manufacturer, "year": s.year,
                   "kind": s.kind, "cpu": s.cpu, "notes": s.notes,
                   "variant_of": parent.name if parent else None,
                   "variants": [c.name for c in lib.children(s.id)],
                   "documents_by_category": {r[0]: r[1] for r in n}}
            hosts, periph = lib.hosts(s.id), lib.peripherals(s.id)
            if hosts:
                row["peripheral_for"] = [h.name for h in hosts]
            if periph:
                row["peripherals_and_expansions"] = [p.name for p in periph]
            links = [describe_link(lib, l) for l in lib.links(s.id)]
            if links:
                row["links"] = [{k: v for k, v in l.items() if v not in (None, "", 0) and k not in
                                 ("link_id", "status_detail", "next_check")} for l in links]
            out.append(row)
        return out

    @mcp.tool()
    def search_docs(query: str, system: str | None = None, category: str | None = None,
                    limit: int = 8, mode: str = "hybrid", include_peripherals: bool = True) -> list[dict]:
        """Search the retro documentation library.

        Args:
            query: natural-language question or keywords, e.g. "SID filter cutoff register" or "$D011".
            system: optional system name or alias to restrict to (see list_systems), e.g. "C64".
            category: optional category, e.g. "Programming Reference", "Schematics", "Pinouts", "Chip Datasheets",
                      "User Guides & Manuals", "Service & Repair", "Photos", "Source", "Firmware & ROMs",
                      "Disk & Tape Images", "My Notes".
            limit: number of passages to return (1-30).
            mode: "hybrid" (default), "keyword" (exact terms, good for register names / addresses) or "semantic".
            include_peripherals: with a system, also search the peripherals and expansions that work with it
                      (default true; their hits say `peripheral_for`).
        """
        lib.reload_settings()
        res = S.search(lib, query, k=max(1, min(limit, 30)), system=system, category=category, mode=mode,
                       include_peripherals=include_peripherals)
        return [r.to_ai() for r in res]

    @mcp.tool()
    def list_documents(system: str | None = None, category: str | None = None, limit: int = 200,
                       include_peripherals: bool = True) -> list[dict]:
        """List documents, optionally filtered by system (with its family and, by default, its peripherals and
        expansions) and/or category - e.g. category "My Notes" for the owner's own notes."""
        names = {s.id: s.name for s in lib.systems()}
        if system:
            s = lib.find_system(system)
            if not s:
                return [{"error": f"No system matching '{system}'"}]
            docs = [d for sid in lib.system_scope(s.id, peripherals=include_peripherals)
                    for d in lib.documents(sid, category)]
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
    def view_page(doc_id: int, page: int = 1, region: list[float] | None = None) -> list[TextContent | Image]:
        """Look at a page of a scanned document or PDF, or at a picture (a photo of a board or machine, a
        schematic, a pinout diagram), as an image. Use it for what OCR text can't give: drawings, chip markings,
        jumper and switch settings, connectors, tables and page layout - or to check doubtful OCR text.

        Args:
            doc_id: a document of kind "pdf" or "image" (from search_docs or list_documents).
            page: page number (1 = first; a single picture has just page 1).
            region: zoom into part of the page: [left, top, right, bottom] as fractions of the page (0-1), e.g.
                    [0, 0, 0.5, 0.5] = top-left quarter. Shown sharper - use it to read small print.
        Each image costs about 1,500 tokens of context, so look only at the pages you need."""
        from .pictures import PictureError, caption, render
        d = lib.document(doc_id)
        if not d:
            return [TextContent(type="text", text=f"No document with id {doc_id}")]
        try:
            data, info = render(lib, d, page, region)
        except PictureError as e:
            return [TextContent(type="text", text=str(e))]
        except Exception as e:                       # unreadable file: say so rather than fail the call
            return [TextContent(type="text", text=f"Couldn't show page {page} of '{d.title}': {e}")]
        return [TextContent(type="text", text=caption(info)), Image(data=data, format="jpeg")]

    @mcp.tool()
    def get_related(doc_id: int) -> list[dict]:
        """Documents linked to this one: its pinout/schematic, datasheet, the source code that implements it,
        errata, newer or older revisions, chapters... Each entry says how it relates (from this document's view)."""
        from .relations import related
        if not lib.document(doc_id):
            return [{"error": f"No document with id {doc_id}"}]
        return [{k: v for k, v in l.items() if k not in ("rel_id", "origin")} for l in related(lib, doc_id)]

    # ------------------------------------------------------------------ disk / tape images, uploads, downloads
    from . import media, media_ops, transfer

    def _fail(e) -> dict:
        return {"error": str(e)}

    def _index(ids):
        try:
            from .indexer import build_index
            build_index(lib, list(ids), log=lambda m: None)
        except Exception:
            pass                       # the app indexes it later

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
    def get_upload_link() -> dict:
        """Get a link the user can open in a browser to send files to RetroRAG (disk/tape images, zips, documents).
        Use this whenever the user has a file in the chat that a RetroRAG tool should work on: tools can't read chat
        attachments. Give the user the link, ask them to drop the file there and tell you when it's done, then call
        list_uploads with the upload_id."""
        try:
            return transfer.upload_link(lib)
        except transfer.TransferError as e:
            return _fail(e)

    @mcp.tool()
    def upload_file(filename: str, data_base64: str, upload_id: str | None = None) -> dict:
        """Send a small file (up to a few hundred KB) straight to RetroRAG as base64, instead of an upload link.
        Zips are unpacked. Returns the `upload` reference(s) to use with the other tools."""
        import base64
        import binascii
        try:
            tid = upload_id or transfer.new_area(lib, "upload")[0]
            refs = transfer.save_file(lib, tid, filename, base64.b64decode(data_base64, validate=False))
        except (transfer.TransferError, binascii.Error, ValueError) as e:
            return _fail(e)
        return {"upload_id": tid, "uploads": refs}

    @mcp.tool()
    def list_uploads(upload_id: str) -> dict:
        """The files the user has sent with an upload link (zips already unpacked). Each has an `upload` reference
        for list_image_contents, read_image_file, extract_image_files, add_file_to_library and create_image;
        disk and tape images show their format."""
        try:
            files = transfer.list_area(lib, upload_id)
        except transfer.TransferError as e:
            return _fail(e)
        out = {"upload_id": upload_id, "files": files}
        if not files:
            out["note"] = "Nothing has arrived yet - ask the user to drop the file on the upload page and try again."
        return out

    @mcp.tool()
    def add_file_to_library(upload: str, system: str, category: str | None = None, title: str | None = None) -> dict:
        """Add an uploaded file to the library, under a system (name or alias). upload: a reference from
        list_uploads, or just the upload_id to add every file in it. Without a category, each file's is guessed
        from its type (disk and tape images -> "Disk & Tape Images", photos -> "Photos", manuals -> "User Guides &
        Manuals", ...). Returns the new doc_ids (then e.g. list_image_contents(doc_id))."""
        s = lib.find_system(system)
        if not s:
            return {"error": f"No system matching '{system}' (see list_systems)"}
        try:
            ids = media_ops.add_upload_to_library(lib, upload, s, category, title, log=lambda m: None)
        except (transfer.TransferError, media.MediaError, OSError) as e:
            return _fail(e)
        _index(ids)
        return {"added": [{"doc_id": i, "title": lib.document(i).title, "category": lib.document(i).category}
                          for i in ids]}

    @mcp.tool()
    def list_image_contents(doc_id: int | None = None, upload: str | None = None) -> dict:
        """The directory of a disk or tape image: file names, types (PRG/SEQ, Program/Bytes, BASIC ...), sizes,
        load / exec addresses, disk name and free space. Give doc_id for an image in the library, or upload for one
        the user sent (from list_uploads)."""
        try:
            img, title, d = media_ops.open_source(lib, doc_id, upload)
        except (transfer.TransferError, media.MediaError, OSError) as e:
            return _fail(e)
        return {**({"doc_id": d.id} if d else {"upload": upload}), "title": title, **media_ops.image_info(img)}

    @mcp.tool()
    def read_image_file(name: str, doc_id: int | None = None, upload: str | None = None, encoding: str = "text",
                        max_chars: int = 60000) -> dict:
        """Read one file from a disk or tape image (doc_id in the library, or upload from list_uploads).

        Args:
            name: the file's name as list_image_contents shows it (folders joined with /).
            encoding: "text" (default) - BASIC programs come back as a listing, text files as text, anything
                      else as a hex dump; or "base64" for the exact bytes.
            max_chars: limit on the returned text.
        """
        try:
            img, _, _ = media_ops.open_source(lib, doc_id, upload)
            e = img.find(name)
        except (transfer.TransferError, media.MediaError, OSError) as ex:
            return _fail(ex)
        info = media_ops.entry_info(img, e)
        if encoding == "base64":
            import base64
            data = img.read(e)[:4 << 20]
            return {**info, "encoding": "base64", "data": base64.b64encode(data).decode("ascii")}
        limit = max(1000, min(max_chars, 400000))
        how, text = media.text_view(img, e, limit=limit)
        return {**info, "shown_as": how, "text": text[:limit]}

    @mcp.tool()
    def extract_image_files(doc_id: int | None = None, upload: str | None = None,
                            names: list[str] | None = None) -> dict:
        """Extract the files on a disk or tape image (doc_id in the library, or upload from list_uploads) without
        adding anything to the library. Returns a download link for a zip of them all and for each file - give
        these to the user. names: which files (default: all). Files are saved as stored (PRG files keep their load
        address, Acorn files get .inf files, Spectrum BASIC programs come as one-file .tap tapes)."""
        try:
            img, title, _ = media_ops.open_source(lib, doc_id, upload)
            return media_ops.extract_to_area(lib, img, names or None, title)
        except (transfer.TransferError, media.MediaError, OSError) as e:
            return _fail(e)

    @mcp.tool()
    def add_image_files_to_library(doc_id: int, names: list[str] | None = None) -> dict:
        """Add the files on a library disk/tape image to the library as their own documents (BASIC programs as
        listings, text as text), linked to the image. names: which files (default: all). For an image the user
        uploaded, add the image first with add_file_to_library."""
        d = lib.document(doc_id)
        if not d:
            return {"error": f"No document with id {doc_id}"}
        try:
            ids = media_ops.add_entries_to_library(lib, d, names or None, log=lambda m: None)
        except media.MediaError as e:
            return _fail(e)
        _index(ids)
        return {"added": [{"doc_id": i, "title": lib.document(i).title} for i in ids]}

    @mcp.tool()
    def create_image(format: str, files: list[dict], system: str | None = None, label: str = "",
                     title: str | None = None, options: dict | None = None, add_to_library: bool = True) -> dict:
        """Create a disk or tape image.

        Args:
            format: format id, e.g. "d64", "d81", "adf", "st", "msa", "img", "ssd", "dsd", "adl", "tap", "tzx", "mdr"
                    (list_media_formats(system) shows which a system uses).
            files: one entry per file:
                {"name": "HELLO", "text": "10 PRINT \"HI\"\n20 GOTO 10", "basic": true}   - BASIC source, tokenised
                    for the machine (Commodore: write in CAPITALS; Spectrum: 48K keywords; BBC BASIC)
                {"name": "README", "text": "..."}            - a text file (converted to the machine's character set)
                {"name": "CODE", "base64": "...", "type": "Bytes", "load": "$8000"}   - exact bytes
                {"upload": "<upload_id>/<file>"}             - a file the user uploaded (list_uploads)
                {"doc_id": 123}                             - a file already in the library
                Optional: "type" (PRG/SEQ/USR, Program/Bytes, BASIC ... see list_media_formats), "load" / "exec"
                addresses ("$C000", "&1900", "0xC000" or a number), "autostart" (Spectrum BASIC line).
                Folders (ADF, ST, IMG, ADL): use "/" in the name, e.g. "s/startup-sequence".
            system: the system it's for (name or alias). Needed when add_to_library is true.
            label: disk / tape name.
            title: document title / file name (default: the label).
            options: format options, e.g. {"size": "pc720"} for img, {"fs": "ffs", "bootable": true} for adf,
                     {"boot": 3} for ssd/dsd/adl (see list_media_formats).
            add_to_library: true (default) adds it to the library ("Disk & Tape Images"); false only makes it and
                     returns a download link for the user.
        """
        s = lib.find_system(system) if system else None
        if system and not s:
            return {"error": f"No system matching '{system}'"}
        if add_to_library and not s:
            return {"error": "Say which system it's for (system=...), or set add_to_library=false to just get a "
                             "download link"}
        try:
            fmt = media.get(format)
            nfs, sources = media_ops.files_from_specs(lib, fmt, files)
            if not add_to_library:
                return media_ops.image_to_area(lib, fmt.id, nfs, label=label, title=title, options=options or {})
            doc_id = media_ops.create_image_doc(lib, s, fmt.id, nfs, label=label, title=title, options=options or {},
                                                source_doc_ids=sources, log=lambda m: None)
        except (media.MediaError, transfer.TransferError, ValueError, TypeError, OSError) as e:
            return _fail(e)
        _index([doc_id])
        d = lib.document(doc_id)
        img = media_ops.open_doc(lib, d)
        return {"doc_id": doc_id, "title": d.title, "system": s.name, "path_on_retrorag_pc": str(lib.abspath(d.path)),
                **media_ops.image_info(img)}

    @mcp.tool()
    def add_note(system: str, title: str, text: str, tags: str = "") -> dict:
        """Save a note in the library owner's "My Notes" for a system or peripheral - a finding, a fix that worked,
        settings, a summary of what you worked out together. Only when the user asks you to note or remember
        something. text: Markdown. tags: comma separated. The note is searchable straight away."""
        from .notes import AI_SOURCE, add_note as _add
        s = lib.find_system(system)
        if not s:
            return {"error": f"No system matching '{system}' (see list_systems)"}
        if not (text or "").strip():
            return {"error": "The note is empty"}
        try:
            doc_id = _add(lib, s, title, text, tags=tags, source=AI_SOURCE)
        except (OSError, ValueError) as e:
            return _fail(e)
        _index([doc_id])
        return {"doc_id": doc_id, "system": s.name, "title": lib.document(doc_id).title,
                "saved_as": str(lib.abspath(lib.document(doc_id).path))}

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
