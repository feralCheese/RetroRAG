"""RetroRAG command line.

    python -m retrorag                         start the Windows app (GUI)
    python -m retrorag mcp   [--library L]     MCP server on stdio (Claude Desktop / Claude Code)
    python -m retrorag mcp   --http            MCP server on streamable HTTP (Unsloth Studio, LM Studio, ...)
    python -m retrorag api   [--library L]     REST API + MCP HTTP (no GUI)
    python -m retrorag index [--all]           build/refresh the index
    python -m retrorag ocr   [--doc ID]        OCR documents that need it
    python -m retrorag search "query" [--system C64] [--category ...]
    python -m retrorag add   --system C64 --category "Schematics" FILE_OR_FOLDER...
    python -m retrorag export --markdown OUT_DIR | --jsonl OUT_FILE
    python -m retrorag stats
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _lib_path(arg: str | None) -> Path:
    from .config import AppSettings, resolve_library_path
    return Path(arg) if arg else resolve_library_path(AppSettings.load().library_path)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="retrorag", description="RetroRAG - retro computing documentation RAG")
    p.add_argument("--library", "-l", help="library folder (default: last used / configured)")
    common = argparse.ArgumentParser(add_help=False)   # so --library also works after the sub-command
    common.add_argument("--library", "-l", default=argparse.SUPPRESS, help="library folder")
    sub = p.add_subparsers(dest="cmd")
    _add = sub.add_parser
    sub.add_parser = lambda *a, **k: _add(*a, parents=[common], **k)
    sub.add_parser("gui")
    m = sub.add_parser("mcp"); m.add_argument("--http", action="store_true"); m.add_argument("--host"); m.add_argument("--port", type=int)
    a = sub.add_parser("api"); a.add_argument("--host"); a.add_argument("--port", type=int); a.add_argument("--no-mcp", action="store_true")
    i = sub.add_parser("index"); i.add_argument("--all", action="store_true")
    o = sub.add_parser("ocr"); o.add_argument("--doc", type=int, action="append"); o.add_argument("--force", action="store_true")
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("--system"); s.add_argument("--category"); s.add_argument("-k", type=int, default=5); s.add_argument("--mode", default="hybrid")
    ad = sub.add_parser("add"); ad.add_argument("--system", required=True); ad.add_argument("--category", default="Other"); ad.add_argument("--link", action="store_true", help="don't copy, reference in place"); ad.add_argument("paths", nargs="+")
    e = sub.add_parser("export"); e.add_argument("--markdown"); e.add_argument("--jsonl"); e.add_argument("--embeddings", action="store_true")
    sub.add_parser("stats")
    dm = sub.add_parser("download-model", help="fetch the built-in embedding model now (e.g. before going offline)")
    dm.add_argument("--to", help="folder to put it in (e.g. dist/RetroRAG/models/bge-small-en-v1.5)")
    cfg = sub.add_parser("settings", help="show or change library settings, e.g. settings api_host=0.0.0.0")
    cfg.add_argument("pairs", nargs="*", metavar="KEY=VALUE")
    args = p.parse_args(argv)

    if args.cmd in (None, "gui"):
        from .gui.app import run
        return run(args.library)

    if args.cmd == "download-model":
        from .embeddings import BUILTIN_DEFAULT, ensure_model
        print(ensure_model(BUILTIN_DEFAULT, target=args.to))
        return 0
    lib_path = _lib_path(args.library)
    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main(str(lib_path), http=args.http, host=args.host, port=args.port)
        return 0

    for stream in (sys.stdout, sys.stderr):   # Windows consoles default to cp1252
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    from .library import Library
    lib = Library(lib_path)
    if args.cmd == "api":
        import time
        from .api_server import start_servers
        st = lib.settings
        host, port = args.host or st.api_host, args.port or st.api_port
        servers = start_servers(lib_path, host, port, None if args.no_mcp else st.mcp_port, st.api_token)
        print(f"REST API  http://{host}:{port}/docs")
        if not args.no_mcp:
            print(f"MCP HTTP  http://{host}:{st.mcp_port}/mcp")
        print("Ctrl+C to stop.")
        try:
            while all(s.running for s in servers):
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        for s in servers:
            s.stop()
        return 0
    if args.cmd == "index":
        from .indexer import build_index
        build_index(lib, rebuild_all=args.all)
        return 0
    if args.cmd == "ocr":
        from .ocr import ocr_document
        docs = [lib.document(d) for d in args.doc] if args.doc else lib.documents(where="needs_ocr=1")
        for d in docs:
            if d:
                ocr_document(lib, d, force=args.force)
        return 0
    if args.cmd == "search":
        from .search import search
        for r in search(lib, args.query, k=args.k, system=args.system, category=args.category, mode=args.mode):
            print(f"\n=== {r.citation()}  [{r.matched}, score {r.score}]  doc {r.doc_id}")
            print(r.text[:800])
        return 0
    if args.cmd == "add":
        from .ingest import add_files, add_folder
        sysm = lib.find_system(args.system) or lib.add_system(args.system)
        for path in map(Path, args.paths):
            if path.is_dir():
                add_folder(lib, sysm, args.category, path, copy=not args.link)
            else:
                add_files(lib, sysm, args.category, [path], copy=not args.link)
        return 0
    if args.cmd == "export":
        from .export import export_jsonl, export_markdown
        if args.markdown:
            export_markdown(lib, Path(args.markdown))
        if args.jsonl:
            export_jsonl(lib, Path(args.jsonl), args.embeddings)
        return 0
    if args.cmd == "settings":
        from dataclasses import fields
        st, types = lib.settings, {f.name: f.type for f in fields(lib.settings)}
        for pair in args.pairs:
            key, _, val = pair.partition("=")
            if key not in types:
                print(f"Unknown setting '{key}'. Known: {', '.join(types)}")
                return 2
            cur = getattr(st, key)
            if isinstance(cur, bool):
                val = val.lower() in ("1", "true", "yes", "on")
            elif isinstance(cur, int):
                val = int(val)
            setattr(st, key, val)
        if args.pairs:
            st.save(lib.root)
            print("Saved.")
        for f in fields(st):
            v = getattr(st, f.name)
            print(f"{f.name:>26} = {'***' if f.name in ('embed_api_key', 'api_token') and v else v}")
        return 0
    if args.cmd == "stats":
        for k, v in lib.stats().items():
            print(f"{k:>16}: {v}")
        print(f"{'library':>16}: {lib.root}")
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
