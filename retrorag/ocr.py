"""OCR with Tesseract, producing editable markdown with low-confidence markers.

Output for a document  foo.pdf:
    foo.pdf.md        <!-- page N --> blocks; uncertain words written as {?w0rd|42?}
    foo.pdf.ocr.json  per page: image size + every OCR line and word with its bounding box and confidence.
                      The proofreading editor uses this to highlight the current line/word on the scan.
                      "binding": {"left"/"right": px} records page-edge strips blanked as binding marks.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Callable

from .library import Document, Library, now_iso
from .textutil import count_markers, join_pages, make_marker, split_pages

Log = Callable[[str], None]


class OCRError(RuntimeError):
    pass


from .platform_util import tesseract_candidates, tesseract_install_hint

INSTALL_HINT = tesseract_install_hint()


def find_tesseract(override: str = "") -> str | None:
    from .config import app_root
    cands = [override] if override else []
    # a Tesseract-OCR folder shipped next to the app (portable builds) wins over a system install
    cands.append(str(app_root() / "Tesseract-OCR" / ("tesseract.exe" if os.name == "nt" else "tesseract")))
    cands += tesseract_candidates()
    for c in cands:
        if c and Path(c).is_file():
            return str(Path(c))
    return None


def configure(lib: Library | None = None, app_override: str = "") -> str:
    import pytesseract
    override = (lib.settings.tesseract_path if lib else "") or app_override
    exe = find_tesseract(override)
    if not exe:
        raise OCRError(INSTALL_HINT)
    pytesseract.pytesseract.tesseract_cmd = exe
    return exe


def tesseract_info(app_override: str = "") -> dict:
    import pytesseract
    exe = find_tesseract(app_override)
    if not exe:
        return {"ok": False, "message": INSTALL_HINT}
    pytesseract.pytesseract.tesseract_cmd = exe
    try:
        return {"ok": True, "path": exe, "version": str(pytesseract.get_tesseract_version()),
                "languages": sorted(l for l in pytesseract.get_languages() if l != "osd")}
    except Exception as e:
        return {"ok": False, "message": f"Tesseract at {exe} failed: {e}"}


# ---------------------------------------------------------------------------- core
CLEANUP_CHOICES = [("auto", "Auto cleanup (default)"), ("none", "No cleanup (crisp scans, thin fonts)"),
                   ("light", "Light cleanup"), ("strong", "Strong cleanup (speckled photocopies)")]
AUTO_RETRY_BELOW = 80   # 'auto': pages scoring under this mean confidence are retried with strong cleanup


def prepare(img, upscale: bool = True, cleanup: str = "light"):
    """Grayscale; enlarge small images (Tesseract wants ~10px+ x-height); optional median denoise, which
    removes the speckle typical of photocopied/scanned retro manuals."""
    from PIL import Image, ImageFilter, ImageOps
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGBA", img.size, "white")
        bg.alpha_composite(img.convert("RGBA"))
        img = bg
    g = ImageOps.grayscale(img) if img.mode != "L" else img.copy()
    if upscale and g.width < 1600:
        f = min(3.0, 2000 / max(1, g.width))
        g = g.resize((int(g.width * f), int(g.height * f)), Image.LANCZOS)
    if cleanup == "light":
        g = ImageOps.autocontrast(g.filter(ImageFilter.MedianFilter(3)), cutoff=1)
    elif cleanup == "strong":
        g = ImageOps.autocontrast(g.filter(ImageFilter.MedianFilter(5)), cutoff=1)
    return g


def reference_height(words: list[dict], threshold: int) -> float:
    """Typical text height on a page: median height of confidently-read words (all words if too few)."""
    import statistics
    hs = [w["b"][3] - w["b"][1] for w in words if w["c"] >= threshold]
    if len(hs) < 3:
        hs = [w["b"][3] - w["b"][1] for w in words]
    return float(statistics.median(hs)) if hs else 0.0


def is_graphic_word(w: dict, ref_h: float, page_h: int, threshold: int) -> bool:
    """A low-confidence 'word' that is far too big for its letters is part of a drawing (a chip outline, a wire,
    a box), not text - e.g. a 2-letter 'Fs' whose box covers a whole IC symbol on a schematic."""
    if w["c"] >= threshold:
        return False
    h = w["b"][3] - w["b"][1]
    wpc = (w["b"][2] - w["b"][0]) / max(1, len(w["t"]))
    if not ref_h:                     # nothing readable to compare with: only a really huge box counts
        return bool(page_h) and h > 0.15 * page_h
    return h > 4.0 * ref_h or wpc > 4.0 * ref_h     # drawing shapes are ~10x text height; real words < 3x


def ocr_image(img, lang: str = "eng", psm: int = 3, threshold: int = 70,
              drop_graphics: bool = True) -> tuple[str, dict]:
    """OCR one page image -> (markdown body, page json)."""
    import pytesseract
    data = pytesseract.image_to_data(img, lang=lang, config=f"--psm {psm}", output_type=pytesseract.Output.DICT)
    lines: dict[tuple, dict] = {}
    order: list[tuple] = []
    for i, txt in enumerate(data["text"]):
        if data["level"][i] != 5:
            continue
        txt = (txt or "").strip()
        conf = float(data["conf"][i])
        if not txt or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        l, t, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        if key not in lines:
            lines[key] = {"words": []}
            order.append(key)
        lines[key]["words"].append({"t": txt, "c": round(conf, 1), "b": [l, t, l + w, t + h]})

    dropped = 0
    if drop_graphics:
        ref = reference_height([w for k in order for w in lines[k]["words"]], threshold)
        for k in order:
            keep = [w for w in lines[k]["words"] if not is_graphic_word(w, ref, img.height, threshold)]
            dropped += len(lines[k]["words"]) - len(keep)
            lines[k]["words"] = keep
        order = [k for k in order if lines[k]["words"]]

    md_lines, json_lines, confs, prev_par = [], [], [], None
    for key in order:
        words = lines[key]["words"]
        par = key[:2]
        if prev_par is not None and par != prev_par:
            md_lines.append("")
        prev_par = par
        md_lines.append(" ".join(make_marker(w["t"], w["c"]) if w["c"] < threshold else w["t"] for w in words))
        bx = [min(w["b"][0] for w in words), min(w["b"][1] for w in words),
              max(w["b"][2] for w in words), max(w["b"][3] for w in words)]
        json_lines.append({"b": bx, "words": words})
        confs += [w["c"] for w in words]
    page = {"width": img.width, "height": img.height, "psm": psm, "lang": lang,
            "mean_conf": round(sum(confs) / len(confs), 1) if confs else None,
            "flagged": sum(1 for c in confs if c < threshold), "dropped_graphics": dropped, "lines": json_lines}
    return "\n".join(md_lines).strip(), page


def is_real_word(text: str, conf: float) -> bool:
    """A word or number Tesseract read confidently - not binding holes read as 'TT', 'Yt' or '{='.
    Numbers count too: a contents page's column of page numbers can sit right next to the rings."""
    if conf < 80:
        return False
    t = text.strip(".,:;!?()[]{}'\"")
    if t.isdigit():
        return conf >= 85
    letters = sum(ch.isalpha() for ch in t)
    return len(t) >= 3 and letters >= 0.75 * len(t) and len(set(t.lower())) >= 2


