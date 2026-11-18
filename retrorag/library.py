"""The library: a folder holding original files, derived text, and a SQLite database.

Layout:
    <library>/library.json            settings (see config.LibrarySettings)
    <library>/retrorag.db             systems, documents, chunks (+ FTS5 index, embeddings)
    <library>/systems/<slug>/<category-slug>/<file>          originals
    <library>/systems/<slug>/<category-slug>/<file>.md       extracted / OCR text (editable)
    <library>/systems/<slug>/<category-slug>/<file>.ocr.json OCR word boxes for the proofreading editor
"""
from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import LibrarySettings

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS systems (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE,
    manufacturer TEXT DEFAULT '',
    year TEXT DEFAULT '',
    kind TEXT DEFAULT '',
    cpu TEXT DEFAULT '',
    aliases TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    system_id INTEGER NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    path TEXT NOT NULL,              -- original file; relative to library unless absolute (linked in place)
    text_path TEXT DEFAULT '',       -- text used for RAG; relative to library unless absolute
    kind TEXT NOT NULL,              -- text | code | pdf | image | binary
    ext TEXT DEFAULT '',
    size INTEGER DEFAULT 0,
    sha1 TEXT DEFAULT '', md5 TEXT DEFAULT '', crc32 TEXT DEFAULT '',
    pages INTEGER DEFAULT 0,
    text_status TEXT DEFAULT 'none', -- none | extracted | partial | ocr | reviewed | info
    needs_ocr INTEGER DEFAULT 0,
    ocr_mean_conf REAL,
    flagged INTEGER DEFAULT 0,       -- low-confidence markers still in text
    tags TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    group_path TEXT DEFAULT '',      -- folder within a source tree (e.g. 'vice-3.8/src/vicii')
    added TEXT, text_updated TEXT, indexed TEXT,
    UNIQUE(system_id, path)
);
CREATE INDEX IF NOT EXISTS idx_docs_system ON documents(system_id, category);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    page INTEGER,
    heading TEXT DEFAULT '',
    text TEXT NOT NULL,
    keywords TEXT DEFAULT '',
    embedding BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, ord);

-- links between documents (see relations.py for the kinds)
CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY,
    src_doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    dst_doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    note TEXT DEFAULT '',
    origin TEXT DEFAULT 'manual',    -- manual | suggested
    created TEXT,
    UNIQUE(src_doc_id, dst_doc_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_rel_src ON relations(src_doc_id);
CREATE INDEX IF NOT EXISTS idx_rel_dst ON relations(dst_doc_id);
-- suggestions the user rejected (pair stored with the smaller id first)
CREATE TABLE IF NOT EXISTS relation_dismissed (
    a INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    b INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    PRIMARY KEY (a, b)
);
-- per-document fingerprints used to suggest links: chip names/numbers, register addresses, mean embedding
CREATE TABLE IF NOT EXISTS doc_features (
    doc_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    features TEXT DEFAULT '',
    vec BLOB,
    updated TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, heading, keywords, content='chunks', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, heading, keywords) VALUES (new.id, new.text, new.heading, new.keywords);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, heading, keywords) VALUES ('delete', old.id, old.text, old.heading, old.keywords);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE OF text, heading, keywords ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, heading, keywords) VALUES ('delete', old.id, old.text, old.heading, old.keywords);
    INSERT INTO chunks_fts(rowid, text, heading, keywords) VALUES (new.id, new.text, new.heading, new.keywords);
END;
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text.strip()).strip("-").lower()
    return s or "item"


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "file"


@dataclass
class System:
    id: int
    name: str
    slug: str
    manufacturer: str = ""
    year: str = ""
    kind: str = ""
    cpu: str = ""
    aliases: str = ""
    notes: str = ""
    created: str = ""


@dataclass
class Document:
    id: int
    system_id: int
    category: str
    title: str
    path: str
    text_path: str
    kind: str
    ext: str = ""
    size: int = 0
    sha1: str = ""
    md5: str = ""
    crc32: str = ""
    pages: int = 0
    text_status: str = "none"
    needs_ocr: int = 0
    ocr_mean_conf: float | None = None
    flagged: int = 0
    tags: str = ""
    notes: str = ""
    source_url: str = ""
    group_path: str = ""
    added: str = ""
    text_updated: str | None = None
    indexed: str | None = None

    @property
    def is_stale(self) -> bool:
        """Text changed (or never indexed) since the last index build."""
        return not self.indexed or (self.text_updated or "") > self.indexed


