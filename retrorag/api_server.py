"""Local REST API (with OpenAPI spec) + MCP over HTTP, for AI tools that can't use stdio MCP.

    REST:  http://127.0.0.1:8765/docs          (Swagger UI)   /openapi.json  (Open WebUI "tool server", etc.)
    MCP:   http://127.0.0.1:8766/mcp           (streamable HTTP: LM Studio, Open WebUI, Cursor, other MCP clients)
Everything is read-only except creating disk/tape images (POST /images) and the upload area (/uploads, /transfer),
which hold files passing between an AI chat and RetroRAG. /transfer/<id>/... links need no access token: the long
random id in the link is the permission, and areas expire (see transfer.py).
"""
from __future__ import annotations

import threading
from pathlib import Path

from .config import APP_VERSION
from .library import Library
from . import search as S


def create_app(library: str | Path):
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel

    lib = Library(library)
    app = FastAPI(title="RetroRAG", version=APP_VERSION,
                  description="Search a library of retro computer & console documentation "
                              "(manuals, programming references, schematics, pinouts, datasheets, emulator source).")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])

    class SearchRequest(BaseModel):
        query: str
        system: str | None = None
        category: str | None = None
        limit: int = 8
        mode: str = "hybrid"

    globals()["SearchRequest"] = SearchRequest      # FastAPI resolves (string) annotations in module globals

    def _search(query, system, category, limit, mode):
        lib.reload_settings()
        return [r.to_dict() for r in S.search(lib, query, k=max(1, min(limit, 50)), system=system,
                                              category=category, mode=mode)]

    @app.get("/", include_in_schema=False)
    def root():
        st = lib.stats()
        return {"name": "RetroRAG", "version": APP_VERSION, "library": str(lib.root), **st,
                "endpoints": ["/search", "/systems", "/documents", "/documents/{id}/text", "/documents/{id}/image",
                              "/media/formats", "/images", "/docs", "/openapi.json"]}

    @app.get("/search", operation_id="search_docs", summary="Search the retro documentation library")
    def search_get(q: str = Query(..., description="Question or keywords, e.g. 'VIC-II raster interrupt' or '$D012'"),
                   system: str | None = Query(None, description="System name or alias, e.g. 'C64', 'Amiga 500'"),
                   category: str | None = Query(None, description="e.g. 'Programming Reference', 'Schematics', 'Pinouts'"),
                   limit: int = 8, mode: str = Query("hybrid", description="hybrid | keyword | semantic")):
        return _search(q, system, category, limit, mode)

    @app.post("/search", operation_id="search_docs_post", summary="Search (JSON body)")
    def search_post(req: SearchRequest):
        return _search(req.query, req.system, req.category, req.limit, req.mode)

    @app.get("/systems", operation_id="list_systems", summary="List retro systems in the library")
    def systems():
        return [s.__dict__ for s in lib.systems()]

    @app.get("/documents", operation_id="list_documents", summary="List documents")
    def documents(system: str | None = None, category: str | None = None, limit: int = 500):
        sid = None
        if system:
            s = lib.find_system(system)
            if not s:
                raise HTTPException(404, f"No system matching '{system}'")
            sid = s.id
        return [{"doc_id": d.id, "title": d.title, "category": d.category, "kind": d.kind, "pages": d.pages,
                 "file": Path(d.path).name, "folder": d.group_path, "text_status": d.text_status}
                for d in lib.documents(sid, category)[:limit]]

    @app.get("/documents/{doc_id}", operation_id="get_document", summary="Document metadata")
    def document(doc_id: int):
        d = lib.document(doc_id)
        if not d:
            raise HTTPException(404, "not found")
        return d.__dict__

    @app.get("/documents/{doc_id}/related", operation_id="get_related",
             summary="Documents linked to this one (pinouts, datasheets, source code, errata, revisions)")
    def document_related(doc_id: int):
        from .relations import related
        if not lib.document(doc_id):
            raise HTTPException(404, "not found")
        return [{k: v for k, v in l.items() if k not in ("rel_id", "origin")} for l in related(lib, doc_id)]

    @app.get("/documents/{doc_id}/text", operation_id="read_document",
             summary="Read a document's text (optionally a page range)")
    def document_text(doc_id: int, page_from: int | None = None, page_to: int | None = None, max_chars: int = 30000):
        if not lib.document(doc_id):
            raise HTTPException(404, "not found")
        return {"doc_id": doc_id, "text": S.document_text(lib, doc_id, page_from, page_to, max_chars)}

    # ------------------------------------------------------------------ disk / tape images
    from fastapi import Response
    from . import media, media_ops

    def _img(doc_id: int):
        d = lib.document(doc_id)
        if not d:
            raise HTTPException(404, "not found")
        try:
            return d, media_ops.open_doc(lib, d)
        except media.MediaError as e:
            raise HTTPException(422, str(e))

    @app.get("/media/formats", operation_id="list_media_formats",
             summary="Disk/tape image formats (and which ones a system uses)")
    def media_formats(system: str | None = None):
        out = {"formats": [f.describe() for f in media.all_formats()]}
        if system:
            s = lib.find_system(system)
            if not s:
                raise HTTPException(404, f"No system matching '{system}'")
            ids, how = lib.system_formats(s.id)
            out.update(system=s.name, system_formats=ids, system_formats_are=how)
        return out

    @app.get("/documents/{doc_id}/image", operation_id="list_image_contents",
             summary="Directory of a disk or tape image")
    def image_contents(doc_id: int):
        d, img = _img(doc_id)
        return {"doc_id": d.id, "title": d.title, **media_ops.image_info(img)}

    @app.get("/documents/{doc_id}/image/file", operation_id="read_image_file",
             summary="One file from a disk/tape image: as text (BASIC listing / text / hex dump) or raw bytes")
    def image_file(doc_id: int, name: str = Query(..., description="File name as the directory shows it"),
                   raw: bool = Query(False, description="true = download the exact bytes")):
        d, img = _img(doc_id)
        try:
            e = img.find(name)
        except media.MediaError as ex:
            raise HTTPException(404, str(ex))
        if raw:
            fn = media_ops.host_name(img, e).split("/")[-1]
            return Response(media_ops.export_bytes(img, e), media_type="application/octet-stream",
                            headers={"Content-Disposition": f'attachment; filename="{fn}"'})
        how, text = media.text_view(img, e)
        return {**media_ops.entry_info(img, e), "shown_as": how, "text": text}

    class ImageFile(BaseModel):
        name: str = ""
        text: str | None = None
        base64: str | None = None
        doc_id: int | None = None
        upload: str | None = None
        basic: bool = False
        type: str = ""
        load: str | int | None = None
        exec: str | int | None = None
        autostart: int | None = None

    class ImageRequest(BaseModel):
        format: str
        files: list[ImageFile]
        system: str | None = None
        label: str = ""
        title: str | None = None
        options: dict = {}
        add_to_library: bool = True

    class ExtractRequest(BaseModel):
        file: str = ""
        names: list[str] | None = None

    class AddRequest(BaseModel):
        system: str
        file: str = ""
        category: str | None = None
        title: str | None = None

    globals().update(ImageFile=ImageFile, ImageRequest=ImageRequest, ExtractRequest=ExtractRequest,
                     AddRequest=AddRequest)

    @app.post("/images", operation_id="create_image",
              summary="Create a disk/tape image from text, BASIC source, base64 data or library documents")
    def create_image(req: ImageRequest):
        s = lib.find_system(req.system) if req.system else None
        if req.system and not s:
            raise HTTPException(404, f"No system matching '{req.system}'")
        if req.add_to_library and not s:
            raise HTTPException(422, "Give a system, or add_to_library=false to just get a download link")
        try:
            fmt = media.get(req.format)
            nfs, sources = media_ops.files_from_specs(lib, fmt, [f.model_dump(exclude_none=True) for f in req.files])
            if not req.add_to_library:
                return media_ops.image_to_area(lib, fmt.id, nfs, label=req.label, title=req.title,
                                               options=req.options)
            doc_id = media_ops.create_image_doc(lib, s, fmt.id, nfs, label=req.label, title=req.title,
                                                options=req.options, source_doc_ids=sources, log=lambda m: None)
        except (media.MediaError, transfer.TransferError, ValueError) as e:
            raise HTTPException(422, str(e))
        d = lib.document(doc_id)
        return {"doc_id": doc_id, "title": d.title, "path": str(lib.abspath(d.path)),
                **media_ops.image_info(media_ops.open_doc(lib, d))}

    # ------------------------------------------------------------------ uploads and downloads
    from fastapi import Request
    from fastapi.responses import FileResponse, HTMLResponse
    from . import transfer
    globals()["Request"] = Request                    # resolvable for FastAPI, like the models above

    def _t(fn, *a, **k):
        try:
            return fn(*a, **k)
        except (transfer.TransferError, media.MediaError) as e:
            raise HTTPException(404 if "No " in str(e) or "expired" in str(e) else 422, str(e))

    @app.post("/uploads", operation_id="get_upload_link",
              summary="A link where the user (or code) can send files to RetroRAG")
    def new_upload():
        return _t(transfer.upload_link, lib)

    @app.get("/uploads/{upload_id}", operation_id="list_uploads", summary="Files sent with an upload link")
    def list_uploads(upload_id: str):
        return {"upload_id": upload_id, "files": _t(transfer.list_area, lib, upload_id)}

    @app.get("/uploads/{upload_id}/image", operation_id="list_upload_image_contents",
             summary="Directory of an uploaded disk/tape image")
    def upload_image(upload_id: str, file: str = ""):
        ref = f"{upload_id}/{file}" if file else upload_id
        img, title, _ = _t(media_ops.open_source, lib, None, ref)
        return {"upload": ref, "title": title, **media_ops.image_info(img)}

    @app.post("/uploads/{upload_id}/extract", operation_id="extract_upload_image_files",
              summary="Extract the files on an uploaded disk/tape image: download links, nothing added to the library")
    def upload_extract(upload_id: str, req: ExtractRequest):
        ref = f"{upload_id}/{req.file}" if req.file else upload_id
        img, title, _ = _t(media_ops.open_source, lib, None, ref)
        return _t(media_ops.extract_to_area, lib, img, req.names or None, title)

    @app.post("/documents/{doc_id}/image/extract", operation_id="extract_image_files",
              summary="Extract the files on a library disk/tape image as download links")
    def doc_extract(doc_id: int, req: ExtractRequest):
        img, title, _ = _t(media_ops.open_source, lib, doc_id, None)
        return _t(media_ops.extract_to_area, lib, img, req.names or None, title)

    @app.post("/uploads/{upload_id}/add", operation_id="add_file_to_library",
              summary="Add an uploaded file (or all of an upload's files) to the library")
    def upload_add(upload_id: str, req: AddRequest):
        s = lib.find_system(req.system)
        if not s:
            raise HTTPException(404, f"No system matching '{req.system}'")
        ref = f"{upload_id}/{req.file}" if req.file else upload_id
        ids = _t(media_ops.add_upload_to_library, lib, ref, s, req.category, req.title, log=lambda m: None)
        return {"added": [{"doc_id": i, "title": lib.document(i).title} for i in ids]}

    @app.get("/transfer/{tid}/upload", include_in_schema=False)
    def upload_page(tid: str):
        if not transfer.upload_open(lib, tid):
            return HTMLResponse("<h1>This upload link has expired</h1><p>Ask the AI for a new one.</p>", 410)
        return HTMLResponse(transfer.upload_page())

    @app.put("/transfer/{tid}/upload/{filename:path}", include_in_schema=False)
    async def upload_put(tid: str, filename: str, request: Request):
        if not transfer.upload_open(lib, tid):
            raise HTTPException(410, "This upload link has expired - ask for a new one")
        data = await request.body()
        return {"saved": _t(transfer.save_file, lib, tid, filename, data)}

    @app.get("/transfer/{tid}/{path:path}", include_in_schema=False)
    def download(tid: str, path: str):
        try:
            f = transfer.resolve(lib, f"{tid}/{path}")
        except transfer.TransferError as e:
            raise HTTPException(404, str(e))
        if f.name == ".meta.json":
            raise HTTPException(404, "not found")
        return FileResponse(f, filename=f.name, media_type="application/octet-stream")

    return app