def _real_words_at_edge(g, side: str, width: int, lang: str) -> list[int]:
    """OCR just one edge strip; -> distance from the page edge of each real word in it (empty for binding junk)."""
    import pytesseract
    from PIL import ImageOps
    from .binding import paper_level
    pad = 20
    box = (0, 0, width, g.height) if side == "left" else (g.width - width, 0, g.width, g.height)
    crop = ImageOps.expand(g.crop(box), border=pad, fill=paper_level(g))   # paper colour: grey pages stay readable
    data = pytesseract.image_to_data(crop, lang=lang, config="--psm 6", output_type=pytesseract.Output.DICT)
    out = []
    for i, txt in enumerate(data["text"]):
        txt = (txt or "").strip()
        if data["level"][i] == 5 and txt and is_real_word(txt, float(data["conf"][i])):
            x0 = data["left"][i] - pad
            out.append(x0 if side == "left" else width - (x0 + data["width"][i]))
    return out


def binding_strips(g, lang: str) -> dict[str, int]:
    """Edge strips of a prepared page image to blank before OCR: ring/comb binding marks and spine shadows
    (see binding.py). Each strip is OCR'd on its own first: if Tesseract reads real words there it is margin text,
    so only the binding bands nearer the edge than those words are blanked. -> {"left": px, "right": px}."""
    from .binding import find_binding
    out: dict[str, int] = {}
    for side, cuts in find_binding(g).items():
        if not cuts:
            continue
        words = _real_words_at_edge(g, side, cuts[-1], lang)
        if words:
            cuts = [c for c in cuts if c <= min(words)]
        if cuts:
            out[side] = cuts[-1]
    return out