class Library:
    """Thread-safe(ish) access: one sqlite connection per thread, WAL mode."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "systems").mkdir(exist_ok=True)
        self.db_path = self.root / "retrorag.db"
        from .platform_util import is_network_path
        self.on_network = is_network_path(self.root)
        self._local = threading.local()
        self.settings = LibrarySettings.load(self.root)
        if not (self.root / "library.json").exists():
            self.settings.save(self.root)
        with self.conn() as c:
            c.executescript(SCHEMA)
            c.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))

    # ------------------------------------------------------------------ plumbing
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            # WAL is fast but needs shared memory, which network shares (NAS over SMB/NFS) don't provide reliably
            c.execute("PRAGMA journal_mode=" + ("DELETE" if self.on_network else "WAL"))
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    def reload_settings(self) -> None:
        self.settings = LibrarySettings.load(self.root)

    def abspath(self, p: str) -> Path:
        if not p:
            return Path()
        pp = Path(p)
        return pp if pp.is_absolute() else self.root / pp

    def relpath(self, p: Path) -> str:
        p = Path(p).resolve()
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            return str(p)

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        r = self.conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_meta(self, key: str, value: str) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, value))

    def bump_index_version(self) -> None:
        v = int(self.get_meta("index_version", "0") or 0) + 1
        self.set_meta("index_version", str(v))

    # ------------------------------------------------------------------ systems
    def systems(self) -> list[System]:
        rows = self.conn().execute("SELECT * FROM systems ORDER BY name COLLATE NOCASE").fetchall()
        return [System(**dict(r)) for r in rows]

    def system(self, system_id: int) -> System | None:
        r = self.conn().execute("SELECT * FROM systems WHERE id=?", (system_id,)).fetchone()
        return System(**dict(r)) if r else None

    def find_system(self, name_or_alias: str) -> System | None:
        """Match by id, name, slug or alias (case-insensitive)."""
        key = (name_or_alias or "").strip().lower()
        if not key:
            return None
        for s in self.systems():
            names = {s.name.lower(), s.slug.lower(), str(s.id)}
            names |= {a.strip().lower() for a in s.aliases.split(",") if a.strip()}
            if key in names:
                return s
        return None

    def system_dir(self, s: System) -> Path:
        return self.root / "systems" / s.slug

    def add_system(self, name: str, **kw: Any) -> System:
        base = slugify(name)
        slug, n = base, 2
        existing = {s.slug for s in self.systems()}
        while slug in existing:
            slug, n = f"{base}-{n}", n + 1
        cols = ["manufacturer", "year", "kind", "cpu", "aliases", "notes"]
        vals = [str(kw.get(k, "") or "") for k in cols]
        with self.conn() as c:
            cur = c.execute(
                f"INSERT INTO systems(name, slug, {', '.join(cols)}, created) VALUES (?,?,{','.join('?' * len(cols))},?)",
                [name.strip(), slug, *vals, now_iso()])
            sid = cur.lastrowid
        s = self.system(sid)
        self.system_dir(s).mkdir(parents=True, exist_ok=True)
        return s

    def update_system(self, system_id: int, **kw: Any) -> None:
        allowed = {"name", "manufacturer", "year", "kind", "cpu", "aliases", "notes"}
        sets = {k: v for k, v in kw.items() if k in allowed}
        if not sets:
            return
        with self.conn() as c:
            c.execute(f"UPDATE systems SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", [*sets.values(), system_id])
        # header chunks mention the system name -> mark docs stale
        with self.conn() as c:
            c.execute("UPDATE documents SET text_updated=? WHERE system_id=?", (now_iso(), system_id))

    def delete_system(self, system_id: int, delete_files: bool = False) -> str:
        """Remove a system and all its documents/chunks from the library.

        delete_files=False: files stay in systems/<slug>/ together with a system.json manifest (titles, tags,
                            notes, categories) so the folder can be re-imported later or on another PC.
        delete_files=True:  the system's folder is deleted. Files that were linked in place (outside the
                            library) are never deleted.
        Returns a human-readable summary."""
        s = self.system(system_id)
        if not s:
            return "No such system."
        n_docs = self.conn().execute("SELECT COUNT(*) FROM documents WHERE system_id=?", (system_id,)).fetchone()[0]
        sdir = self.system_dir(s)
        if not delete_files:
            from .ingest import write_manifest
            write_manifest(self, s)
        with self.conn() as c:
            c.execute("DELETE FROM systems WHERE id=?", (system_id,))
        self.bump_index_version()
        if delete_files:
            import shutil
            shutil.rmtree(sdir, ignore_errors=True)
            return f"Removed '{s.name}' ({n_docs} documents) and deleted its folder."
        return f"Removed '{s.name}' ({n_docs} documents) from the library. Files kept in {sdir}"

    # ------------------------------------------------------------------ documents
    def documents(self, system_id: int | None = None, category: str | None = None,
                  where: str = "", params: Iterable[Any] = ()) -> list[Document]:
        q, p = "SELECT * FROM documents WHERE 1=1", []
        if system_id is not None:
            q += " AND system_id=?"; p.append(system_id)
        if category:
            q += " AND category=?"; p.append(category)
        if where:
            q += " AND " + where; p.extend(params)
        q += " ORDER BY category, group_path, title COLLATE NOCASE"
        return [Document(**dict(r)) for r in self.conn().execute(q, p).fetchall()]

    def document(self, doc_id: int) -> Document | None:
        r = self.conn().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        return Document(**dict(r)) if r else None

    def find_document_by_path(self, system_id: int, path: str) -> Document | None:
        r = self.conn().execute("SELECT * FROM documents WHERE system_id=? AND path=?", (system_id, path)).fetchone()
        return Document(**dict(r)) if r else None

    def insert_document(self, **kw: Any) -> int:
        kw.setdefault("added", now_iso())
        kw.setdefault("text_updated", now_iso())
        cols = list(kw)
        with self.conn() as c:
            cur = c.execute(f"INSERT INTO documents({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [kw[k] for k in cols])
            return cur.lastrowid

    def update_document(self, doc_id: int, **kw: Any) -> None:
        if not kw:
            return
        with self.conn() as c:
            c.execute(f"UPDATE documents SET {', '.join(f'{k}=?' for k in kw)} WHERE id=?", [*kw.values(), doc_id])

    def touch_text(self, doc_id: int) -> None:
        self.update_document(doc_id, text_updated=now_iso())

    def delete_document(self, doc_id: int, delete_files: bool = False) -> None:
        d = self.document(doc_id)
        with self.conn() as c:
            c.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        self.bump_index_version()
        if delete_files and d:
            orig = self.abspath(d.path)
            if not Path(d.path).is_absolute():   # never delete linked-in-place originals
                for p in {orig, self.abspath(d.text_path), orig.with_name(orig.name + ".ocr.json")}:
                    try:
                        if p.is_file():
                            p.unlink()
                    except OSError:
                        pass
            else:
                for p in {self.abspath(d.text_path)}:
                    if p != orig and p.is_file() and self.root in p.parents:
                        p.unlink(missing_ok=True)

    def read_text(self, d: Document) -> str:
        if not d.text_path:
            return ""
        p = self.abspath(d.text_path)
        if not p.exists():
            return ""
        from .textutil import read_text_file
        return read_text_file(p)

    def derived_dir(self, d: Document) -> Path:
        """Where derived files (.md, .ocr.json) for a document live."""
        orig = self.abspath(d.path)
        if Path(d.path).is_absolute():
            # linked-in-place: keep derived files inside the library
            s = self.system(d.system_id)
            out = self.system_dir(s) / "_derived" / slugify(d.category) / d.group_path
            out.mkdir(parents=True, exist_ok=True)
            return out
        return orig.parent

    def derived_md_path(self, d: Document) -> Path:
        return self.derived_dir(d) / (Path(d.path).name + ".md")

    def ocr_json_path(self, d: Document) -> Path:
        return self.derived_dir(d) / (Path(d.path).name + ".ocr.json")

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict[str, Any]:
        c = self.conn()
        one = lambda q: c.execute(q).fetchone()[0]
        return {
            "systems": one("SELECT COUNT(*) FROM systems"),
            "documents": one("SELECT COUNT(*) FROM documents"),
            "chunks": one("SELECT COUNT(*) FROM chunks"),
            "embedded": one("SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL"),
            "needs_ocr": one("SELECT COUNT(*) FROM documents WHERE needs_ocr=1"),
            "flagged_docs": one("SELECT COUNT(*) FROM documents WHERE flagged>0"),
            "stale": one("SELECT COUNT(*) FROM documents WHERE indexed IS NULL OR text_updated > indexed"),
            "embed_signature": self.get_meta("embed_signature", ""),
        }
