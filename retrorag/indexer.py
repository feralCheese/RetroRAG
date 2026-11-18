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
             f"System: {sysname}" + (f" (also known as {s.aliases})" if s and s.aliases else ""),
             f"Category: {d.category}",
             f"File: {Path(d.path).name}" + (f"  (in {d.group_path})" if d.group_path else ""),
             f"Type: {d.kind}" + (f", {d.pages} pages" if d.pages else "")]
    if s and (s.manufacturer or s.year or s.cpu):
        lines.append("System details: " + ", ".join(x for x in (s.manufacturer, s.year, s.cpu) if x))
    if d.tags:
        lines.append(f"Tags: {d.tags}")
    if d.source_url:
        lines.append(f"Source: {d.source_url}")
    if d.notes:
        lines.append(f"Notes: {d.notes}")
    from .relations import describe, related
    links = related(lib, d.id)
    if links:
        lines.append("Related documents:\n" + describe(links))
    if d.needs_ocr and d.text_status in ("none",):
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
    with c:
        c.execute("DELETE FROM chunks WHERE doc_id=?", (d.id,))
        c.executemany(
            "INSERT INTO chunks(doc_id, ord, page, heading, text, keywords) VALUES (?,?,?,?,?,?)",
            [(d.id, i, ch.page, f"{d.title} | {ch.heading}" if ch.heading else d.title, ch.text,
              f"{sys_kw} {hex_keywords(ch.text)}") for i, ch in enumerate(chunks)])
        c.execute("UPDATE documents SET indexed=?, flagged=? WHERE id=?", (now_iso(), flagged, d.id))
    return len(chunks)


def embed_missing(lib: Library, progress=None, cancel=None, log: Log = print) -> int:
    emb = get_embedder(lib.settings)
    if emb is None:
        return 0
    c = lib.conn()
    rows = c.execute("""SELECT c.id, c.text, c.heading, d.title, s.name AS sysname FROM chunks c
                        JOIN documents d ON d.id=c.doc_id JOIN systems s ON s.id=d.system_id
                        WHERE c.embedding IS NULL ORDER BY c.id""").fetchall()
    if not rows:
        return 0
    log(f"Embedding {len(rows)} chunks with {lib.settings.embed_provider}/{lib.settings.embed_model} ...")
    bs = max(1, lib.settings.embed_batch)
    done = 0
    for i in range(0, len(rows), bs):
        if cancel and cancel():
            log("Embedding cancelled (progress saved).")
            break
        batch = rows[i:i + bs]
        heading = lambda r: r["heading"].split(" | ", 1)[-1]
        texts = [embed_context(r["title"], r["sysname"], heading(r), r["text"])[:8000] for r in batch]
        vecs = emb.embed(texts)
        with c:
            c.executemany("UPDATE chunks SET embedding=? WHERE id=?",
                          [(v.astype(np.float32).tobytes(), r["id"]) for v, r in zip(vecs, batch)])
        done += len(batch)
        if progress:
            progress(done, len(rows), f"embedded {done}/{len(rows)}")
    lib.set_meta("embed_signature", emb.signature)
    return done


def build_index(lib: Library, doc_ids: list[int] | None = None, *, rebuild_all: bool = False,
                progress=None, cancel=None, log: Log = print) -> dict:
    lib.reload_settings()
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