def ocr_best(img, upscale: bool, cleanup: str, lang: str, psm: int, threshold: int,
             drop_graphics: bool = True, ignore_binding: bool = True) -> tuple[str, dict]:
    """OCR with the chosen cleanup; 'auto' tries no cleanup first and retries poor pages with strong cleanup,
    keeping whichever result Tesseract is more confident about. ignore_binding blanks ring binding marks and
    spine shadows at the page edges first (recorded in the page json as "binding": {side: width})."""
    from .binding import blank_edges
    first = "none" if cleanup == "auto" else cleanup
    g = prepare(img, upscale, first)
    strips = binding_strips(g, lang) if ignore_binding else {}
    lr = strips.get("left", 0), strips.get("right", 0)
    body, pj = ocr_image(blank_edges(g, *lr), lang, psm, threshold, drop_graphics)
    pj["cleanup"] = first
    if cleanup == "auto" and (pj["mean_conf"] or 0) < AUTO_RETRY_BELOW:
        body2, pj2 = ocr_image(blank_edges(prepare(img, upscale, "strong"), *lr), lang, psm, threshold,
                               drop_graphics)
        if (pj2["mean_conf"] or 0) > (pj["mean_conf"] or 0):
            body, pj = body2, pj2
            pj["cleanup"] = "strong"
    if strips:
        pj["binding"] = strips
    return body, pj


def page_images(lib: Library, d: Document, pages: list[int] | None, dpi: int):
    """Yield (page_number, PIL image) for an image or PDF document."""
    from PIL import Image
    src = lib.abspath(d.path)
    if d.kind == "image":
        with Image.open(src) as im:
            n = getattr(im, "n_frames", 1)
            for i in range(n):
                if pages and (i + 1) not in pages:
                    continue
                im.seek(i)
                yield i + 1, im.copy()
    elif d.kind == "pdf":
        import pymupdf
        with pymupdf.open(src) as doc:
            for i, page in enumerate(doc):
                if pages and (i + 1) not in pages:
                    continue
                pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
                yield i + 1, Image.frombytes("L", (pix.width, pix.height), pix.samples)
    else:
        raise OCRError(f"OCR only applies to images and PDFs (this is {d.kind}).")


