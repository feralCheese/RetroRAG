"""RetroRAG command line.

    python -m retrorag                         start the Windows app (GUI)
    python -m retrorag mcp   [--library L]     MCP server on stdio (Claude Desktop / Claude Code)
    python -m retrorag mcp   --http            MCP server on streamable HTTP (Unsloth Studio, LM Studio, ...)
    python -m retrorag api   [--library L]     REST API + MCP HTTP (no GUI)
    python -m retrorag index [--all]           build/refresh the index
    python -m retrorag ocr   [--doc ID]        OCR documents that need it
    python -m retrorag search "query" [--system C64] [--category ...]
    python -m retrorag add   --system C64 [--category "Schematics"] FILE_OR_FOLDER...   (category: guessed per file)
    python -m retrorag export --markdown OUT_DIR | --jsonl OUT_FILE
    python -m retrorag stats
    python -m retrorag image formats | list IMG | read IMG NAME | extract IMG [NAME...] --to DIR
    python -m retrorag image create FORMAT OUT FILE... [--label L] [--basic NAME=SOURCE.bas] [--opt key=value]
    python -m retrorag links [list | due | refresh [--all] | check ID...]   saved links and web copies
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _lib_path(arg: str | None) -> Path:
    from .config import AppSettings, resolve_library_path
    return Path(arg) if arg else resolve_library_path(AppSettings.load().library_path)


def _image_cmd(args) -> int:
    from . import media, media_ops
    a = args.args
    try:
        if args.action == "formats":
            for f in media.all_formats():
                print(f"{f.id:5} {f.name:38} {' '.join(f.exts):18} {'read+create' if f.writable else 'read'}"
                      + (f"   types: {', '.join(f.kinds)}" if len(f.kinds) > 1 else ""))
                for o in f.options:
                    print(f"{'':6}option {o.key}: " + ", ".join(str(v) for _, v in o.choices))
            return 0
        if args.action == "create":
            if len(a) < 2:
                print("usage: image create FORMAT OUT [FILE...] [--basic NAME=FILE]", file=sys.stderr)
                return 2
            fmt = media.get(a[0])
            files = [media.prepare(fmt, Path(f).name, Path(f).read_bytes()) for f in a[2:]]
            for spec in args.basic:
                name, _, src = spec.partition("=")
                files.append(media_ops.new_file(fmt, name, text=Path(src).read_text("utf-8", "replace"), basic=True))
            opts = {}
            for kv in args.opt:
                k, _, v = kv.partition("=")
                opts[k] = int(v) if v.isdigit() else {"true": True, "false": False}.get(v.lower(), v)
            if "bootable" in opts:
                opts["bootable"] = bool(opts["bootable"])
            raw = fmt.create(files, args.label, **opts)
            Path(a[1]).write_bytes(raw)
            print(f"Wrote {a[1]}: {fmt.name}, {len(files)} file(s), {len(raw):,} bytes")
            return 0
        if not a:
            print("usage: image list|read|extract IMAGE ...", file=sys.stderr)
            return 2
        img = media.open_image(a[0])
        if args.action == "list":
            print(f"{img.fmt.name}" + (f': "{img.label}"' if img.label else ""))
            for line in img.info:
                print("  " + line)
            for e in img.entries():
                print(f"  {e.name + ('/' if e.is_dir else ''):32} {'' if e.is_dir else e.kind:10} "
                      f"{'' if e.is_dir else e.size:>8}  {'' if e.is_dir else e.describe(img.fmt.hex_prefix, kind=False)}")
        elif args.action == "read":
            for n in a[1:]:
                how, text = media.text_view(img, img.find(n))
                print(f"--- {n} ({how})")
                print(text)
        elif args.action == "extract":
            out = media_ops.extract_to_folder(img, a[1:] or None, Path(args.to or "."))
            print(f"Extracted {len(out)} file(s) to {Path(args.to or '.').resolve()}")
        return 0
    except (media.MediaError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


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
    ad = sub.add_parser("add"); ad.add_argument("--system", required=True); ad.add_argument("--category", default=None, help="default: guessed for each file from its type"); ad.add_argument("--link", action="store_true", help="don't copy, reference in place"); ad.add_argument("paths", nargs="+")
    e = sub.add_parser("export"); e.add_argument("--markdown"); e.add_argument("--jsonl"); e.add_argument("--embeddings", action="store_true")
    sub.add_parser("stats")
    dm = sub.add_parser("download-model", help="fetch the built-in embedding model now (e.g. before going offline)")
    dm.add_argument("--to", help="folder to put it in (e.g. dist/RetroRAG/models/bge-small-en-v1.5)")
    cfg = sub.add_parser("settings", help="show or change library settings, e.g. settings api_host=0.0.0.0")
    cfg.add_argument("pairs", nargs="*", metavar="KEY=VALUE")
    im = sub.add_parser("image", help="disk / tape images: formats, list, read, extract, create")
    im.add_argument("action", choices=["formats", "list", "read", "extract", "create"])
    im.add_argument("args", nargs="*", help="list/read/extract: IMAGE [NAME...];  create: FORMAT OUT [FILE...]")
    im.add_argument("--to", help="extract: folder to write to")
    im.add_argument("--label", default="", help="create: disk / tape name")
    im.add_argument("--basic", action="append", default=[], metavar="NAME=FILE",
                    help="create: tokenise a BASIC text file as program NAME")
    im.add_argument("--opt", action="append", default=[], metavar="KEY=VALUE",
                    help="create: format option, e.g. size=pc720, fs=ffs, bootable=1, boot=3")
    ln = sub.add_parser("links", help="saved links and copies of web pages / sites / GitHub repositories")
    ln.add_argument("action", nargs="?", default="list", choices=["list", "due", "refresh", "check"])
    ln.add_argument("ids", nargs="*", type=int, help="check: link ids")
    ln.add_argument("--all", action="store_true", help="refresh: every link, not just the ones due")
    args = p.parse_args(argv)

    if args.cmd == "image":
        return _image_cmd(args)

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
    if args.cmd == "links":
        from .webcopy import STATUS_WORDS, run_links
        from .library import LINK_MODES
        names = {s.id: s.name for s in lib.systems()}
        if args.action in ("list", "due"):
            for l in (lib.links_due() if args.action == "due" else lib.links()):
                due = l.next_due()
                print(f"{l.id:4}  {names.get(l.system_id, '?')[:22]:22}  {LINK_MODES[l.mode][:24]:24}  "
                      f"{STATUS_WORDS.get(l.status, l.status)[:16]:16}  next {due.date() if due else '-'}  {l.label}")
            return 0
        ids = args.ids if args.action == "check" else [l.id for l in (lib.links() if args.all else lib.links_due())]
        if not ids:
            print("Nothing to re-check.")
            return 0
        res = run_links(lib, ids, log=print)
        if any(r.get("added") or r.get("updated") for r in res):
            from .indexer import build_index
            build_index(lib, log=print)
        return 0 if all(r["status"] == "ok" for r in res) else 1
    if args.cmd == "stats":
        for k, v in lib.stats().items():
            print(f"{k:>16}: {v}")
        print(f"{'library':>16}: {lib.root}")
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
