"""A page of a scan or PDF, or a picture, as an image an AI can look at (MCP view_page).

OCR gives the words on a page but not drawings, chip markings on a board photo, jumper settings or layout, so an
AI that can see images (Claude, and vision models in clients that pass tool images on) can look at the page
itself. Pages are sized for that: at most 1568 pixels on the long side and about 1.15 megapixels, which is what
Claude reads at full detail (anything bigger is scaled down by the model anyway, and costs more). A region
zooms into part of the page, rendered sharper, so small print and chip numbers can be read.
"""
from __future__ import annotations

import io
import math

from .library import Document, Library

MAX_SIDE = 1568
MAX_PIXELS = 1_150_000
MAX_PDF_DPI = 600
MIN_ZOOM_SIDE = 1000      # a zoomed region smaller than this (long side) is enlarged towards it


class PictureError(Exception):
    pass


def viewable(d: Document | None) -> bool:
    return bool(d) and d.kind in ("pdf", "image")


def _region(region) -> tuple[float, float, float, float] | None:
    if region is None or (isinstance(region, (list, tuple)) and len(region) == 0):
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in region)
    except (TypeError, ValueError):
        raise PictureError("region must be four numbers: [left, top, right, bottom] as fractions of the page (0-1)")
    x0, y0, x1, y1 = (min(1.0, max(0.0, v)) for v in (x0, y0, x1, y1))
    if x1 - x0 < 0.02 or y1 - y0 < 0.02:
        raise PictureError("region is too small or back to front: give [left, top, right, bottom] with left < right "
                           "and top < bottom, as fractions of the page (0-1)")
    return x0, y0, x1, y1


def _fit(w: int, h: int) -> float:
    return min(1.0, MAX_SIDE / max(w, h), math.sqrt(MAX_PIXELS / max(1, w * h)))


def render(lib: Library, d: Document, page: int = 1, region=None) -> tuple[bytes, dict]:
    """(JPEG bytes, details) for one page of a PDF / scan or one picture, optionally zoomed into a region."""
    from PIL import Image
    if not viewable(d):
        raise PictureError(f"'{d.title}' isn't a scan, PDF or picture (it's {d.kind}) - use read_document for its text"
                           if d else "No such document")
    box = _region(region)
    src = lib.abspath(d.path)
    if not src.exists():
        raise PictureError(f"The file for '{d.title}' is missing ({src.name})")
    if d.kind == "pdf":
        import pymupdf
        with pymupdf.open(src) as doc:
            pages = doc.page_count
            if not 1 <= page <= pages:
                raise PictureError(f"'{d.title}' has {pages} page(s); page {page} doesn't exist")
            pg = doc[page - 1]
            r = pg.rect
            clip = r if not box else pymupdf.Rect(r.x0 + box[0] * r.width, r.y0 + box[1] * r.height,
                                                   r.x0 + box[2] * r.width, r.y0 + box[3] * r.height)
            longest = max(clip.width, clip.height)
            cap = max(MAX_PDF_DPI / 72, MIN_ZOOM_SIDE / longest) if box else MAX_PDF_DPI / 72
            zoom = min(cap, MAX_SIDE / longest, math.sqrt(MAX_PIXELS / max(1.0, clip.width * clip.height)))
            pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
            im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            full = (round(r.width / 72 * 300), round(r.height / 72 * 300))      # its size scanned at 300 dpi
    else:
        with Image.open(src) as f:
            pages = getattr(f, "n_frames", 1)
            if not 1 <= page <= pages:
                raise PictureError(f"'{d.title}' has {pages} page(s); page {page} doesn't exist")
            f.seek(page - 1)
            try:
                from PIL import ImageOps
                im = ImageOps.exif_transpose(f) or f               # phone photos: turn them the right way up
            except Exception:
                im = f
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
                im = Image.alpha_composite(bg, im)
            im = im.convert("RGB")
            full = im.size
            if box:
                w, h = im.size
                im = im.crop((round(box[0] * w), round(box[1] * h), round(box[2] * w), round(box[3] * h)))
    s = _fit(*im.size)
    if box and s >= 1.0 and d.kind == "image":
        # a zoomed-in part of a small picture: enlarge it (up to 3x) so small print is big enough to read
        s = min(3.0, MIN_ZOOM_SIDE / max(im.size), s * 3) if max(im.size) < MIN_ZOOM_SIDE else 1.0
        s = min(s, _fit(round(im.width * s), round(im.height * s)) * s)
    if s != 1.0:
        im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.LANCZOS)
    import numpy as np
    a = np.asarray(im.resize((min(64, im.width), min(64, im.height))), dtype=np.int16)
    if np.abs(a[..., 0] - a[..., 1]).max() < 12 and np.abs(a[..., 1] - a[..., 2]).max() < 12:
        im = im.convert("L")                                        # black-and-white scan: smaller, same detail
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85, optimize=True)
    s_ = lib.system(d.system_id)
    info = {"doc_id": d.id, "title": d.title, "system": s_.name if s_ else "", "category": d.category,
            "page": page, "pages": pages, "shown_size": list(im.size), "full_size": list(full)}
    if box:
        info["region"] = [round(v, 3) for v in box]
    return buf.getvalue(), info


def caption(info: dict) -> str:
    where = f"page {info['page']} of {info['pages']}" if info["pages"] > 1 else "the picture"
    zoom = (f", zoomed into region {info['region']}" if info.get("region") else "")
    text = (f"{info['system']} / {info['title']} ({info['category']}) - {where}{zoom}, "
            f"shown at {info['shown_size'][0]}x{info['shown_size'][1]} pixels.")
    if not info.get("region"):
        text += (" To read small print or chip markings, look again with region=[left, top, right, bottom] "
                 "(fractions of the page, e.g. [0.5, 0, 1, 0.5] = top-right quarter) - it is shown sharper.")
    text += (" If no image follows this text, your chat app doesn't pass pictures from tools to the model: use "
             "read_document for the page's OCR text instead.")
    return text
