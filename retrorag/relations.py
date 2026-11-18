"""Links between documents, and suggestions for new ones.

Why: search already connects text-rich documents about the same topic. Explicit links help where it can't:
  * text-poor documents (schematics, pinouts, chip photos) that OCR can't describe,
  * different vocabulary (emulator source code vs. a prose manual),
  * authority (errata, newer revisions) and
  * multi-step questions (pinout -> datasheet -> KERNAL listing).
Links are shown to the AI next to search hits (titles only - cheap), through the MCP tool get_related, in each
document's indexed header, and in exports.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np

from .library import Document, Library, now_iso
from .textutil import HEX_RE, strip_markers

# kind -> (label from the source's point of view, label from the target's point of view)
KINDS: dict[str, tuple[str, str]] = {
    "related": ("Related to", "Related to"),
    "pinout_for": ("Pinout / schematic for", "Pinout / schematic in"),
    "datasheet_for": ("Datasheet for", "Datasheet in"),
    "implements": ("Implements (source code)", "Implemented in (source code)"),
    "errata_for": ("Errata / corrections for", "Corrected by"),
    "revision_of": ("Newer revision of", "Superseded by"),
    "part_of": ("Part / chapter of", "Includes"),
}
SYMMETRIC = {"related"}


class RelationError(ValueError):
    pass


# ============================================================================ storage
def add_relation(lib: Library, src: int, dst: int, kind: str = "related", note: str = "",
                 origin: str = "manual") -> int:
    if src == dst:
        raise RelationError("A document can't be linked to itself.")
    if kind not in KINDS:
        raise RelationError(f"Unknown link type '{kind}'.")
    c = lib.conn()
    if kind in SYMMETRIC:
        src, dst = min(src, dst), max(src, dst)
    existing = c.execute("SELECT id FROM relations WHERE ((src_doc_id=? AND dst_doc_id=?) OR (src_doc_id=? AND "
                         "dst_doc_id=?)) AND kind=?", (src, dst, dst, src, kind)).fetchone()
    if existing:
        if note:
            with c:
                c.execute("UPDATE relations SET note=? WHERE id=?", (note, existing["id"]))
        return existing["id"]
    with c:
        cur = c.execute("INSERT INTO relations(src_doc_id, dst_doc_id, kind, note, origin, created) "
                        "VALUES (?,?,?,?,?,?)", (src, dst, kind, note.strip(), origin, now_iso()))
        a, b = min(src, dst), max(src, dst)
        c.execute("DELETE FROM relation_dismissed WHERE a=? AND b=?", (a, b))
        # both documents' indexed headers mention their links -> re-index them
        c.execute("UPDATE documents SET text_updated=? WHERE id IN (?,?)", (now_iso(), src, dst))
    return cur.lastrowid


def remove_relation(lib: Library, rel_id: int) -> None:
    c = lib.conn()
    r = c.execute("SELECT src_doc_id, dst_doc_id FROM relations WHERE id=?", (rel_id,)).fetchone()
    with c:
        c.execute("DELETE FROM relations WHERE id=?", (rel_id,))
        if r:
            c.execute("UPDATE documents SET text_updated=? WHERE id IN (?,?)",
                      (now_iso(), r["src_doc_id"], r["dst_doc_id"]))


def dismiss(lib: Library, a: int, b: int) -> None:
    with lib.conn() as c:
        c.execute("INSERT OR IGNORE INTO relation_dismissed(a, b) VALUES (?,?)", (min(a, b), max(a, b)))


def related(lib: Library, doc_id: int) -> list[dict]:
    """Links of one document, each phrased from that document's point of view."""
    return related_map(lib, [doc_id]).get(doc_id, [])


