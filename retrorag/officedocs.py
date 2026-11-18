"""Word and Excel documents -> readable Markdown. Pure Python: Microsoft Office is NOT needed.

  .docx / .docm   Office Open XML, read directly (headings, lists, tables)
  .doc            Word 97-2003 binary, text recovered from the document's piece table (olefile)
  .xlsx / .xlsm   each sheet becomes a Markdown table (openpyxl, cached cell values)
  .xls            Excel 97-2003 (xlrd)
"""
from __future__ import annotations

import re
import struct
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

WORD_EXT = {".docx", ".docm", ".dotx", ".doc", ".dot"}
EXCEL_EXT = {".xlsx", ".xlsm", ".xltx", ".xls"}
OFFICE_EXT = WORD_EXT | EXCEL_EXT
MAX_ROWS = 20000          # per sheet

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# Word binary control characters
CELL_MARK = "\x07"        # end of a table cell (two in a row = end of a table row)
FIELD_START, FIELD_SEP, FIELD_END = "\x13", "\x14", "\x15"
PARA_END = "\r"


def office_to_markdown(path: Path) -> str:
    ext = Path(path).suffix.lower()
    if ext in (".docx", ".docm", ".dotx"):
        return docx_to_markdown(path)
    if ext in (".doc", ".dot"):
        return doc_to_markdown(path)
    if ext in (".xlsx", ".xlsm", ".xltx"):
        return xlsx_to_markdown(path)
    if ext == ".xls":
        return xls_to_markdown(path)
    raise ValueError(f"Not an Office format: {ext}")


# ============================================================================ tables
def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).replace("|", "\\|").replace("\n", " ").strip()