class ServerThread:
    """Run a uvicorn server in a background thread so the GUI can start/stop it."""

    def __init__(self, app, host: str, port: int):
        import uvicorn
        # use_colors=False: never probe the console (there isn't one when the app runs without a window)
        self.config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on",
                                     use_colors=False)
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.host, self.port = host, port

    def start(self):
        self.thread.start()
        return self

    def stop(self, timeout: float = 5):
        self.server.should_exit = True
        self.thread.join(timeout)

    @property
    def running(self) -> bool:
        return self.thread.is_alive()


AUTH_HELP = (b'{"detail":"missing or wrong access token. Send the header Authorization with the value '
             b'\'Bearer <token>\' (the token is in RetroRAG: Settings > Servers, or AI > Connect AI)."}')


def presented_tokens(headers: dict[str, str]) -> list[str]:
    """Every place a client might have put the access token. The standard is 'Authorization: Bearer <token>', but
    apps with separate header name / value boxes get filled in other ways too: 'Authorization: <token>',
    'X-API-Key: <token>', or a header called 'Bearer' holding the token. Any of them is fine as long as the token
    itself is right."""
    out = []
    auth = headers.get("authorization", "").strip()
    if auth:
        scheme, _, rest = auth.partition(" ")
        out.append(rest.strip() if scheme.lower() == "bearer" and rest.strip() else auth)
    for name in ("x-api-key", "bearer", "api-key"):
        v = headers.get(name, "").strip()
        if v:
            out.append(v[7:].strip() if v.lower().startswith("bearer ") else v)
    return out


