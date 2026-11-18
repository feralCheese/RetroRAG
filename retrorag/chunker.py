"""Split document text into retrieval chunks that keep page numbers and heading context."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .textutil import COMMENT_LINE_RE, split_pages, strip_markers

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


@dataclass
class Chunk:
    text: str
    page: int | None
    heading: str


def _paragraphs(body: str) -> list[str]:
    paras, cur = [], []
    for line in body.split("\n"):
        if COMMENT_LINE_RE.match(line):
            continue
        if line.strip():
            cur.append(line.rstrip())
        elif cur:
            paras.append("\n".join(cur)); cur = []
    if cur:
        paras.append("\n".join(cur))
    return paras


def _hard_split(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):   # break at whitespace when possible
            ws = text.rfind("\n", start + size // 2, end)
            ws = ws if ws > 0 else text.rfind(" ", start + size // 2, end)
            end = ws if ws > 0 else end
        out.append(text[start:end].strip())
        start = end
    return [o for o in out if o]


def chunk_prose(text: str, size: int = 1200, overlap: int = 200) -> list[Chunk]:
    """Paragraph-packing chunker. Chunks never span pages (so citations are exact); a heading starts a new
    chunk once the current one is reasonably full; consecutive chunks overlap by whole paragraphs."""
    text = strip_markers(text)
    chunks: list[Chunk] = []
    headings: dict[int, str] = {}

    def hpath() -> str:
        return " > ".join(headings[k] for k in sorted(headings))

    for page, body in split_pages(text):
        buf: list[str] = []
        buf_len, fresh, buf_heading = 0, 0, hpath()   # fresh = pieces not carried over as overlap
        for para in _paragraphs(body):
            m = HEADING_RE.match(para.split("\n", 1)[0])
            if m:
                if fresh and buf_len > size * 0.4:
                    chunks.append(Chunk("\n\n".join(buf), page, buf_heading))
                    fresh = 0
                if not fresh:
                    buf, buf_len = [], 0
                level = len(m.group(1))
                for k in [k for k in headings if k >= level]:
                    del headings[k]
                headings[level] = m.group(2)[:120]
                if not fresh:
                    buf_heading = hpath()
            for piece in _hard_split(para, size):
                if fresh and buf_len + len(piece) > size:
                    chunks.append(Chunk("\n\n".join(buf), page, buf_heading))
                    keep, kept = [], 0
                    for p in reversed(buf):
                        if kept + len(p) > overlap:
                            break
                        keep.insert(0, p); kept += len(p) + 2
                    buf, buf_len, fresh, buf_heading = keep, kept, 0, hpath()
                buf.append(piece); buf_len += len(piece) + 2; fresh += 1
        if fresh:
            chunks.append(Chunk("\n\n".join(buf), page, buf_heading))
    return [c for c in chunks if c.text.strip()]


def chunk_code(text: str, path: str, lines_per_chunk: int = 60, max_chars: int = 2400) -> list[Chunk]:
    lines = text.split("\n")
    chunks, i = [], 0
    overlap = max(3, lines_per_chunk // 10)
    while i < len(lines):
        end = min(len(lines), i + lines_per_chunk)
        piece = "\n".join(lines[i:end])
        while len(piece) > max_chars and end - i > 5:
            end = i + (end - i) // 2
            piece = "\n".join(lines[i:end])
        if piece.strip():
            chunks.append(Chunk(piece, None, f"{path}:{i + 1}-{end}"))
        if end >= len(lines):
            break
        i = max(end - overlap, i + 1)
    return chunks
