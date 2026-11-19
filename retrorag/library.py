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
from datetime import datetime, timedelta, timezone
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
    created TEXT,
    parent_id INTEGER REFERENCES systems(id) ON DELETE SET NULL,  -- this system is a variant of parent_id
    media_formats TEXT                -- disk/tape formats it uses, e.g. 'd64,d81' (NULL = not chosen: suggest)
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
    link_id INTEGER REFERENCES links(id) ON DELETE SET NULL,   -- copied from this link (web page / site / repo)
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

-- peripherals and expansions: system_id (a disk drive, second processor, accelerator...) works with host_id
CREATE TABLE IF NOT EXISTS system_hosts (
    system_id INTEGER NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
    host_id INTEGER NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
    PRIMARY KEY (system_id, host_id)
);
CREATE INDEX IF NOT EXISTS idx_hosts_host ON system_hosts(host_id);

-- links on a system: just saved, or copied into the library (see webcopy.py)
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY,
    system_id INTEGER NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    title TEXT DEFAULT '',
    note TEXT DEFAULT '',
    mode TEXT DEFAULT 'link',        -- link (saved only) | page | site (the page and the pages under it) | repo (GitHub)
    category TEXT DEFAULT '',        -- where copies go
    max_pages INTEGER DEFAULT 100,   -- site: page limit
    include_files INTEGER DEFAULT 1, -- site: also copy PDFs / text files the pages link to
    branch TEXT DEFAULT '',          -- repo: branch, tag or commit ('' = the default branch)
    refresh_days INTEGER DEFAULT 180,-- remind to re-copy / re-check after this many days (0 = never)
    status TEXT DEFAULT '',          -- '' (not done yet) | ok | unreachable | gone | refused | error
    status_msg TEXT DEFAULT '',
    last_fetched TEXT,               -- last time it worked (copied, or checked for a saved link)
    last_checked TEXT,               -- last attempt
    fail_count INTEGER DEFAULT 0,    -- attempts in a row that couldn't reach it
    first_failed TEXT,
    version TEXT DEFAULT '',         -- repo: commit copied
    created TEXT
);
CREATE INDEX IF NOT EXISTS idx_links_system ON links(system_id);

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


def normalise_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("A link needs an address")
    if "://" not in url:
        url = "https://" + url
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("Links must be web addresses (http:// or https://)")
    return url


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
    parent_id: int | None = None      # variant of this system (None = top-level / family)
    media_formats: str | None = None  # 'd64,d81' ... ; None = never chosen (formats are suggested from the name)


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
    link_id: int | None = None
    added: str = ""
    text_updated: str | None = None
    indexed: str | None = None

    @property
    def is_stale(self) -> bool:
        """Text changed (or never indexed) since the last index build."""
        return not self.indexed or (self.text_updated or "") > self.indexed


LINK_MODES = {"link": "Saved link", "page": "Copy of the page", "site": "Copy of the page and the pages under it",
              "repo": "Copy of the GitHub repository"}
