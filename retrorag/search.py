"""Hybrid retrieval: BM25 (SQLite FTS5) + vector cosine similarity, fused with reciprocal-rank fusion."""
from __future__ import annotations

import re
import threading
from dataclasses import asdict, dataclass

import numpy as np

from .embeddings import EmbeddingError, get_embedder
from .library import Library
from .textutil import normalise_query_hex, split_pages, strip_markers

STOP = set("a an and are as at be by for from has have how i in is it its of on or that the this to was what "
           "when where which who why will with does do can you me my".split())


@dataclass
class Result:
    chunk_id: int
    doc_id: int
    score: float
    text: str
    page: int | None
    heading: str
    title: str
    system: str
    category: str
    path: str
    kind: str
    matched: str = ""   # 'keyword', 'semantic' or 'both'
    related: list = None   # links of the hit's document: [{doc_id, title, relation, note}]
    peripheral_for: list = None   # the hit is from a peripheral / expansion of these machines

    def __post_init__(self):
        if self.related is None:
            self.related = []
        if self.peripheral_for is None:
            self.peripheral_for = []

    def citation(self) -> str:
        return f"{self.system} / {self.title}" + (f", page {self.page}" if self.page else "")

    def to_ai(self) -> dict:
        """What the AI gets for a hit (MCP)."""
        return {"citation": self.citation(), "doc_id": self.doc_id, "chunk_id": self.chunk_id, "page": self.page,
                "section": self.heading, "category": self.category, "kind": self.kind, "text": self.text,
                **({"peripheral_for": self.peripheral_for} if self.peripheral_for else {}),
                **({"related": self.related} if self.related else {})}

    def to_dict(self) -> dict:
        d = asdict(self)
        d["citation"] = self.citation()
        return d


class _VectorCache:
    """In-memory matrix of chunk embeddings, reloaded when the library's index_version changes."""
    lock = threading.Lock()

    def __init__(self):
        self.version = None
        self.ids = np.zeros(0, dtype=np.int64)
        self.doc_ids = np.zeros(0, dtype=np.int64)
        self.mat = np.zeros((0, 0), dtype=np.float16)

    def ensure(self, lib: Library) -> None:
        v = (str(lib.db_path), lib.get_meta("index_version", "0"))
        if v == self.version:
            return
        with self.lock:
            rows = lib.conn().execute("SELECT id, doc_id, embedding FROM chunks WHERE embedding IS NOT NULL").fetchall()
            if rows:
                dim = len(rows[0]["embedding"]) // 4
                rows = [r for r in rows if len(r["embedding"]) == dim * 4]
                self.ids = np.array([r["id"] for r in rows], dtype=np.int64)
                self.doc_ids = np.array([r["doc_id"] for r in rows], dtype=np.int64)
                self.mat = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]).astype(np.float16)
            else:
                self.__init__()
            self.version = v

    def query(self, q: np.ndarray, allowed_docs: set[int] | None, top: int) -> list[tuple[int, float]]:
        if not len(self.ids) or q.shape[0] != self.mat.shape[1]:
            return []
        sims = np.empty(len(self.ids), dtype=np.float32)
        q32 = q.astype(np.float32)
        for i in range(0, len(self.ids), 50000):
            sims[i:i + 50000] = self.mat[i:i + 50000].astype(np.float32) @ q32
        if allowed_docs is not None:
            sims[~np.isin(self.doc_ids, list(allowed_docs))] = -2
        top = min(top, len(sims))
        idx = np.argpartition(-sims, top - 1)[:top]
        idx = idx[np.argsort(-sims[idx])]
        return [(int(self.ids[i]), float(sims[i])) for i in idx if sims[i] > -1.5]


_caches: dict[str, _VectorCache] = {}


def _cache(lib: Library) -> _VectorCache:
    return _caches.setdefault(str(lib.db_path), _VectorCache())


def _fts_query(q: str) -> str:
    toks = [t for t in re.findall(r"\w+", normalise_query_hex(q).lower()) if t not in STOP]
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(toks))


def _filters(lib: Library, system: str | None, category: str | None, kinds: list[str] | None,
             peripherals: bool = True):
    where, params = [], []
    if system:
        s = lib.find_system(system)
        ids = lib.system_scope(s.id, peripherals=peripherals) if s else [-1]
        where.append(f"d.system_id IN ({','.join('?' * len(ids))})"); params += ids
    if category:
        from .config import canonical_category
        where.append("d.category LIKE ?"); params.append(canonical_category(category))
    if kinds:
        where.append(f"d.kind IN ({','.join('?' * len(kinds))})"); params += list(kinds)
    return where, params


