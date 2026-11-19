"""Run heavy work (OCR, PDF extraction, imports) in a separate process.

PyMuPDF and Pillow hold Python's GIL while they render pages / filter images, so doing that work on a background
*thread* still freezes the window. A child process can't block the UI at all, and cancelling is a clean
terminate(). Progress and log lines come back over a queue.

Nothing here imports Qt, so the child process starts quickly.
"""
from __future__ import annotations

import importlib
import multiprocessing as mp
import queue as _queue
import traceback
from typing import Any, Callable


class Cancelled(Exception):
    pass


def run_in_process(target: str, kwargs: dict, progress: Callable[[int, int, str], None] | None = None,
                   log: Callable[[str], None] | None = None, cancelled: Callable[[], bool] | None = None) -> Any:
    """Call `module:function(**kwargs, progress=..., log=..., cancel=...)` in a child process and wait for it,
    relaying progress/log messages. Raises Cancelled if `cancelled()` becomes true (the child is terminated)."""
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_child, args=(target, kwargs, q), daemon=True, name=f"retrorag-{target.split(':')[-1]}")
    p.start()
    result, error, done = None, None, False
    try:
        while not done:
            try:
                msg = q.get(timeout=0.15)
            except _queue.Empty:
                if cancelled and cancelled():
                    p.terminate()
                    p.join(3)
                    raise Cancelled()
                if not p.is_alive():
                    error = error or f"worker process stopped unexpectedly (exit code {p.exitcode})"
                    break
                continue
            kind = msg[0]
            if kind == "progress" and progress:
                progress(*msg[1:])
            elif kind == "log" and log:
                log(msg[1])
            elif kind == "result":
                result, done = msg[1], True
            elif kind == "error":
                error, done = msg[1], True
    finally:
        if p.is_alive():
            p.join(5)
            if p.is_alive():
                p.terminate()
    if error:
        raise RuntimeError(error)
    return result


def _child(target: str, kwargs: dict, q) -> None:
    mod, fn = target.split(":")
    try:
        f = getattr(importlib.import_module(mod), fn)
        res = f(**kwargs,
                progress=lambda i, n, m="": q.put(("progress", int(i), int(n), str(m))),
                log=lambda m: q.put(("log", str(m))),
                cancel=lambda: False)
        q.put(("result", res))
    except Exception as e:
        q.put(("error", f"{e}\n{traceback.format_exc(limit=6)}"))


# ============================================================================ job entry points (run in the child)
def ocr_job(library: str, doc_ids: list[int], pages: list[int] | None = None, force: bool = False,
            psm: int | None = None, cleanup: str | None = None, app_tesseract: str = "",
            progress=None, log=print, cancel=None) -> list[dict]:
    from .library import Library
    from .ocr import ocr_document
    lib = Library(library)
    out = []
    for k, did in enumerate(doc_ids):
        d = lib.document(did)
        if not d:
            continue
        prefix = f"[{k + 1}/{len(doc_ids)}] " if len(doc_ids) > 1 else ""
        prog = (lambda i, n, m="", p=prefix: progress(i, n, p + m)) if progress else None
        res = ocr_document(lib, d, pages=pages, force=force, psm=psm, cleanup=cleanup, progress=prog,
                           cancel=cancel, log=log, app_tesseract=app_tesseract)
        out.append({"doc_id": did, **(res or {})})
    return out


def add_files_job(library: str, system_id: int, category: str, paths: list[str], copy: bool = True,
                  progress=None, log=print, cancel=None) -> list[int]:
    from pathlib import Path
    from .ingest import add_files
    from .library import Library
    lib = Library(library)
    return add_files(lib, lib.system(system_id), category, [Path(p) for p in paths], copy=copy,
                     progress=progress, cancel=cancel, log=log)


def add_folder_job(library: str, system_id: int, category: str, folder: str, copy: bool = True,
                   kinds: list[str] | None = None, progress=None, log=print, cancel=None) -> list[int]:
    from pathlib import Path
    from .ingest import add_folder
    from .library import Library
    lib = Library(library)
    return add_folder(lib, lib.system(system_id), category, Path(folder), copy=copy,
                      kinds=set(kinds) if kinds is not None else None, progress=progress, cancel=cancel, log=log)


def import_folder_job(library: str, system_id: int, folder: str, progress=None, log=print, cancel=None) -> list[int]:
    from pathlib import Path
    from .ingest import import_system_folder
    from .library import Library
    lib = Library(library)
    return import_system_folder(lib, lib.system(system_id), Path(folder), progress=progress, cancel=cancel, log=log)


def extract_job(library: str, doc_id: int, progress=None, log=print, cancel=None) -> int:
    from .ingest import extract
    from .library import Library
    lib = Library(library)
    extract(lib, lib.document(doc_id), log=log)
    return doc_id


def links_job(library: str, link_ids: list[int], progress=None, log=print, cancel=None) -> list[dict]:
    """Copy / re-copy / check links (webcopy.py). In a child process: PDF text extraction holds the GIL."""
    from .library import Library
    from .webcopy import run_links
    lib = Library(library)
    return run_links(lib, link_ids, progress=progress, log=log)
