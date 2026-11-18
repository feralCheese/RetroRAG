"""Export the library for other RAG systems.

  * Markdown corpus: one clean .md per document with YAML front matter, organised by system/category.
    Drop it into Claude Projects knowledge, Open WebUI / AnythingLLM / LM Studio document collections, NotebookLM...
  * JSONL chunks: the exact chunks RetroRAG retrieves, with metadata (and optionally embeddings), for LlamaIndex,
    LangChain, Chroma, Qdrant, etc.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from .library import Library, safe_filename, slugify
from .textutil import strip_markers


def _yaml(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def export_markdown(lib: Library, out_dir: Path, system_ids: list[int] | None = None, log=print) -> int:
    out_dir = Path(out_dir)
    n = 0
    for s in lib.systems():
        if system_ids and s.id not in system_ids:
            continue
        for d in lib.documents(s.id):
            text = strip_markers(lib.read_text(d))
            if not text.strip():
                continue
            target = out_dir / s.slug / slugify(d.category) / d.group_path / (safe_filename(Path(d.path).name) + ".md")
            target.parent.mkdir(parents=True, exist_ok=True)
            fm = ["---", f"title: {_yaml(d.title)}", f"system: {_yaml(s.name)}", f"category: {_yaml(d.category)}",
                  f"source_file: {_yaml(Path(d.path).name)}", f"kind: {d.kind}"]
            if s.aliases:
                fm.append(f"system_aliases: {_yaml(s.aliases)}")
            if d.pages:
                fm.append(f"pages: {d.pages}")
            if d.tags:
                fm.append(f"tags: {_yaml(d.tags)}")
            if d.source_url:
                fm.append(f"source_url: {_yaml(d.source_url)}")
            from .relations import related
            links = related(lib, d.id)
            if links:
                fm.append("related:")
                for l in links:
                    fm.append(f"  - {{title: {_yaml(l['title'])}, system: {_yaml(l['system'])}, "
                              f"relation: {_yaml(l['relation'])}" + (f", note: {_yaml(l['note'])}" if l['note'] else "") + "}")
            if d.text_status in ("ocr", "reviewed"):
                fm.append(f"ocr: {'reviewed' if d.text_status == 'reviewed' else 'unreviewed'}")
            fm.append("---\n")
            body = text if d.kind != "code" else f"```\n{text}\n```\n"
            target.write_text("\n".join(fm) + body, "utf-8")
            n += 1
    log(f"Exported {n} markdown documents to {out_dir}")
    return n


def export_jsonl(lib: Library, out_file: Path, include_embeddings: bool = False, log=print) -> int:
    c = lib.conn()
    rows = c.execute("""SELECT c.id, c.doc_id, c.ord, c.page, c.heading, c.text, c.embedding, d.title, d.category,
                               d.path, d.kind, s.name AS system FROM chunks c JOIN documents d ON d.id=c.doc_id
                               JOIN systems s ON s.id=d.system_id ORDER BY c.doc_id, c.ord""")
    n = 0
    with open(out_file, "w", encoding="utf-8") as f:
        for r in rows:
            rec = {"id": f"{r['doc_id']}-{r['ord']}", "text": r["text"],
                   "metadata": {"system": r["system"], "title": r["title"], "category": r["category"],
                                "page": r["page"], "section": r["heading"].split(" | ", 1)[-1],
                                "file": Path(r["path"]).name, "kind": r["kind"], "doc_id": r["doc_id"]}}
            if include_embeddings and r["embedding"]:
                rec["embedding_f32_b64"] = base64.b64encode(r["embedding"]).decode()
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    if include_embeddings:
        log(f"Embeddings model: {lib.get_meta('embed_signature', '')}")
    log(f"Exported {n} chunks to {out_file}")
    return n