def search(lib: Library, query: str, k: int = 8, system: str | None = None, category: str | None = None,
           kinds: list[str] | None = None, mode: str = "hybrid", per_doc: int = 3,
           include_peripherals: bool = True) -> list[Result]:
    """mode: hybrid | keyword | semantic. With a system, include_peripherals also searches the peripherals and
    expansions that work with it (each such hit says which machines it's a peripheral for)."""
    where, params = _filters(lib, system, category, kinds, include_peripherals)
    c = lib.conn()
    pool = max(40, k * 6)
    ranked: dict[str, list[int]] = {}

    if mode in ("hybrid", "keyword"):
        fq = _fts_query(query)
        if fq:
            sql = ("SELECT c.id FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid JOIN documents d ON d.id=c.doc_id "
                   "WHERE chunks_fts MATCH ? " + "".join(f" AND {w}" for w in where) +
                   " ORDER BY bm25(chunks_fts, 1.0, 2.0, 1.5) LIMIT ?")
            try:
                ranked["keyword"] = [r[0] for r in c.execute(sql, [fq, *params, pool]).fetchall()]
            except Exception:
                ranked["keyword"] = []

    if mode in ("hybrid", "semantic"):
        emb = get_embedder(lib.settings)
        if emb is not None and lib.get_meta("embed_signature", "") == emb.signature:
            cache = _cache(lib)
            cache.ensure(lib)
            allowed = None
            if where:
                allowed = {r[0] for r in c.execute("SELECT d.id FROM documents d WHERE " + " AND ".join(where), params)}
            try:
                qv = emb.embed([query], is_query=True)[0]
                ranked["semantic"] = [cid for cid, _ in cache.query(qv, allowed, pool)]
            except EmbeddingError:
                ranked["semantic"] = []

    # Reciprocal rank fusion
    fused: dict[int, float] = {}
    how: dict[int, set] = {}
    for name, ids in ranked.items():
        for rank, cid in enumerate(ids):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (60 + rank)
            how.setdefault(cid, set()).add(name)
    order = sorted(fused, key=lambda i: -fused[i])

    results: list[Result] = []
    per: dict[int, int] = {}
    host_cache: dict[int, list[str]] = {}

    def hosts_of(sid: int) -> list[str]:
        if sid not in host_cache:
            host_cache[sid] = [h.name for h in lib.effective_hosts(sid)]
        return host_cache[sid]
    for cid in order:
        r = c.execute("""SELECT c.id, c.doc_id, c.text, c.page, c.heading, d.title, d.category, d.path, d.kind,
                                s.name AS sysname, d.system_id FROM chunks c JOIN documents d ON d.id=c.doc_id
                                JOIN systems s ON s.id=d.system_id WHERE c.id=?""", (cid,)).fetchone()
        if not r or per.get(r["doc_id"], 0) >= per_doc:
            continue
        per[r["doc_id"]] = per.get(r["doc_id"], 0) + 1
        m = how[cid]
        results.append(Result(cid, r["doc_id"], round(fused[cid] * 1000, 2), r["text"], r["page"],
                              r["heading"].split(" | ", 1)[-1], r["title"], r["sysname"], r["category"],
                              r["path"], r["kind"], "both" if len(m) > 1 else next(iter(m))))
        results[-1].peripheral_for = hosts_of(r["system_id"])
        if len(results) >= k:
            break
    if results:
        from .relations import related_map
        rm = related_map(lib, list({r.doc_id for r in results}))
        for r in results:
            r.related = [{"doc_id": l["doc_id"], "title": l["title"], "relation": l["relation"],
                          **({"note": l["note"]} if l["note"] else {})} for l in rm.get(r.doc_id, [])[:5]]
    return results


def document_text(lib: Library, doc_id: int, page_from: int | None = None, page_to: int | None = None,
                  max_chars: int = 40000) -> str:
    d = lib.document(doc_id)
    if not d:
        return ""
    text = strip_markers(lib.read_text(d))
    if page_from or page_to:
        pages = split_pages(text)
        lo, hi = page_from or 1, page_to or page_from or 10 ** 9
        text = "\n\n".join(f"[page {n}]\n{b.strip()}" for n, b in pages if n is not None and lo <= n <= hi)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[... truncated at {max_chars} characters; request a page range for more]"
    return text


def neighbours(lib: Library, chunk_id: int, before: int = 1, after: int = 1) -> str:
    c = lib.conn()
    r = c.execute("SELECT doc_id, ord FROM chunks WHERE id=?", (chunk_id,)).fetchone()
    if not r:
        return ""
    rows = c.execute("SELECT text FROM chunks WHERE doc_id=? AND ord BETWEEN ? AND ? AND ord > 0 ORDER BY ord",
                     (r["doc_id"], r["ord"] - before, r["ord"] + after)).fetchall()
    return "\n\n".join(x["text"] for x in rows)