def related_map(lib: Library, doc_ids: list[int]) -> dict[int, list[dict]]:
    if not doc_ids:
        return {}
    q = ",".join("?" * len(doc_ids))
    rows = lib.conn().execute(
        f"""SELECT r.id, r.src_doc_id, r.dst_doc_id, r.kind, r.note, r.origin,
                   ds.title AS src_title, dd.title AS dst_title, ss.name AS src_sys, sd.name AS dst_sys,
                   ds.category AS src_cat, dd.category AS dst_cat, ds.pages AS src_pages, dd.pages AS dst_pages
            FROM relations r JOIN documents ds ON ds.id=r.src_doc_id JOIN documents dd ON dd.id=r.dst_doc_id
            JOIN systems ss ON ss.id=ds.system_id JOIN systems sd ON sd.id=dd.system_id
            WHERE r.src_doc_id IN ({q}) OR r.dst_doc_id IN ({q})""", [*doc_ids, *doc_ids]).fetchall()
    out: dict[int, list[dict]] = {}
    wanted = set(doc_ids)
    for r in rows:
        fwd, inv = KINDS.get(r["kind"], (r["kind"], r["kind"]))
        if r["src_doc_id"] in wanted:
            out.setdefault(r["src_doc_id"], []).append(
                {"rel_id": r["id"], "doc_id": r["dst_doc_id"], "title": r["dst_title"], "system": r["dst_sys"],
                 "category": r["dst_cat"], "relation": fwd, "kind": r["kind"], "note": r["note"],
                 "origin": r["origin"]})
        if r["dst_doc_id"] in wanted:
            out.setdefault(r["dst_doc_id"], []).append(
                {"rel_id": r["id"], "doc_id": r["src_doc_id"], "title": r["src_title"], "system": r["src_sys"],
                 "category": r["src_cat"], "relation": inv, "kind": r["kind"], "note": r["note"],
                 "origin": r["origin"]})
    for v in out.values():
        v.sort(key=lambda x: (x["relation"], x["title"].lower()))
    return out


def describe(links: list[dict], limit: int = 12) -> str:
    """One line per link, used in the indexed document header and read_document output."""
    lines = []
    for l in links[:limit]:
        s = f"{l['relation']}: {l['title']} ({l['system']}, doc {l['doc_id']})"
        if l.get("note"):
            s += f" - {l['note']}"
        lines.append(s)
    if len(links) > limit:
        lines.append(f"... and {len(links) - limit} more")
    return "\n".join(lines)


# ============================================================================ fingerprints
CHIP_NAMES = ["SID", "VIC-II", "VIC", "CIA", "VIA", "PIA", "ACIA", "TED", "VDC", "REU", "ANTIC", "GTIA", "POKEY",
              "CTIA", "TIA", "RIOT", "ULA", "PPU", "APU", "Paula", "Agnus", "Denise", "Blitter", "CRTC", "PSG",
              "VDP", "PLA", "KERNAL", "BASIC", "DOS", "Kickstart", "BIOS"]
_NAMED_RE = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(n) for n in sorted(CHIP_NAMES, key=len, reverse=True))
                       + r")(?![A-Za-z0-9])")
_PART_RE = re.compile(r"(?<![\w$&])(Z80|Z8\d{3}|YM\d{4}|AY-?3-?\d{4}|SN76\d{3}|TMS\d{4}[A-Z]?|WD\d{4}|SAA\d{4}|MC6\d{3,4}|"
                      r"HD\d{5}|uPD\d{3,4}|i8\d{3}|680[0-6]0|65816|65C02|65CE02|6[0-9]{3}|8[0-9]{3})\b", re.I)
_DEC_ADDR_RE = re.compile(r"\b(4\d{4}|5\d{4}|6[0-5]\d{3})\b")


def fingerprint(text: str) -> set[str]:
    """Chip names / part numbers and memory-mapped register addresses mentioned in a text."""
    feats: set[str] = set()
    for m in _NAMED_RE.finditer(text):
        feats.add("chip:" + m.group(1).upper())
    for m in _PART_RE.finditer(text):
        feats.add("part:" + m.group(1).upper().replace("-", ""))
    for m in HEX_RE.finditer(text):
        h = (m.group(1) or m.group(2) or "").lower()
        if len(h) == 4 and (m.group(1) or re.search(r"\d", h)):
            feats.add("addr:" + h)
            feats.add("page:" + h[:2])
    for m in _DEC_ADDR_RE.finditer(text):
        v = int(m.group(1))
        if 40960 <= v <= 65535:          # decimal POKE/PEEK addresses -> same features as their hex form
            h = f"{v:04x}"
            feats.add("addr:" + h)
            feats.add("page:" + h[:2])
    return feats


