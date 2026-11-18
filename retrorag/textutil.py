"""Text helpers shared by ingest, OCR, chunking, editor and servers."""
from __future__ import annotations

import html
import re
from pathlib import Path

# ---------------------------------------------------------------- low-confidence OCR markers
# A word Tesseract was unsure about is written as  {?w0rd|42?}  (text | confidence 0-100).
# The editor highlights these; "accept" replaces the marker with its (corrected) inner text.
# Markers are stripped before anything is indexed, so they never leak into the RAG.
MARKER_RE = re.compile(r"\{\?(.*?)\|(\d{1,3})\?\}")
PAGE_RE = re.compile(r"^<!--\s*page\s+(\d+)\s*-->\s*$", re.M | re.I)
COMMENT_LINE_RE = re.compile(r"^\s*<!--.*-->\s*$")


def make_marker(word: str, conf: float) -> str:
    return "{?" + word + "|" + str(int(round(conf))) + "?}"


def strip_markers(text: str) -> str:
    return MARKER_RE.sub(lambda m: m.group(1), text)


def count_markers(text: str) -> int:
    return sum(1 for _ in MARKER_RE.finditer(text))


def page_marker(n: int) -> str:
    return f"<!-- page {n} -->"


def split_pages(text: str) -> list[tuple[int | None, str]]:
    """Split text on <!-- page N --> markers -> [(page, body)]. Text before the first marker gets page None."""
    out: list[tuple[int | None, str]] = []
    pos, cur = 0, None
    for m in PAGE_RE.finditer(text):
        body = text[pos:m.start()]
        if body.strip() or cur is not None:
            out.append((cur, body))
        cur, pos = int(m.group(1)), m.end()
    tail = text[pos:]
    if tail.strip() or cur is not None:
        out.append((cur, tail))
    return out


def join_pages(pages: list[tuple[int | None, str]]) -> str:
    parts = []
    for n, body in pages:
        body = body.strip("\n")
        parts.append(body if n is None else f"{page_marker(n)}\n{body}")
    return "\n\n".join(parts).rstrip() + "\n"


# ---------------------------------------------------------------- file reading
def looks_binary(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data[:8192]:
        return True
    sample = data[:8192]
    printable = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13) or b >= 0x80)
    return printable / len(sample) < 0.85


def decode_bytes(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def read_text_file(p: Path) -> str:
    return decode_bytes(Path(p).read_bytes()).replace("\r\n", "\n").replace("\r", "\n")


def html_to_text(src: str) -> str:
    src = re.sub(r"(?is)<(script|style).*?</\1>", "", src)
    src = re.sub(r"(?i)<br\s*/?>", "\n", src)
    src = re.sub(r"(?i)</(p|div|h\d|li|tr|pre|table)>", "\n\n", src)
    src = re.sub(r"(?i)<h(\d)[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", src)
    src = re.sub(r"(?i)<li[^>]*>", "- ", src)
    src = re.sub(r"<[^>]+>", "", src)
    src = html.unescape(src)
    src = re.sub(r"[ \t]+\n", "\n", src)
    return re.sub(r"\n{3,}", "\n\n", src).strip() + "\n"


# ---------------------------------------------------------------- retro-friendly keyword normalisation
# Hex addresses are written many ways: $D020, 0xD020, D020h, &D020, D020H.  The FTS tokenizer drops '$'/'&'
# but keeps '0x' and 'h', so we add a normalised form ('d020') of every hex literal to a keywords column
# and apply the same normalisation to queries.
HEX_RE = re.compile(r"(?:\$|0x|&)([0-9A-Fa-f]{2,8})\b|\b([0-9A-Fa-f]{2,8})[hH]\b")


def hex_keywords(text: str) -> str:
    seen: dict[str, None] = {}
    for m in HEX_RE.finditer(text):
        h = (m.group(1) or m.group(2)).lower()
        if m.group(2) and not re.search(r"\d", h):   # skip words like 'each' -> 'eac'+'h'
            continue
        seen.setdefault(h, None)
    return " ".join(seen)


def normalise_query_hex(q: str) -> str:
    def rep(m: re.Match) -> str:
        h = m.group(1) or m.group(2)
        if m.group(2) and not re.search(r"\d", h):
            return m.group(0)
        return h
    return HEX_RE.sub(rep, q)