def render_page(lib: Library, d: Document, page: int, dpi: int = 150):
    """PIL image of a page for display (original colours)."""
    from PIL import Image
    src = lib.abspath(d.path)
    if d.kind == "pdf":
        import pymupdf
        with pymupdf.open(src) as doc:
            if not 1 <= page <= doc.page_count:
                return None
            pix = doc[page - 1].get_pixmap(dpi=dpi, alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if d.kind == "image":
        with Image.open(src) as im:
            im.seek(max(0, min(page - 1, getattr(im, "n_frames", 1) - 1)))
            return im.convert("RGB")
    return None


def has_manual_edits(lib: Library, d: Document) -> bool:
    """True when the OCR text was edited/reviewed since OCR ran (so re-running OCR would lose work)."""
    if d.text_status == "reviewed":
        return True
    md, js = lib.derived_md_path(d), lib.ocr_json_path(d)
    return md.exists() and js.exists() and md.stat().st_mtime > js.stat().st_mtime + 2


def load_ocr_json(lib: Library, d: Document) -> dict:
    p = lib.ocr_json_path(d)
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            pass
    return {"pages": {}}


def ocr_document(lib: Library, d: Document, *, pages: list[int] | None = None, force: bool = False,
                 psm: int | None = None, cleanup: str | None = None, progress=None, cancel=None, log: Log = print,
                 app_tesseract: str = "") -> dict:
    """OCR a document. PDFs: by default only pages without a usable text layer (force=True: all pages).
    `pages` restricts to specific page numbers (used by the editor's 're-OCR this page')."""
    configure(lib, app_tesseract)
    st = lib.settings
    psm = psm or st.ocr_psm
    cleanup = cleanup or st.ocr_cleanup
    md_path = lib.derived_md_path(d)

    existing: dict[int, str] = {}
    if md_path.exists():
        existing = {n: b for n, b in split_pages(md_path.read_text("utf-8")) if n is not None}

    if d.kind == "pdf" and pages is None and not force:
        from .ingest import scanned_pages
        pages = scanned_pages(lib.abspath(d.path), st.pdf_min_text_chars)
        if not pages:
            log(f"{d.title}: every page already has a text layer (use 'Force OCR' to OCR anyway).")
            lib.update_document(d.id, needs_ocr=0)
            return {"pages": 0}
    total = d.pages or (len(pages) if pages else 1)
    todo = pages or list(range(1, total + 1))
    js = load_ocr_json(lib, d)
    js.update({"engine": "tesseract", "threshold": st.low_conf_threshold, "updated": now_iso()})
    done = bound = 0
    for n, img in page_images(lib, d, todo, st.ocr_dpi):
        if cancel and cancel():
            log("OCR cancelled - pages done so far were kept.")
            break
        if progress:
            progress(done, len(todo), f"{d.title}: OCR page {n}")
        body, pj = ocr_best(img, st.ocr_upscale_small_images and d.kind == "image", cleanup,
                            st.ocr_language, psm, st.low_conf_threshold, st.ocr_drop_graphics,
                            st.ocr_ignore_binding)
        if pj.get("binding"):
            bound += 1
        existing[n] = body if body else "<!-- OCR found no text on this page -->"
        js["pages"][str(n)] = pj
        done += 1
        # write after every page so a crash/cancel loses nothing
        _write(lib, d, md_path, existing, js)
    if progress:
        progress(len(todo), len(todo), "done")

    confs = [p["mean_conf"] for p in js["pages"].values() if p.get("mean_conf") is not None]
    text = md_path.read_text("utf-8") if md_path.exists() else ""
    flagged = count_markers(text)
    still = d.kind == "pdf" and "<!-- no text layer" in text
    lib.update_document(d.id, text_path=lib.relpath(md_path), text_status="ocr", needs_ocr=int(still),
                        flagged=flagged, ocr_mean_conf=round(sum(confs) / len(confs), 1) if confs else None,
                        text_updated=now_iso())
    log(f"OCR {d.title}: {done} page(s), mean confidence {confs and round(sum(confs) / len(confs), 1)}, "
        f"{flagged} low-confidence word(s) flagged."
        + (f" Binding marks / spine shadow ignored on {bound} page(s)." if bound else ""))
    return {"pages": done, "flagged": flagged, "binding_pages": bound}


def _write(lib: Library, d: Document, md_path: Path, pages: dict[int, str], js: dict) -> None:
    body = join_pages(sorted(pages.items()))
    if not body.lstrip().startswith("# "):
        body = f"# {d.title}\n\n" + body
    md_path.write_text(body, "utf-8")
    lib.ocr_json_path(d).write_text(json.dumps(js, separators=(",", ":")), "utf-8")
