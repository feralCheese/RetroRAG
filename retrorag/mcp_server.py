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
Text from scanned documents is OCR output and may contain recognition errors."""


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