def update_features(lib: Library, doc_ids: list[int]) -> None:
    """Recompute fingerprint + mean embedding for documents (called after (re)indexing)."""
    c = lib.conn()
    for did in doc_ids:
        d = lib.document(did)
        if not d:
            continue
        rows = c.execute("SELECT ord, text, embedding FROM chunks WHERE doc_id=?", (did,)).fetchall()
        body = [r for r in rows if r["ord"] > 0]
        text = " ".join(r["text"] for r in body) + f" {d.title} {d.tags} {d.notes}"
        feats = fingerprint(strip_markers(text))
        vecs = [np.frombuffer(r["embedding"], dtype=np.float32) for r in body if r["embedding"]]
        if not vecs:   # text-poor document (schematic, pinout...): use its header - title, tags and notes
            vecs = [np.frombuffer(r["embedding"], dtype=np.float32) for r in rows if r["ord"] == 0 and r["embedding"]]
        vec = None
        if vecs and len({v.shape for v in vecs}) == 1:
            m = np.mean(vecs, axis=0)
            n = np.linalg.norm(m)
            vec = (m / n).astype(np.float32).tobytes() if n else None
        with c:
            c.execute("INSERT INTO doc_features(doc_id, features, vec, updated) VALUES (?,?,?,?) "
                      "ON CONFLICT(doc_id) DO UPDATE SET features=excluded.features, vec=excluded.vec, "
                      "updated=excluded.updated", (did, " ".join(sorted(feats)), vec, now_iso()))


def ensure_features(lib: Library, log=print) -> int:
    """Fill in fingerprints for indexed documents that don't have one yet (e.g. libraries from older versions)."""
    rows = lib.conn().execute("""SELECT d.id FROM documents d LEFT JOIN doc_features f ON f.doc_id=d.id
                                 WHERE d.indexed IS NOT NULL AND (f.doc_id IS NULL OR f.updated < d.indexed)""").fetchall()
    ids = [r[0] for r in rows]
    if ids:
        log(f"Fingerprinting {len(ids)} document(s) for link suggestions...")
        update_features(lib, ids)
    return len(ids)


# ============================================================================ suggestions
SIM_BASE = 0.70      # similarity below this adds nothing
SIM_ALONE = 0.88     # similarity at/above this suggests a link even without shared chips/addresses


@dataclass
class Suggestion:
    src: int
    dst: int
    kind: str
    score: float
    reasons: list[str] = field(default_factory=list)
    src_title: str = ""
    dst_title: str = ""
    src_system: str = ""
    dst_system: str = ""

    def reason_text(self) -> str:
        return "; ".join(self.reasons)


def _guess_kind(a: Document, b: Document) -> tuple[str, int, int]:
    """Pick a link type (and direction) from the two documents' categories / kinds."""
    def pin(d): return d.category in ("Pinouts", "Schematics")
    def sheet(d): return d.category == "Chip Datasheets"
    def code(d): return d.kind == "code" or d.category in ("Emulator Source", "Firmware & ROMs")
    for test, kind in ((pin, "pinout_for"), (sheet, "datasheet_for"), (code, "implements")):
        if test(a) and not test(b):
            return kind, a.id, b.id
        if test(b) and not test(a):
            return kind, b.id, a.id
    return "related", min(a.id, b.id), max(a.id, b.id)


def _pretty_feature(f: str) -> str:
    kind, _, v = f.partition(":")
    if kind == "addr":
        return f"${v.upper()}"
    if kind == "page":
        return f"${v.upper()}xx"
    return v