def table_markdown(rows: list[list]) -> str:
    rows = [[_cell(c) for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    while width and not any(len(r) >= width and r[width - 1] for r in rows):   # trim empty right columns
        width -= 1
    rows = [(r + [""] * width)[:width] for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


# ============================================================================ Word .docx
def _para_text(p) -> str:
    parts = []
    for el in p.iter():
        tag = el.tag
        if tag == _W + "t":
            parts.append(el.text or "")
        elif tag == _W + "tab":
            parts.append("\t")
        elif tag in (_W + "br", _W + "cr"):
            parts.append("\n")
    return "".join(parts)


def _heading_level(p) -> int:
    ppr = p.find(_W + "pPr")
    if ppr is None:
        return 0
    st = ppr.find(_W + "pStyle")
    name = (st.get(_W + "val") if st is not None else "") or ""
    if name.lower() == "title":
        return 1
    m = re.match(r"(?i)heading\s*(\d)", name)
    if m:
        return min(6, int(m.group(1)))
    ol = ppr.find(_W + "outlineLvl")
    if ol is not None and (ol.get(_W + "val") or "").isdigit():
        return min(6, int(ol.get(_W + "val")) + 1)
    return 0


def docx_to_markdown(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(_W + "body")
    out: list[str] = []
    for el in list(body) if body is not None else []:
        if el.tag == _W + "p":
            text = _para_text(el).strip()
            if not text:
                continue
            lvl = _heading_level(el)
            ppr = el.find(_W + "pPr")
            listed = ppr is not None and ppr.find(_W + "numPr") is not None
            out.append(("#" * lvl + " " + text) if lvl else (("- " + text) if listed else text))
        elif el.tag == _W + "tbl":
            rows = []
            for tr in el.iter(_W + "tr"):
                rows.append([" ".join(_para_text(p).strip() for p in tc.iter(_W + "p")).strip()
                             for tc in tr.findall(_W + "tc")])
            md = table_markdown(rows)
            if md:
                out.append(md)
    return "\n\n".join(out) + "\n"


# ============================================================================ Word 97-2003 .doc
def doc_to_markdown(path: Path) -> str:
    """Text of a Word 97-2003 document via its piece table (CLX); falls back to scanning for text runs
    (e.g. Word 6/95 files, which use an older layout)."""
    import olefile
    with olefile.OleFileIO(str(path)) as ole:
        wd = ole.openstream("WordDocument").read()
        flags = struct.unpack_from("<H", wd, 0x0A)[0]
        table_name = "1Table" if flags & 0x0200 else "0Table"
        try:
            table = ole.openstream(table_name).read()
            fc_clx, lcb_clx = struct.unpack_from("<II", wd, 0x01A2)
            text = _doc_pieces(wd, table[fc_clx:fc_clx + lcb_clx])
        except Exception:
            text = _doc_fallback(wd)
    return _clean_doc_text(text)


def _doc_pieces(wd: bytes, clx: bytes) -> str:
    i = 0
    while i < len(clx) and clx[i] == 0x01:          # skip Prc entries (property modifiers)
        cb = struct.unpack_from("<H", clx, i + 1)[0]
        i += 3 + cb
    if i >= len(clx) or clx[i] != 0x02:
        raise ValueError("no piece table")
    lcb = struct.unpack_from("<I", clx, i + 1)[0]
    plc = clx[i + 5:i + 5 + lcb]
    n = (lcb - 4) // 12                             # (n+1) CPs of 4 bytes + n PCDs of 8 bytes
    cps = struct.unpack_from(f"<{n + 1}I", plc, 0)
    parts = []
    for k in range(n):
        pcd = plc[4 * (n + 1) + 8 * k: 4 * (n + 1) + 8 * (k + 1)]
        fc = struct.unpack_from("<I", pcd, 2)[0]
        chars = cps[k + 1] - cps[k]
        if fc & 0x40000000:                         # compressed: 8-bit cp1252 at fc/2
            off = (fc & ~0x40000000) // 2
            parts.append(wd[off:off + chars].decode("cp1252", errors="replace"))
        else:                                       # UTF-16LE
            parts.append(wd[fc:fc + 2 * chars].decode("utf-16-le", errors="replace"))
    return "".join(parts)


def _doc_fallback(wd: bytes) -> str:
    runs = re.findall(rb"[\x20-\x7e\r\t\x80-\xff]{20,}", wd)
    return PARA_END.join(r.decode("cp1252", errors="replace") for r in runs)


def _printable(s: str, keep: str = "\t") -> str:
    return "".join(ch for ch in s if ord(ch) >= 32 or ch in keep)


def _clean_doc_text(text: str) -> str:
    """Word control characters -> Markdown: fields keep their result, cell marks become table rows."""
    field_with_result = re.escape(FIELD_START) + r"[^\x13\x14\x15]*" + re.escape(FIELD_SEP) + r"([^\x13\x15]*)" + re.escape(FIELD_END)
    text = re.sub(field_with_result, r"\1", text)
    text = re.sub(re.escape(FIELD_START) + r"[^\x13\x15]*" + re.escape(FIELD_END), "", text)
    text = text.replace(CELL_MARK + CELL_MARK, CELL_MARK + PARA_END)    # cell mark + row mark = end of row
    blocks: list[list[str]] = []          # each block is one paragraph, or all the rows of one table
    for para in text.split(PARA_END):
        para = para.replace("\x0b", "\n").replace("\x0c", "\n").replace("\x1e", "-").replace("\x1f", "")
        if CELL_MARK in para:
            cells = [_printable(c).strip().replace("|", "\\|") for c in para.split(CELL_MARK)]
            if cells and cells[-1] == "":
                cells = cells[:-1]
            row = "| " + " | ".join(cells) + " |"
            if blocks and blocks[-1][0].startswith("| "):
                if len(blocks[-1]) == 1:                  # header separator after the first row
                    blocks[-1].append("|" + "---|" * len(cells))
                blocks[-1].append(row)
            else:
                blocks.append([row])
            continue
        para = _printable(para, "\n\t").strip()
        if para:
            blocks.append([para])
    md = "\n\n".join("\n".join(b) for b in blocks)
    return re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"


# ============================================================================ Excel
def xlsx_to_markdown(path: Path) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(str(path), read_only=True, data_only=True)
    out = []
    try:
        for ws in wb.worksheets:
            rows, truncated = [], False
            for k, r in enumerate(ws.iter_rows(values_only=True)):
                if k >= MAX_ROWS:
                    truncated = True
                    break
                rows.append(list(r))
            md = table_markdown(rows)
            if md:
                out.append(f"## Sheet: {ws.title}\n\n{md}" + (f"\n\n(first {MAX_ROWS} rows only)" if truncated else ""))
    finally:
        wb.close()
    return "\n\n".join(out) + "\n"


def xls_to_markdown(path: Path) -> str:
    import xlrd
    book = xlrd.open_workbook(str(path), on_demand=True)
    out = []
    try:
        for sh in book.sheets():
            rows = [sh.row_values(i) for i in range(min(sh.nrows, MAX_ROWS))]
            md = table_markdown(rows)
            if md:
                out.append(f"## Sheet: {sh.name}\n\n{md}" + (f"\n\n(first {MAX_ROWS} rows only)" if sh.nrows > MAX_ROWS else ""))
    finally:
        book.release_resources()
    return "\n\n".join(out) + "\n"