NOT_DUE = ("gone", "refused", "error")    # links nobody needs reminding about (until they're edited or retried)


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@dataclass
class Link:
    id: int
    system_id: int
    url: str
    title: str = ""
    note: str = ""
    mode: str = "link"
    category: str = ""
    max_pages: int = 100
    include_files: int = 1
    branch: str = ""
    refresh_days: int = 180
    status: str = ""
    status_msg: str = ""
    last_fetched: str | None = None
    last_checked: str | None = None
    fail_count: int = 0
    first_failed: str | None = None
    version: str = ""
    created: str = ""

    @property
    def copies(self) -> bool:
        return self.mode != "link"

    @property
    def label(self) -> str:
        return self.title or self.url

    def next_due(self) -> datetime | None:
        """When to re-copy (or re-check) it; None = no reminder (switched off, gone, refused, or needs editing)."""
        if self.refresh_days <= 0 or self.status in NOT_DUE:
            return None
        if self.status == "unreachable":          # try again tomorrow
            return (parse_iso(self.last_checked) or datetime.now(timezone.utc)) + timedelta(days=1)
        last = parse_iso(self.last_fetched)
        return last + timedelta(days=self.refresh_days) if last else parse_iso(self.created) or datetime.now(timezone.utc)

    def is_due(self, now: datetime | None = None) -> bool:
        due = self.next_due()
        return due is not None and due <= (now or datetime.now(timezone.utc))


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
            cols = {r["name"] for r in c.execute("PRAGMA table_info(systems)")}
            if "parent_id" not in cols:   # libraries made before variants existed
                c.execute("ALTER TABLE systems ADD COLUMN parent_id INTEGER REFERENCES systems(id) ON DELETE SET NULL")
            if "media_formats" not in cols:   # ... or before disk/tape formats
                c.execute("ALTER TABLE systems ADD COLUMN media_formats TEXT")
            dcols = {r["name"] for r in c.execute("PRAGMA table_info(documents)")}
            if "link_id" not in dcols:        # ... or before links
                c.execute("ALTER TABLE documents ADD COLUMN link_id INTEGER REFERENCES links(id) ON DELETE SET NULL")
            c.execute("CREATE INDEX IF NOT EXISTS idx_docs_link ON documents(link_id)")

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
        systems = self.systems()
        for s in systems:
            names = {s.name.lower(), s.slug.lower(), str(s.id)}
            names |= {a.strip().lower() for a in s.aliases.split(",") if a.strip()}
            if key in names:
                return s
        # no exact match (an AI saying "1541" for "1541 Disk Drive"): a whole-word match, preferring the top of a
        # family (so "1541" picks "1541 Disk Drive" rather than its variant "1541-II")
        if len(key) < 3:
            return None
        pat = re.compile(r"(?<![\w-])" + re.escape(key) + r"(?![\w-])")
        cands = [s for s in systems if pat.search(s.name.lower()) or
                 any(pat.search(a.strip().lower()) for a in s.aliases.split(",") if a.strip())]
        ids = {s.id for s in cands}
        tops = [s for s in cands if not ({a.id for a in self.ancestors(s.id)} & ids)]
        return min(tops, key=lambda s: len(s.name)) if tops else None

    def system_dir(self, s: System) -> Path:
        return self.root / "systems" / s.slug

    # ------------------------------------------------------------------ variants (system families)
    def children(self, system_id: int) -> list[System]:
        return [s for s in self.systems() if s.parent_id == system_id]

    def ancestors(self, system_id: int) -> list[System]:
        """Parent, grandparent, ... (nearest first)."""
        out, seen = [], {system_id}
        s = self.system(system_id)
        while s and s.parent_id and s.parent_id not in seen:
            seen.add(s.parent_id)
            s = self.system(s.parent_id)
            if s:
                out.append(s)
        return out

    def descendants(self, system_id: int) -> list[System]:
        by_parent: dict = {}
        for s in self.systems():
            by_parent.setdefault(s.parent_id, []).append(s)
        out, todo, seen = [], [system_id], {system_id}
        while todo:
            for ch in by_parent.get(todo.pop(), []):
                if ch.id not in seen:
                    seen.add(ch.id)
                    out.append(ch)
                    todo.append(ch.id)
        return out

    def system_scope(self, system_id: int, peripherals: bool = False) -> list[int]:
        """Systems whose documents apply when working with this one: itself, its variants, and the families it
        belongs to (their documents are common to all variants). peripherals=True adds the peripherals and
        expansions that work with any of those (and theirs in turn: a RAM board for a disk drive...)."""
        base = [system_id] + [s.id for s in self.ancestors(system_id)] + [s.id for s in self.descendants(system_id)]
        if not peripherals:
            return base
        by_host: dict[int, list[int]] = {}
        for r in self.conn().execute("SELECT system_id, host_id FROM system_hosts"):
            by_host.setdefault(r["host_id"], []).append(r["system_id"])
        if not by_host:
            return base
        out, seen, todo = list(base), set(base), list(base)
        while todo:
            for p in by_host.get(todo.pop(), []):
                for x in [p] + [a.id for a in self.ancestors(p)] + [d.id for d in self.descendants(p)]:
                    if x not in seen:
                        seen.add(x)
                        out.append(x)
                        todo.append(x)
        return out

    def family_label(self, s: System) -> str:
        """'ZX Spectrum 128K (variant of ZX Spectrum)' style description for headers and the AI."""
        anc = self.ancestors(s.id)
        kids = self.children(s.id)
        parts = []
        hosts = self.effective_hosts(s.id)
        if hosts:
            parts.append("a peripheral / expansion for " + ", ".join(h.name for h in hosts) +
                         " - its own hardware and documentation, and possibly its own CPU and OS")
        if anc:
            parts.append("variant of " + " > ".join(a.name for a in reversed(anc)))
        if kids:
            parts.append("documents here apply to all its variants: " + ", ".join(k.name for k in kids))
        return f"{s.name} ({'; '.join(parts)})" if parts else s.name

    # ------------------------------------------------------------------ peripherals and expansions
    def hosts(self, system_id: int) -> list[System]:
        """The machines a peripheral / expansion works with."""
        rows = self.conn().execute("SELECT s.* FROM system_hosts h JOIN systems s ON s.id=h.host_id "
                                   "WHERE h.system_id=? ORDER BY s.name COLLATE NOCASE", (system_id,)).fetchall()
        return [System(**dict(r)) for r in rows]

    def effective_hosts(self, system_id: int) -> list[System]:
        """Its own hosts, or (for a variant of a peripheral, e.g. 1541-II of 1541) its family's."""
        for sid in [system_id] + [a.id for a in self.ancestors(system_id)]:
            h = self.hosts(sid)
            if h:
                return h
        return []

    def peripherals(self, host_id: int) -> list[System]:
        """Peripherals and expansions attached directly to this system."""
        rows = self.conn().execute("SELECT s.* FROM system_hosts h JOIN systems s ON s.id=h.system_id "
                                   "WHERE h.host_id=? ORDER BY s.name COLLATE NOCASE", (host_id,)).fetchall()
        return [System(**dict(r)) for r in rows]

    def host_map(self) -> dict[int, list[int]]:
        """{peripheral id: [host ids]}"""
        out: dict[int, list[int]] = {}
        for r in self.conn().execute("SELECT system_id, host_id FROM system_hosts"):
            out.setdefault(r["system_id"], []).append(r["host_id"])
        return out

    def set_hosts(self, system_id: int, host_ids) -> None:
        """Make a system a peripheral / expansion of these machines ([] = of none)."""
        ids: list[int] = []
        for h in host_ids or []:
            h = int(h)
            if h != system_id and h not in ids and self.system(h):
                ids.append(h)
        old = {h.id for h in self.hosts(system_id)}
        if set(ids) == old:
            return
        with self.conn() as c:
            c.execute("DELETE FROM system_hosts WHERE system_id=?", (system_id,))
            c.executemany("INSERT INTO system_hosts(system_id, host_id) VALUES (?,?)", [(system_id, h) for h in ids])
        self._touch_family(system_id)      # document headers say what it's a peripheral of
        self.bump_index_version()

    def add_host(self, system_id: int, host_id: int) -> None:
        self.set_hosts(system_id, [h.id for h in self.hosts(system_id)] + [host_id])

    def remove_host(self, system_id: int, host_id: int) -> None:
        self.set_hosts(system_id, [h.id for h in self.hosts(system_id) if h.id != host_id])

    def _touch_family(self, system_id: int) -> None:
        """Document headers mention the family / variants: re-index the whole family after a change."""
        root = ([a.id for a in self.ancestors(system_id)] or [system_id])[-1]
        ids = [root] + [s.id for s in self.descendants(root)]
        with self.conn() as c:
            c.execute(f"UPDATE documents SET text_updated=? WHERE system_id IN ({','.join('?' * len(ids))})",
                      [now_iso(), *ids])

    def set_parent(self, system_id: int, parent_id: int | None) -> None:
        if parent_id is not None and (parent_id == system_id or
                                      parent_id in {s.id for s in self.descendants(system_id)}):
            raise ValueError("A system can't be a variant of itself or of one of its own variants.")
        old = self.system(system_id)
        with self.conn() as c:
            c.execute("UPDATE systems SET parent_id=? WHERE id=?", (parent_id, system_id))
        if old and old.parent_id:
            self._touch_family(old.parent_id)
        self._touch_family(system_id)

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
        if kw.get("media_formats") is not None:
            self.set_media_formats(sid, kw["media_formats"])
        s = self.system(sid)
        self.system_dir(s).mkdir(parents=True, exist_ok=True)
        if kw.get("parent_id"):
            self.set_parent(sid, int(kw["parent_id"]))
            s = self.system(sid)
        if kw.get("hosts"):
            self.set_hosts(sid, kw["hosts"])
        return s

    def update_system(self, system_id: int, **kw: Any) -> None:
        if "parent_id" in kw:
            cur = self.system(system_id)
            if cur and cur.parent_id != kw["parent_id"]:
                self.set_parent(system_id, kw["parent_id"])
        if "media_formats" in kw:
            self.set_media_formats(system_id, kw["media_formats"])
        if "hosts" in kw:
            self.set_hosts(system_id, kw["hosts"])
        allowed = {"name", "manufacturer", "year", "kind", "cpu", "aliases", "notes"}
        sets = {k: v for k, v in kw.items() if k in allowed}
        if not sets:
            return
        with self.conn() as c:
            c.execute(f"UPDATE systems SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", [*sets.values(), system_id])
        # header chunks mention the system name -> mark docs stale
        self._touch_family(system_id)

    # ------------------------------------------------------------------ disk / tape formats
    def set_media_formats(self, system_id: int, formats) -> None:
        """formats: list of ids or 'd64,d81'; None = forget the choice (suggest from the name again)."""
        if formats is not None and not isinstance(formats, str):
            formats = ",".join(formats)
        if formats is not None:
            from . import media
            known = {f.id for f in media.all_formats()}
            ids = [f.strip().lower().lstrip(".") for f in formats.split(",") if f.strip()]
            bad = [f for f in ids if f not in known]
            if bad:
                raise ValueError(f"Unknown disk/tape format(s): {', '.join(bad)}. Known: {', '.join(sorted(known))}")
            formats = ",".join(dict.fromkeys(ids))
        with self.conn() as c:
            c.execute("UPDATE systems SET media_formats=? WHERE id=?", (formats, system_id))

    def system_formats(self, system_id: int) -> tuple[list[str], str]:
        """Disk/tape formats for a system -> (ids, where from: 'chosen' | 'inherited' | 'suggested')."""
        from . import media
        s = self.system(system_id)
        if s is None:
            return [], "suggested"
        if s.media_formats is not None:
            return [f for f in s.media_formats.split(",") if f], "chosen"
        for a in self.ancestors(system_id):
            if a.media_formats is not None:
                return [f for f in a.media_formats.split(",") if f], "inherited"
        names = [s.name, s.aliases] + [a.name for a in self.ancestors(system_id)]
        ids = media.suggest_for_system(*names) or media.suggest_for_system(s.manufacturer)
        return ids, "suggested"

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
        if s.parent_id:
            self._touch_family(s.parent_id)
        with self.conn() as c:
            # its variants move up a level (to this system's own family, or top level)
            c.execute("UPDATE systems SET parent_id=? WHERE parent_id=?", (s.parent_id, system_id))
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

    # ------------------------------------------------------------------ moving documents
    def move_documents(self, doc_ids: list[int], system_id: int, category: str | None = None,
                       log=print, group_path: str | None = None) -> int:
        """Move documents to another system (and/or category). Files move on disk together with their OCR text,
        corrections and word boxes; links to other documents are kept (they refer to document ids).
        group_path: None keeps each document's folder, "" puts it at the top level of the category,
        "a/b" puts it in that folder."""
        import shutil
        from .ingest import _unique_dest
        target = self.system(system_id)
        if not target:
            raise ValueError("No such system")
        moved = 0
        for did in doc_ids:
            d = self.document(did)
            if not d:
                continue
            cat = category or d.category
            group = d.group_path if group_path is None else group_path.strip("/")
            if d.system_id == system_id and cat == d.category and group == d.group_path:
                continue
            old_md, old_js = self.derived_md_path(d), self.ocr_json_path(d)
            upd = {"system_id": system_id, "category": cat, "group_path": group, "text_updated": now_iso()}
            if d.kind == "image" and d.text_status == "none" and cat != d.category:
                from .categorize import PHOTOS
                upd["needs_ocr"] = int(cat != PHOTOS)     # photos aren't queued for OCR (see ingest.extract)
            if Path(d.path).is_absolute():              # linked in place: the original stays where it is
                new_dir = self.system_dir(target) / "_derived" / slugify(cat) / group
                new_name = Path(d.path).name
            else:
                src = self.abspath(d.path)
                new_dir = self.system_dir(target) / slugify(cat) / group
                new_dir.mkdir(parents=True, exist_ok=True)
                dest = _unique_dest(new_dir / src.name)
                if src.exists():
                    shutil.move(str(src), dest)
                upd["path"] = self.relpath(dest)
                new_name = dest.name
            new_dir.mkdir(parents=True, exist_ok=True)
            for old, suffix, key in ((old_md, ".md", "md"), (old_js, ".ocr.json", "js")):
                if old.exists() and old.resolve() != self.abspath(d.path).resolve():
                    new = new_dir / (new_name + suffix)
                    shutil.move(str(old), new)
                    if key == "md" and d.text_path and self.abspath(d.text_path).resolve() == old.resolve():
                        upd["text_path"] = self.relpath(new)
            if d.text_path == d.path and "path" in upd:      # text and code: the original *is* the text
                upd["text_path"] = upd["path"]
            self.update_document(did, **upd)
            self._prune_empty_dirs(self.abspath(d.path).parent)
            moved += 1
        if moved:
            self.bump_index_version()
            log(f"Moved {moved} document(s) to {target.name}" + (f" / {category}" if category else ""))
        return moved

    def _prune_empty_dirs(self, folder: Path) -> None:
        """Remove folders left empty by a move, up to (not including) the system folder."""
        stop = self.root / "systems"
        try:
            folder = folder.resolve()
            while stop in folder.parents and folder.parent != stop:
                if any(folder.iterdir()):
                    break
                folder.rmdir()
                folder = folder.parent
        except OSError:
            pass

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
            if not Path(d.path).is_absolute():
                self._prune_empty_dirs(self.abspath(d.path).parent)   # e.g. a folder an unzip created

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

    # ------------------------------------------------------------------ links (see webcopy.py)
    LINK_FIELDS = ("url", "title", "note", "mode", "category", "max_pages", "include_files", "branch", "refresh_days",
                   "status", "status_msg", "last_fetched", "last_checked", "fail_count", "first_failed", "version",
                   "system_id")

    def links(self, system_id: int | None = None) -> list[Link]:
        q, p = "SELECT * FROM links", []
        if system_id is not None:
            q += " WHERE system_id=?"
            p.append(system_id)
        rows = self.conn().execute(q + " ORDER BY system_id, title COLLATE NOCASE, url", p).fetchall()
        return [Link(**dict(r)) for r in rows]

    def link(self, link_id: int) -> Link | None:
        r = self.conn().execute("SELECT * FROM links WHERE id=?", (link_id,)).fetchone()
        return Link(**dict(r)) if r else None

    def add_link(self, system_id: int, url: str, **kw: Any) -> Link:
        url = normalise_url(url)
        vals = {k: v for k, v in kw.items() if k in self.LINK_FIELDS and k not in ("url", "system_id")}
        if vals.get("mode", "link") not in LINK_MODES:
            raise ValueError(f"Unknown link mode {vals['mode']!r}")
        cols = ["system_id", "url", "created", *vals]
        with self.conn() as c:
            cur = c.execute(f"INSERT INTO links({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [system_id, url, now_iso(), *vals.values()])
        return self.link(cur.lastrowid)

    def update_link(self, link_id: int, **kw: Any) -> None:
        sets = {k: v for k, v in kw.items() if k in self.LINK_FIELDS}
        if "url" in sets:
            sets["url"] = normalise_url(sets["url"])
        if not sets:
            return
        with self.conn() as c:
            c.execute(f"UPDATE links SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", [*sets.values(), link_id])

    def link_doc_ids(self, link_id: int) -> list[int]:
        return [r[0] for r in self.conn().execute("SELECT id FROM documents WHERE link_id=?", (link_id,))]

    def delete_link(self, link_id: int, delete_docs: bool = False) -> int:
        """Remove a link. delete_docs=True also removes the documents copied from it (and their files);
        otherwise they stay in the library as ordinary documents. -> number of documents removed."""
        ids = self.link_doc_ids(link_id) if delete_docs else []
        for did in ids:
            self.delete_document(did, delete_files=True)
        with self.conn() as c:
            c.execute("DELETE FROM links WHERE id=?", (link_id,))
        return len(ids)

    def links_due(self) -> list[Link]:
        now = datetime.now(timezone.utc)
        return [l for l in self.links() if l.is_due(now)]

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict[str, Any]:
        c = self.conn()
        one = lambda q: c.execute(q).fetchone()[0]
        links = self.links()
        now = datetime.now(timezone.utc)
        return {
            "links": len(links),
            "links_due": sum(1 for l in links if l.is_due(now)),
            "links_gone": sum(1 for l in links if l.status == "gone"),
            "systems": one("SELECT COUNT(*) FROM systems"),
            "documents": one("SELECT COUNT(*) FROM documents"),
            "chunks": one("SELECT COUNT(*) FROM chunks"),
            "embedded": one("SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL"),
            "needs_ocr": one("SELECT COUNT(*) FROM documents WHERE needs_ocr=1"),
            "flagged_docs": one("SELECT COUNT(*) FROM documents WHERE flagged>0"),
            "stale": one("SELECT COUNT(*) FROM documents WHERE indexed IS NULL OR text_updated > indexed"),
            "embed_signature": self.get_meta("embed_signature", ""),
        }
