"""Find ring / comb / wire binding marks and spine shadows at the left and right edges of a scanned page.

Printed text never reaches the edge of the paper - there is always a white margin. Binding marks do: they are a
band of ink that starts at (or very near) the edge and is separated from the text by a white gutter, and is either
a column of evenly spaced holes, rings or comb teeth, or one long dark band (a spine or scanner-lid shadow, or the
dark edge of the page). Tesseract reads these as junk 'words' ("TT", "Yt", "{=") and they can upset its layout
analysis, so OCR blanks them out first.

Anything else near the edge - page numbers, margin notes, thumb tabs, drawings - doesn't fit those shapes and is
left alone. The OCR step also double-checks each strip with Tesseract before blanking it (see ocr.py), so a
column of real margin text is never removed.
"""
from __future__ import annotations

SIDES = ("left", "right")

MAX_STRIP = 0.16      # binding never reaches further in than this fraction of the page width
NEAR_EDGE = 0.05      # the first band must start within this fraction of the page edge
GUTTER = 0.010        # minimum white gap after each band, as a fraction of the width
BLANK = 0.012         # a column counts as blank when at most this fraction of its pixels are ink
WORK_WIDTH = 700      # pages are analysed at about this width (fast; plenty for finding holes)


def _runs(mask) -> list[tuple[int, int]]:
    """[start, end) runs of True in a 1-D bool array."""
    import numpy as np
    if not mask.any():
        return []
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist()))


def classify_band(ink) -> tuple[str, dict]:
    """ink: bool array (rows x band columns), oriented so column 0 is nearest the page edge.
    -> ('rings' | 'shadow' | '', measurements)."""
    import numpy as np
    h = ink.shape[0]
    rows = ink.mean(axis=1) > 0.02
    blobs = [(a, b) for a, b in _runs(rows) if b - a >= max(2, h * 0.004)]
    covered = sum(b - a for a, b in blobs) / max(1, h)
    info: dict = {"blobs": len(blobs), "covered": round(float(covered), 2)}
    # one long dark band: spine shadow, scanner-lid shadow, page edge, holes merged together by a shadow
    if covered > 0.6 and len(blobs) <= 6:
        return "shadow", info
    if len(blobs) < 6:
        return "", info
    centres = [(a + b) / 2 for a, b in blobs]
    span = (centres[-1] - centres[0]) / h
    gaps = np.diff(centres)
    med_gap = float(np.median(gaps))
    # holes repeat at a fixed pitch (wire 3:1 = 1/3", wire 2:1 = 1/2", plastic comb = 9/16"); a missing or
    # merged hole doubles a gap, so gaps of about 1x or 2x the usual one both count as regular
    ratio = gaps / max(1.0, med_gap)
    regular = float(np.mean((np.abs(ratio - 1) < 0.25) | (np.abs(ratio - 2) < 0.25)))
    # every hole is the same shape, so the blobs reach the same distance in from the edge
    reach = np.array([ink[a:b].any(axis=0).sum() for a, b in blobs], float)
    same_shape = float(reach.std() / max(1.0, reach.mean()))
    # periodicity of the whole row profile (robust when neighbouring holes merge into longer blobs)
    prof = ink.mean(axis=1).astype(float)
    prof -= prof.mean()
    ac = np.correlate(prof, prof, "full")[h - 1:]
    lo, hi = max(2, int(h * 0.01)), max(3, int(h * 0.09))
    peak = float(ac[lo:hi].max() / ac[0]) if ac[0] > 0 and hi > lo else 0.0
    info.update(pitch=round(med_gap, 1), regular=round(regular, 2), same_shape=round(same_shape, 2),
                periodic=round(peak, 2), span=round(float(span), 2))
    if span > 0.5 and (((peak >= 0.35 or regular >= 0.75) and same_shape < 0.5) or peak >= 0.6):
        return "rings", info
    return "", info


def find_binding(gray, explain: bool = False) -> dict:
    """Binding marks at each edge of a page image (PIL, any mode).

    -> {"left": [w1, w2, ...], "right": [...]} - for each side, the strip widths in the image's own pixels that
    would remove the 1st, 1st+2nd, ... binding band counted in from the edge (innermost last; empty = nothing
    found). Each width ends a little way into the white gutter after its band, never in the text.
    explain=True adds "<side>_bands" with what was measured, for testing and tuning."""
    import numpy as np
    from PIL import Image
    g = gray if gray.mode == "L" else gray.convert("L")
    scale = min(1.0, WORK_WIDTH / max(1, g.width))
    if scale < 1.0:
        g = g.resize((max(1, round(g.width * scale)), max(1, round(g.height * scale))), Image.BILINEAR)
    a = np.asarray(g, dtype=np.uint8)
    out: dict = {"left": [], "right": []}
    if a.size == 0 or a.shape[1] < 50 or a.shape[0] < 50:
        return out
    paper = int(np.median(a))                      # most of a page is paper; ink is clearly darker than it
    ink = a < max(60, min(160, paper - 40))
    H, W = ink.shape
    # ruled lines across the page (headers, footers, box edges) would bridge the gutter: leave those rows out
    keep_rows = ink.mean(axis=1) < 0.35
    col = ink[keep_rows].mean(axis=0) if keep_rows.any() else ink.mean(axis=0)
    max_w, gut = int(W * MAX_STRIP), max(2, int(W * GUTTER))
    for side in SIDES:
        c = col if side == "left" else col[::-1]
        inked = c > BLANK
        if not inked.any():
            continue
        first = int(np.argmax(inked))
        if first > W * NEAR_EDGE or first >= max_w:
            continue
        # walk in from the edge one inked band at a time (page edge, holes, rings, shadow...); each band must be
        # followed by a white gutter; stop at the first band that isn't binding - that's where the page begins
        x, bands = first, []
        while x < max_w:
            start, blank_run, end = x, 0, None
            while x < max_w:
                if inked[x]:
                    blank_run = 0
                else:
                    blank_run += 1
                    if blank_run >= gut:
                        end = x - blank_run + 1
                        break
                x += 1
            if end is None or end <= start:
                break
            band = ink[:, start:end] if side == "left" else ink[:, W - end:W - start][:, ::-1]
            kind, info = classify_band(band)
            bands.append({"kind": kind, "from": start, "to": end, **info})
            if not kind:
                break
            out[side].append(int(round((end + gut // 2) / scale)))
            while x < max_w and not inked[x]:          # skip the rest of the gutter to the next band
                x += 1
        if explain:
            out[side + "_bands"] = bands
    return out


def paper_level(img) -> int:
    """Grey level of the paper: the median pixel (most of a page is paper)."""
    hist = img.convert("L").histogram()
    half, run = sum(hist) / 2, 0
    for level, count in enumerate(hist):
        run += count
        if run >= half:
            return level
    return 255


def blank_edges(img, left: int = 0, right: int = 0):
    """Copy of img with the given edge strips painted the page's own paper colour. (Not pure white: on a grey
    page, e.g. a cover, white strips would shift Tesseract's black/white threshold and lose the real text.)"""
    if not left and not right:
        return img
    from PIL import ImageDraw
    out = img.copy()
    d = ImageDraw.Draw(out)
    p = paper_level(out)
    fill = p if out.mode in ("L", "1") else (p,) * len(out.getbands())
    if left:
        d.rectangle([0, 0, left - 1, out.height], fill=fill)
    if right:
        d.rectangle([out.width - right, 0, out.width, out.height], fill=fill)
    return out