def suggest(lib: Library, doc_id: int | None = None, limit: int = 50, min_score: float = 1.5,
            log=print) -> list[Suggestion]:
    """Suggest links for one document (doc_id) or across the whole library.

    Score = rarity-weighted shared chip names/part numbers/register addresses + content similarity
    (mean embeddings). Code-to-code pairs are skipped (files of one source tree are trivially related),
    as are existing links and dismissed suggestions."""
    ensure_features(lib, log)
    c = lib.conn()
    rows = c.execute("SELECT doc_id, features, vec FROM doc_features").fetchall()
    if not rows:
        return []
    docs = {d.id: d for d in lib.documents()}
    systems = {s.id: s.name for s in lib.systems()}
    feats = {r["doc_id"]: set(r["features"].split()) for r in rows if r["doc_id"] in docs}
    n = max(1, len(feats))
    df: dict[str, int] = {}
    for fs in feats.values():
        for f in fs:
            df[f] = df.get(f, 0) + 1
    idf = {f: math.log(1 + n / k) for f, k in df.items()}

    # embedding matrix (only docs with a vector of the common dimension)
    vec_rows = [(r["doc_id"], np.frombuffer(r["vec"], dtype=np.float32)) for r in rows if r["vec"] and r["doc_id"] in docs]
    dim = max((v.shape[0] for _, v in vec_rows), default=0)
    vec_rows = [(i, v) for i, v in vec_rows if v.shape[0] == dim]
    vid = {i: k for k, (i, _) in enumerate(vec_rows)}
    mat = np.vstack([v for _, v in vec_rows]) if vec_rows else None

    linked = set()
    for r in c.execute("SELECT src_doc_id, dst_doc_id FROM relations"):
        linked.add((min(r[0], r[1]), max(r[0], r[1])))
    dismissed = {(r[0], r[1]) for r in c.execute("SELECT a, b FROM relation_dismissed")}

    # inverted index on informative features: skip ones in (nearly) every document, e.g. 'BASIC' in a C64 library
    cap = max(5, n * 0.2)
    inv: dict[str, list[int]] = {}
    for did, fs in feats.items():
        for f in fs:
            if df[f] <= cap and (df[f] < n or n < 3):
                inv.setdefault(f, []).append(did)

    sources = [doc_id] if doc_id is not None else [d for d in feats if docs[d].kind != "code"]
    out: dict[tuple[int, int], Suggestion] = {}
    for a in sources:
        if a not in docs:
            continue
        cands: dict[int, float] = {}
        for f in feats.get(a, ()):
            for b in inv.get(f, ()):
                if b != a:
                    cands[b] = cands.get(b, 0.0) + idf[f]
        sims: dict[int, float] = {}
        if mat is not None and a in vid:
            s = mat @ mat[vid[a]]
            top = np.argsort(-s)[:40]
            for k in top:
                b = vec_rows[k][0]
                if b != a and s[k] >= SIM_ALONE:
                    sims[b] = float(s[k])
                    cands.setdefault(b, 0.0)
        for b, fscore in cands.items():
            pair = (min(a, b), max(a, b))
            if pair in linked or pair in dismissed or pair in out:
                continue
            da, db = docs[a], docs[b]
            if da.kind == "code" and db.kind == "code":
                continue
            sim = sims.get(b)
            if sim is None and mat is not None and a in vid and b in vid:
                sim = float(mat[vid[a]] @ mat[vid[b]])
            # embeddings rate everything about one machine as fairly similar, so similarity alone only counts
            # when it's very high; otherwise it just boosts pairs that share chips/addresses
            if fscore <= 0 and (sim or 0) < SIM_ALONE:
                continue
            score = fscore + max(0.0, (sim or 0) - SIM_BASE) * 10
            if score < min_score:
                continue
            shared = sorted((feats[a] & feats[b]) & set(inv), key=lambda f: -idf[f])
            # don't list both an address and its page
            shown, pages = [], {f[5:7] for f in shared if f.startswith("addr:")}
            for f in shared:
                if f.startswith("page:") and f[5:] in pages:
                    continue
                shown.append(_pretty_feature(f))
            reasons = []
            if shown:
                reasons.append("both mention " + ", ".join(shown[:6]) + (" …" if len(shown) > 6 else ""))
            if sim and sim > SIM_BASE + 0.05:
                reasons.append(f"similar content ({sim:.2f})")
            kind, src, dst = _guess_kind(da, db)
            out[pair] = Suggestion(src, dst, kind, round(score, 2), reasons, docs[src].title, docs[dst].title,
                                   systems.get(docs[src].system_id, ""), systems.get(docs[dst].system_id, ""))
    res = sorted(out.values(), key=lambda s: -s.score)
    return res[:limit]
