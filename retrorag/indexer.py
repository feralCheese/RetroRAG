"""Build the retrieval index: chunk each document, store chunks in SQLite/FTS5, embed them."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np

from .chunker import Chunk, chunk_code, chunk_prose
from .embeddings import EmbeddingError, get_embedder
from .library import Document, Library, System, now_iso
from .textutil import count_markers, hex_keywords

Log = Callable[[str], None]


def doc_header(lib: Library, d: Document, s: System | None) -> str:
    """A small always-present chunk describing the document, so every file (even an un-OCR'd schematic
    image or a disk image) can be found by title, system, category, tags or notes."""
    sysname = s.name if s else ""
    lines = [f"Document: {d.title}",
             f"System: {lib.family_label(s) if s else sysname}" + (f" (also known as {s.aliases})" if s and s.aliases else ""),
             f"Category: {d.category}",
             f"File: {Path(d.path).name}" + (f"  (in {d.group_path})" if d.group_path else ""),
             f"Type: {d.kind}" + (f", {d.pages} pages" if d.pages else "")]
    if s and (s.manufacturer or s.year or s.cpu):
        lines.append("System details: " + ", ".join(x for x in (s.manufacturer, s.year, s.cpu) if x))
    from .config import NOTES_CATEGORY
    if d.category == NOTES_CATEGORY:
        lines.append("This is one of the library owner's own notes: their first-hand findings about the "
                     + ("machine" if s is None or not lib.effective_hosts(s.id) else "peripheral / expansion") + ".")
    if d.tags:
        lines.append(f"Tags: {d.tags}")
    if d.source_url and d.link_id:
        lines.append(f"Source: {d.source_url} (copied from the web {(d.text_updated or d.added or '')[:10]})")
    elif d.source_url:
        lines.append(f"Source: {d.source_url}")
    if d.notes:
        lines.append(f"Notes: {d.notes}")
    from .relations import describe, related
    links = related(lib, d.id)
    if links:
        lines.append("Related documents:\n" + describe(links))
    from .categorize import PHOTOS
    if d.kind in ("image", "pdf") and d.text_status == "none" and not d.needs_ocr:
        what = ("A photo, so there's no text to read" if d.kind == "image" and d.category == PHOTOS else
                "Nothing in it worth reading as text (not OCR'd)")
        lines.append(f"({what}: look at it with view_page(doc_id={d.id}).)")
    elif d.needs_ocr and d.text_status in ("none",):
        lines.append("(Text not yet available - scanned/image document awaiting OCR.)")
    return "\n".join(lines)


def chunk_document(lib: Library, d: Document) -> list[Chunk]:
    s = lib.system(d.system_id)
    st = lib.settings
    chunks = [Chunk(doc_header(lib, d, s), None, "Document info")]
    text = lib.read_text(d)
    if text.strip():
        if d.kind == "code":
            rel = f"{d.group_path}/{Path(d.path).name}" if d.group_path else Path(d.path).name
            chunks += chunk_code(text, rel, st.code_chunk_lines, st.chunk_chars * 2)
        else:
            chunks += chunk_prose(text, st.chunk_chars, st.chunk_overlap)
    return chunks


def embed_context(title: str, system: str, heading: str, text: str) -> str:
    ctx = f"{system} - {title}" + (f" - {heading}" if heading and heading != "Document info" else "")
    return f"{ctx}\n{text}"


def index_document(lib: Library, d: Document) -> int:
    s = lib.system(d.system_id)
    chunks = chunk_document(lib, d)
    sys_kw = " ".join(x for x in ((s.name, s.aliases.replace(",", " ")) if s else ()) if x)
    flagged = count_markers(lib.read_text(d)) if d.kind in ("pdf", "image") else 0
    c = lib.conn()
    # Re-indexing after an edit usually changes only a few chunks: keep the embeddings of the unchanged ones
    # (embedding is by far the slowest part of indexing).
    old = {(r["heading"], r["text"]): r["embedding"] for r in
           c.execute("SELECT heading, text, embedding FROM chunks WHERE doc_id=? AND embedding IS NOT NULL", (d.id,))}
    rows = []
    for i, ch in enumerate(chunks):
        heading = f"{d.title} | {ch.heading}" if ch.heading else d.title
        rows.append((d.id, i, ch.page, heading, ch.text, f"{sys_kw} {hex_keywords(ch.text)}", old.get((heading, ch.text))))
    with c:
        c.execute("DELETE FROM chunks WHERE doc_id=?", (d.id,))
        c.executemany("INSERT INTO chunks(doc_id, ord, page, heading, text, keywords, embedding) VALUES (?,?,?,?,?,?,?)",
                      rows)
        c.execute("UPDATE documents SET indexed=?, flagged=? WHERE id=?", (now_iso(), flagged, d.id))
    return len(chunks)


def embed_missing(lib: Library, progress=None, cancel=None, log: Log = print) -> int:
    emb = get_embedder(lib.settings)
    if emb is None:
        return 0
    import itertools
    import time
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    st = lib.settings
    c = lib.conn()
    code_filter = "" if st.embed_code else " AND d.kind != 'code'"   # source code: keyword search only (fast)
    rows = c.execute(f"""SELECT c.id, c.text, c.heading, d.title, s.name AS sysname FROM chunks c
                         JOIN documents d ON d.id=c.doc_id JOIN systems s ON s.id=d.system_id
                         WHERE c.embedding IS NULL{code_filter} ORDER BY c.id""").fetchall()
    if not rows:
        lib.set_meta("embed_signature", emb.signature)
        return 0
    if hasattr(emb, "prepare"):
        emb.prepare(log)                       # built-in model: load (download once if needed)
    log(f"Embedding {len(rows)} chunks with {st.embed_provider}/{st.embed_model} ...")
    # the built-in model already uses every CPU core; parallel requests only help with servers
    bs, par = max(1, st.embed_batch), (1 if st.embed_provider == "builtin" else max(1, st.embed_parallel))
    heading = lambda r: r["heading"].split(" | ", 1)[-1]
    batches = [rows[i:i + bs] for i in range(0, len(rows), bs)]

    def work(batch):
        return batch, emb.embed([embed_context(r["title"], r["sysname"], heading(r), r["text"])[:8000] for r in batch])

    done, t0, cancelled = 0, time.time(), False
    with ThreadPoolExecutor(par) as ex:            # keep `par` requests in flight
        it = iter(batches)
        pending = {ex.submit(work, b) for b in itertools.islice(it, par)}
        while pending:
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for f in finished:
                batch, vecs = f.result()          # re-raises EmbeddingError
                with c:
                    c.executemany("UPDATE chunks SET embedding=? WHERE id=?",
                                  [(v.astype(np.float32).tobytes(), r["id"]) for v, r in zip(vecs, batch)])
                done += len(batch)
                if progress:
                    rate = done / max(0.001, time.time() - t0)
                    left = (len(rows) - done) / rate if rate else 0
                    eta = f"~{left / 60:.0f} min left" if left >= 90 else f"~{left:.0f} s left"
                    progress(done, len(rows), f"embedding {done}/{len(rows)} chunks · {rate:.1f}/s · {eta}")
            if cancel and cancel():
                cancelled = True
            if not cancelled:
                for b in itertools.islice(it, par - len(pending)):
                    pending.add(ex.submit(work, b))
    if cancelled:
        log("Embedding cancelled (progress saved - Build index continues where it stopped).")
    else:
        log(f"Embedded {done} chunks in {time.time() - t0:.0f}s ({done / max(0.001, time.time() - t0):.1f}/s).")
    lib.set_meta("embed_signature", emb.signature)
    return done


def _refresh_media_cards(lib: Library, log: Log) -> None:
    """Disk / tape images added before RetroRAG could read them have a basic info card: rebuild it with the
    directory, BASIC listings and text files, so their contents become searchable."""
    from . import media, media_ops
    from .ingest import extract
    exts = sorted(media.media_exts() | {".md"})
    q = ",".join("?" * len(exts))
    done = 0
    for d in lib.documents(where=f"kind='binary' AND lower(ext) IN ({q})", params=exts):
        try:
            card = lib.abspath(d.text_path).read_text("utf-8", errors="replace") if d.text_path else ""
        except OSError:
            card = ""
        if any(f"## {w} contents (" in card for w in ("Disk", "Tape", "Cartridge")):
            continue
        if media_ops.doc_format(lib, d) is None:
            continue
        try:
            extract(lib, d, log=log)
            done += 1
        except Exception as e:
            log(f"Could not read the image {d.title}: {e}")
    if done:
        log(f"Listed the contents of {done} disk / tape image(s).")


def _convert_raw_rtf(lib: Library, log: Log) -> None:
    """RTF, Word and Excel files added before those formats were supported were indexed as raw RTF codes or as
    binary 'info cards': convert them to readable text now."""
    from .ingest import extract
    from .officedocs import OFFICE_EXT
    exts = sorted({".rtf"} | OFFICE_EXT)
    q = ",".join("?" * len(exts))
    old = lib.documents(where=f"lower(ext) IN ({q}) AND (text_path=path OR kind='binary')", params=exts)
    done = 0
    for d in old:
        try:
            if d.kind != "text":
                lib.update_document(d.id, kind="text")
                d = lib.document(d.id)
            extract(lib, d, log=log)
            done += 1
        except Exception as e:
            log(f"Could not convert {d.title}: {e}")
    if done:
        log(f"Converted {done} RTF / Word / Excel document(s) to readable text.")


def _maybe_switch_to_builtin(lib: Library, log: Log) -> None:
    """A library set to Ollama on a PC without Ollama, with nothing embedded yet: use the built-in model instead
    of silently falling back to keyword-only search."""
    st = lib.settings
    if st.embed_provider != "ollama" or lib.stats()["embedded"]:
        return
    from .embeddings import BUILTIN_DEFAULT, BUILTIN_MODELS, ollama_reachable
    if ollama_reachable(st.embed_url or "http://localhost:11434"):
        return
    spec = BUILTIN_MODELS[BUILTIN_DEFAULT]
    st.embed_provider, st.embed_model, st.embed_url = "builtin", BUILTIN_DEFAULT, ""
    st.embed_doc_prefix, st.embed_query_prefix = spec["doc_prefix"], spec["query_prefix"]
    st.save(lib.root)
    log("Ollama isn't running on this PC - switched this library to the built-in embedding model (no server needed).")


PDF_TEXT_READING = 2     # 2: rotated pages read as shown, letter-spaced text closed up (ingest.pdf_page_text)


def _reread_pdf_text(lib: Library, log: Log) -> None:
    """PDF text layers read before PDF_TEXT_READING 2 can be backwards (pages stored rotated) or spaced out
    ('c h e m i s t'): read those pages again, once. OCR'd and edited pages are kept (ingest.reread_text_layer)."""
    if int(lib.get_meta("pdf_text_reading", "1") or 1) >= PDF_TEXT_READING:
        return
    from .ingest import reread_text_layer
    docs = pages = 0
    for d in lib.documents(where="kind='pdf' AND text_path IS NOT NULL AND text_path != ''"):
        try:
            n = reread_text_layer(lib, d)
        except Exception as e:
            log(f"Couldn't re-read the text of {d.title}: {e}")
            continue
        if n:
            docs, pages = docs + 1, pages + n
            log(f"Re-read {n} page(s) of {d.title} (rotated or letter-spaced text)")
    lib.set_meta("pdf_text_reading", str(PDF_TEXT_READING))
    if pages:
        log(f"Re-read {pages} page(s) of {docs} PDF(s) whose text came out backwards or spaced out; "
            "OCR'd and edited pages were kept.")