class BearerAuth:
    """Minimal ASGI middleware: require the access token (see presented_tokens); CORS preflight is let through."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token

    def allowed(self, headers: dict[str, str]) -> bool:
        import hmac
        want = self.token.encode()
        return any(hmac.compare_digest(t.encode(), want) for t in presented_tokens(headers))

    async def __call__(self, scope, receive, send):
        if (scope["type"] == "http" and scope.get("method") != "OPTIONS"
                and not scope.get("path", "").startswith("/transfer/")):      # transfer links carry their own key
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            if not self.allowed(headers):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": AUTH_HELP})
                return
        await self.app(scope, receive, send)


def mcp_http_app(library: str | Path, host: str):
    from .mcp_server import build_server
    mcp = build_server(library)
    kw = {}
    if host not in ("127.0.0.1", "localhost"):
        # Listening on the LAN: allow any Host header (DNS-rebinding protection is for localhost-only servers)
        from mcp.server.transport_security import TransportSecuritySettings
        kw["transport_security"] = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return mcp.streamable_http_app(host=host, **kw)


def start_servers(library: str | Path, host: str, port: int, mcp_port: int | None = None,
                  token: str = "") -> list[ServerThread]:
    wrap = (lambda a: BearerAuth(a, token)) if token else (lambda a: a)
    servers = [ServerThread(wrap(create_app(library)), host, port).start()]
    if mcp_port:
        servers.append(ServerThread(wrap(mcp_http_app(library, host)), host, mcp_port).start())
    return servers
