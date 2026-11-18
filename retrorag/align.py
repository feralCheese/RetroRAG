"""Keep proofread text in step with the OCR word boxes on the scan.

The editor's text is edited freely (words corrected, deleted, inserted; whole lines removed), so mapping
"line 7, word 3" to the OCR result breaks after the first deletion. Instead, the words of a page's text are
aligned to the words Tesseract originally read by *content* (difflib + similarity-based pairing inside changed
regions). This works across sessions too, because it only needs the saved text and the .ocr.json.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from .textutil import COMMENT_LINE_RE, PAGE_RE

TOKEN_RE = re.compile(r"\{\?(.*?)\|(\d{1,3})\?\}|\S+")


@dataclass
class Tok:
    start: int            # absolute position of the token in the document text
    end: int
    inner_start: int      # where the word itself starts ({?word|NN?} -> position of 'word')
    inner_end: int
    text: str             # the word (marker unwrapped)
    marker: bool          # still flagged as low-confidence
    ref: tuple | None = None    # (line index, word index) in the page's OCR json, None for text the user added


def page_tokens(text: str, lo: int, hi: int) -> list[Tok]:
    """Words of text[lo:hi], skipping page markers and comment lines."""
    toks: list[Tok] = []
    pos = lo
    for line in text[lo:hi].split("\n"):
        if line.strip() and not PAGE_RE.match(line) and not COMMENT_LINE_RE.match(line):
            for m in TOKEN_RE.finditer(line):
                marker = m.group(2) is not None
                if marker:
                    toks.append(Tok(pos + m.start(), pos + m.end(), pos + m.start(1), pos + m.end(1), m.group(1), True))
                else:
                    toks.append(Tok(pos + m.start(), pos + m.end(), pos + m.start(), pos + m.end(), m.group(0), False))
        pos += len(line) + 1
    return toks


def _sim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a.lower(), b.lower(), autojunk=False).ratio()


def _pair_block(toks: list[Tok], words: list[tuple], min_sim: float = 0.34) -> list[tuple[int, int]]:
    """Monotonic best pairing of a changed region (corrections, deletions, insertions) by string similarity."""
    n, m = len(toks), len(words)
    if n == m and n <= 2:                 # a straight correction of one or two words
        return [(i, i) for i in range(n)]
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    move = [[0] * (m + 1) for _ in range(n + 1)]   # 0 = pair, 1 = skip token, 2 = skip word
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            s = _sim(toks[i].text, words[j][2]["t"])
            pair = (s + best[i + 1][j + 1]) if s >= min_sim else -1.0
            opts = (pair, best[i + 1][j], best[i][j + 1])
            k = max(range(3), key=lambda x: opts[x])
            best[i][j], move[i][j] = opts[k], k
    out, i, j = [], 0, 0
    while i < n and j < m:
        if move[i][j] == 0:
            out.append((i, j)); i += 1; j += 1
        elif move[i][j] == 1:
            i += 1
        else:
            j += 1
    return out


def align(toks: list[Tok], page_json: dict) -> list[Tok]:
    """Fill Tok.ref for every token that corresponds to an OCR word on the page."""
    words = [(li, wi, w) for li, line in enumerate(page_json.get("lines", [])) for wi, w in enumerate(line["words"])]
    if not toks or not words:
        return toks
    if len(toks) * len(words) > 25_000_000:          # absurdly large page: fall back to order
        for t, (li, wi, _w) in zip(toks, words):
            t.ref = (li, wi)
        return toks
    sm = difflib.SequenceMatcher(None, [t.text for t in toks], [w[2]["t"] for w in words], autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                toks[i1 + k].ref = words[j1 + k][:2]
        elif tag == "replace" and (i2 - i1) * (j2 - j1) <= 40_000:
            for a, b in _pair_block(toks[i1:i2], words[j1:j2]):
                toks[i1 + a].ref = words[j1 + b][:2]
    return toks


def token_at(toks: list[Tok], pos: int, line_lo: int, line_hi: int) -> Tok | None:
    """Token under the cursor; at a gap, the nearest token on the same line (prefer the one after)."""
    on_line = [t for t in toks if t.start >= line_lo and t.end <= line_hi]
    for t in on_line:
        if t.start <= pos <= t.end:
            return t
    after = [t for t in on_line if t.start >= pos]
    if after:
        return after[0]
    return on_line[-1] if on_line else None