def _describe_text_layers(lib: Library, log: Log) -> None:
    """Note where each PDF's own text is (for the document page), for PDFs added before RetroRAG did that."""
    import json
    import pymupdf
    from .ingest import layer_summary
    n = 0
    for d in lib.documents(where="kind='pdf' AND text_layer IS NULL"):
        try:
            with pymupdf.open(lib.abspath(d.path)) as doc:
                info = layer_summary(doc, lib.settings.pdf_min_text_chars)
        except Exception as e:
            info = {"error": str(e)[:200]}
        lib.update_document(d.id, text_layer=json.dumps(info))
        n += 1
    if n:
        log(f"Noted which pages of {n} PDF(s) came with their own text.")


def build_index(lib: Library, doc_ids: list[int] | None = None, *, rebuild_all: bool = False,
                progress=None, cancel=None, log: Log = print) -> dict:
    lib.reload_settings()
    if lib.settings.normalise_builtin():
        lib.settings.save(lib.root)
        log(f"Embedding model set to the built-in {lib.settings.embed_model}.")
    _maybe_switch_to_builtin(lib, log)
    _convert_raw_rtf(lib, log)
    _refresh_media_cards(lib, log)
    _reread_pdf_text(lib, log)
    _describe_text_layers(lib, log)
    sig = lib.settings.embed_signature()
    old_sig = lib.get_meta("embed_signature", "")
    c = lib.conn()
    sig_changed = sig != "none" and bool(old_sig) and old_sig != sig
    if sig_changed:
        log(f"Embedding model changed ({old_sig} -> {sig}): all chunks will be re-embedded.")
        with c:
            c.execute("UPDATE chunks SET embedding=NULL")
            c.execute("UPDATE doc_features SET vec=NULL")
    if doc_ids is not None:
        docs = [d for d in (lib.document(i) for i in doc_ids) if d]
    elif rebuild_all:
        docs = lib.documents()
    else:
        docs = lib.documents(where="(indexed IS NULL OR text_updated > indexed)")
    log(f"Indexing {len(docs)} document(s)...")
    n_chunks = 0
    for i, d in enumerate(docs):
        if cancel and cancel():
            log("Indexing cancelled.")
            break
        if progress:
            progress(i, len(docs), d.title)
        try:
            n_chunks += index_document(lib, d)
        except Exception as e:
            log(f"ERROR indexing {d.title}: {e}")
    embedded, err = 0, ""
    try:
        embedded = embed_missing(lib, progress=progress, cancel=cancel, log=log)
    except EmbeddingError as e:
        err = str(e)
        log(f"Embedding skipped: {e}\nKeyword search still works; re-run 'Build index' once embeddings are available.")
    try:   # fingerprints for link suggestions
        from .relations import update_features
        update_features(lib, [d.id for d in docs] if not (sig_changed and embedded) else [d.id for d in lib.documents()])
    except Exception as e:
        log(f"Could not update link fingerprints: {e}")
    lib.bump_index_version()
    if sig == "none":
        lib.set_meta("embed_signature", "none")
    log(f"Index updated: {len(docs)} docs, {n_chunks} chunks, {embedded} embedded.")
    return {"docs": len(docs), "chunks": n_chunks, "embedded": embedded, "error": err}
