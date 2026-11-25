"""OCR proofreading editor: page scan on the left, editable markdown on the right.

Workflow:  F3 jumps to the next low-confidence word and selects it -> type the correction (or leave it) ->
Ctrl+Enter accepts it (removes the {?..|NN?} marker) and jumps to the next one. The scan highlights the
line and word under the cursor; clicking on the scan moves the cursor to that line.
"""
from __future__ import annotations

import re

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QBrush, QColor, QFont, QImage, QKeySequence, QPainter, QPainterPath, QPen, QPixmap,
                           QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextDocument)
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QMenu, QToolButton, QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene,
                               QGraphicsView, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QSpinBox, QSplitter, QToolBar, QVBoxLayout, QWidget)

from ..library import Document, Library, now_iso
from . import theme
from ..align import align, page_tokens, token_at
from ..ocr import is_graphic_word, load_ocr_json, page_rotations, reference_height, render_page, set_page_rotation
from ..textutil import COMMENT_LINE_RE, MARKER_RE, PAGE_RE, count_markers, pages_needing_ocr, strip_markers

PSM_SHORT = {3: "Layout: auto", 4: "Single column", 6: "Uniform block", 11: "Sparse text", 12: "Sparse + rotate",
             1: "Auto + rotate"}
CLEANUP_SHORT = {"auto": "Clean: auto", "none": "Clean: off", "light": "Clean: light", "strong": "Clean: strong"}
PSM_CHOICES = [(3, "3 - Auto (default)"), (4, "4 - Single column"), (6, "6 - Uniform block"),
               (11, "11 - Sparse text (schematics, pinouts)"), (12, "12 - Sparse + orientation"), (1, "1 - Auto + orientation")]


def conf_color(conf: int) -> QColor:
    if theme.is_retro():      # inverse video, brighter = less sure
        return QColor(theme.T["accent"] if conf < 40 else theme.T["text"] if conf < 60 else theme.T["muted"])
    if conf < 40:
        return QColor(255, 120, 120)
    if conf < 60:
        return QColor(255, 190, 110)
    return QColor(255, 235, 120)


def box_colors(kind: str) -> tuple:
    """(pen, fill) for boxes drawn over the scan: originally-flagged words, current line, current word."""
    if theme.is_retro():
        t = theme.T
        c = {"flag": QColor(t["muted"]), "line": QColor(t["accent"]), "word": QColor(t["text"])}[kind]
        fill = QColor(c); fill.setAlpha({"flag": 40, "line": 0, "word": 80}[kind])
        return c, (fill if kind != "line" else None)
    return {"flag": (QColor(220, 60, 60, 160), QColor(255, 80, 80, 40)), "line": (QColor(40, 120, 255), None),
            "word": (QColor(255, 140, 0), QColor(255, 160, 0, 70))}[kind]


def is_noise_line(line: str, threshold: int = 70) -> bool:
    """True for lines that are mostly OCR junk (specks and fragments of diagrams), not real-but-misread words:
    most tokens are tiny or punctuation-heavy AND the line's average confidence is low."""
    tokens: list[tuple[str, int]] = []
    pos = 0
    for m in MARKER_RE.finditer(line):
        tokens += [(w, 95) for w in line[pos:m.start()].split()]
        tokens.append((m.group(1), int(m.group(2))))
        pos = m.end()
    tokens += [(w, 95) for w in line[pos:].split()]
    if not tokens:
        return False
    alnum_total = sum(sum(ch.isalnum() for ch in w) for w, _ in tokens)
    if alnum_total <= 2:
        return True
    def junk(w: str) -> bool:
        a = sum(ch.isalnum() for ch in w)
        return a <= 2 or a / len(w) < 0.5
    junk_share = sum(junk(w) for w, _ in tokens) / len(tokens)
    mean_conf = sum(c for _, c in tokens) / len(tokens)
    return junk_share >= 0.7 and mean_conf < max(50, threshold - 10)


class MarkerHighlighter(QSyntaxHighlighter):
    def __init__(self, doc: QTextDocument):
        super().__init__(doc)
        self.page_fmt = QTextCharFormat()
        self.page_fmt.setForeground(QColor(120, 140, 200))
        self.page_fmt.setFontItalic(True)
        self.comment_fmt = QTextCharFormat()
        self.comment_fmt.setForeground(QColor(140, 140, 140))
        self.head_fmt = QTextCharFormat()
        self.head_fmt.setFontWeight(QFont.Bold)
        self.syntax_fmt = QTextCharFormat()
        self.syntax_fmt.setForeground(QColor(150, 110, 60))
        self.refresh_formats()

    def refresh_formats(self) -> None:
        t = theme.T
        retro = theme.is_retro()
        self.page_fmt.setForeground(QColor(t["accent"] if retro else "#7890c8"))
        self.comment_fmt.setForeground(QColor(t["muted"]))
        self.syntax_fmt.setForeground(QColor(t["muted"] if retro else "#96703c"))
        self.head_fmt.setFontWeight(QFont.Normal if retro else QFont.Bold)
        self.head_fmt.setForeground(QColor(t["accent"]) if retro else QColor(t["text"]))
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:
        if PAGE_RE.match(text):
            self.setFormat(0, len(text), self.page_fmt)
            return
        if COMMENT_LINE_RE.match(text):
            self.setFormat(0, len(text), self.comment_fmt)
            return
        if text.startswith("#"):
            self.setFormat(0, len(text), self.head_fmt)
        for m in MARKER_RE.finditer(text):
            conf = int(m.group(2))
            word = QTextCharFormat()
            word.setBackground(conf_color(conf))
            word.setForeground(QColor(theme.T["on_accent"]) if theme.is_retro() else QColor(0, 0, 0))
            word.setFontUnderline(True)
            word.setUnderlineColor(QColor(200, 0, 0))
            self.setFormat(m.start(), m.end() - m.start(), self.syntax_fmt)
            self.setFormat(m.start(1), len(m.group(1)), word)


class PageView(QGraphicsView):
    clicked = Signal(float, float)   # in OCR-image coordinates

    def __init__(self, fit_mode: str = "width"):
        super().__init__()
        self.fit_mode = fit_mode      # 'width' (editor) or 'page' (preview); kept until the user zooms
        self.user_zoomed = False
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.pix_item: QGraphicsPixmapItem | None = None
        self.overlay: list = []
        self.scale_factor = 1.0      # display pixels per OCR pixel
        self._fit_pending = True
        self._press = None

    def set_image(self, img, ocr_width: int | None) -> None:
        sc = self.scene()
        sc.clear()
        self.overlay = []
        if img is None:
            self.pix_item = None
            sc.addText("No preview available")
            return
        if theme.is_retro():
            img = theme.phosphorize(img)
        else:
            img = img.convert("RGB")
        data = img.tobytes("raw", "RGB")
        qimg = QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888).copy()
        self.pix_item = sc.addPixmap(QPixmap.fromImage(qimg))
        sc.setSceneRect(QRectF(0, 0, img.width, img.height))
        self.scale_factor = img.width / ocr_width if ocr_width else 1.0
        if not self.user_zoomed:
            self.fit()

    def fit(self) -> None:
        if not self.pix_item:
            return
        if self.fit_mode == "page":
            self.fitInView(self.pix_item, Qt.KeepAspectRatio)
            return
        self.resetTransform()
        w = self.pix_item.pixmap().width()
        if w:
            f = max(0.05, (self.viewport().width() - 20) / w)
            self.scale(f, f)

    def fit_width(self) -> None:
        """Toolbar 'fit' button: back to automatic fitting."""
        self.user_zoomed = False
        self.fit()

    def zoom(self, f: float) -> None:
        self.user_zoomed = True
        self.scale(f, f)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if not self.user_zoomed:
            self.fit()

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            self.zoom(1.15 if e.angleDelta().y() > 0 else 1 / 1.15)
        else:
            super().wheelEvent(e)

    def mousePressEvent(self, e):
        self._press = e.position()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        if self._press is not None and (e.position() - self._press).manhattanLength() < 4 and self.pix_item:
            p = self.mapToScene(e.position().toPoint())
            self.clicked.emit(p.x() / self.scale_factor, p.y() / self.scale_factor)
        self._press = None

    def clear_overlay(self):
        for it in self.overlay:
            self.scene().removeItem(it)
        self.overlay = []

    def add_box(self, b, pen: QColor, fill: QColor | None = None, width: float = 2) -> QGraphicsRectItem:
        s = self.scale_factor
        r = QRectF(b[0] * s - 2, b[1] * s - 2, (b[2] - b[0]) * s + 4, (b[3] - b[1]) * s + 4)
        it = self.scene().addRect(r, QPen(pen, width), QBrush(fill) if fill else QBrush(Qt.NoBrush))
        it.setZValue(5)
        self.overlay.append(it)
        return it


class FindBar(QWidget):
    def __init__(self, editor: QPlainTextEdit):
        super().__init__()
        self.ed = editor
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        self.find = QLineEdit(placeholderText="Find")
        self.repl = QLineEdit(placeholderText="Replace with")
        self.case = QCheckBox("Case")
        self.regex = QCheckBox("Regex")
        self.status = QLabel("")
        b_next, b_rep, b_all, b_close = (QPushButton(t) for t in ("Find next", "Replace", "Replace all", "✕"))
        b_close.setFixedWidth(28)
        for w in (self.find, self.repl, self.case, self.regex, b_next, b_rep, b_all, self.status, b_close):
            lay.addWidget(w)
        self.find.returnPressed.connect(self.find_next)
        b_next.clicked.connect(self.find_next)
        b_rep.clicked.connect(self.replace_one)
        b_all.clicked.connect(self.replace_all)
        b_close.clicked.connect(self.hide)

    def _pattern(self):
        t = self.find.text()
        if not t:
            return None
        flags = 0 if self.case.isChecked() else re.I
        try:
            return re.compile(t if self.regex.isChecked() else re.escape(t), flags)
        except re.error as e:
            self.status.setText(f"Bad regex: {e}")
            return None

    def find_next(self) -> bool:
        pat = self._pattern()
        if not pat:
            return False
        text = self.ed.toPlainText()
        start = self.ed.textCursor().selectionEnd()
        m = pat.search(text, start) or pat.search(text, 0)
        if not m:
            self.status.setText("Not found")
            return False
        c = self.ed.textCursor()
        c.setPosition(m.start()); c.setPosition(m.end(), QTextCursor.KeepAnchor)
        self.ed.setTextCursor(c)
        self.status.setText("")
        return True

    def replace_one(self):
        pat = self._pattern()
        c = self.ed.textCursor()
        if pat and c.hasSelection() and pat.fullmatch(c.selectedText()):
            c.insertText(pat.sub(self.repl.text(), c.selectedText()) if self.regex.isChecked() else self.repl.text())
        self.find_next()

    def replace_all(self):
        pat = self._pattern()
        if not pat:
            return
        text = self.ed.toPlainText()
        repl = self.repl.text() if self.regex.isChecked() else self.repl.text().replace("\\", "\\\\")
        new, n = pat.subn(repl, text)
        if n:
            c = self.ed.textCursor()
            c.beginEditBlock()
            c.select(QTextCursor.Document)
            c.insertText(new)
            c.endEditBlock()
        self.status.setText(f"{n} replaced")


class ProofEditor(QWidget):
    saved = Signal(int)                  # doc id
    request_reocr = Signal(int, int, int, str)  # doc id, page, psm, cleanup
    request_ocr_rest = Signal(int)              # doc id: OCR the pages not read yet
    dirty_changed = Signal(bool)
    cancel_job = Signal()

    def __init__(self, lib: Library, doc: Document, font_size: int = 11):
        super().__init__()
        self.lib, self.doc = lib, doc
        self._load_ocr()
        self.threshold = int(self.ocr.get("threshold", lib.settings.low_conf_threshold))
        self.cur_page = None
        self._last_loc = None
        self._align_cache: dict = {}      # page -> (page text, aligned tokens)
        self._flags_key = None            # which alignment the red flag boxes were drawn for
        self._flash_item = None
        self._flash_left = 0
        self._static_overlay: list = []
        self._img_cache: dict[int, object] = {}
        self.editable = bool(doc.text_path) and doc.text_path != doc.path
        self.has_pages = doc.kind in ("pdf", "image")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        tb = QToolBar()
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.setIconSize(QSize(16, 16))
        lay.addWidget(tb)
        from .widgets import BusyBanner
        self.busy = BusyBanner()
        self.busy.cancel.connect(self.cancel_job.emit)
        lay.addWidget(self.busy)
        # a PDF whose pages weren't all OCR'd: say so, and offer to read the rest (Re-OCR only does one page)
        self.rest = QFrame()
        self.rest.setObjectName("restBanner")
        rh = QHBoxLayout(self.rest)
        rh.setContentsMargins(10, 6, 6, 6)
        self.rest_label = QLabel()
        self.rest_label.setWordWrap(True)
        self.rest_btn = QPushButton()
        self.rest_btn.setObjectName("primary")
        self.rest_btn.clicked.connect(lambda: self.request_ocr_rest.emit(self.doc.id))
        rh.addWidget(self.rest_label, 1)
        rh.addWidget(self.rest_btn)
        self.rest.hide()
        lay.addWidget(self.rest)
        self._icons: list = []
        # scan tools live in their own toolbar above the page image
        stb = QToolBar()
        stb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        stb.setIconSize(QSize(16, 16))

        self.text = QPlainTextEdit()
        self._font_size = font_size
        self.text.setFont(theme.mono_font(font_size))
        self.text.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.text.setReadOnly(not self.editable)
        self.hl = MarkerHighlighter(self.text.document())
        self.view = PageView()
        self.view.clicked.connect(self._on_image_click)

        self.split = QSplitter()
        self.split.setChildrenCollapsible(False)
        if self.has_pages:
            left = QWidget()
            lv = QVBoxLayout(left)
            lv.setContentsMargins(0, 0, 0, 0)
            lv.setSpacing(0)
            lv.addWidget(stb)
            lv.addWidget(self.view, 1)
            left.setMinimumWidth(320)
            self.split.addWidget(left)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(4)
        self.loupe = QLabel()
        self.loupe.setObjectName("loupe")
        self.loupe.setAlignment(Qt.AlignCenter)
        self.loupe.setFixedHeight(118)
        self.loupe.setMinimumWidth(10)
        self.loupe.setToolTip("Magnified view of the line you're on, with the current word boxed")
        self.loupe.setVisible(self.has_pages)
        rv.addWidget(self.loupe)
        self.hint = QLabel()
        self.hint.setObjectName("callout")
        self.hint.setWordWrap(True)
        self.hint.hide()
        rv.addWidget(self.hint)
        rv.addWidget(self.text, 1)
        self.split.addWidget(right)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 1)
        lay.addWidget(self.split, 1)
        self.findbar = FindBar(self.text)
        self.findbar.hide()
        lay.addWidget(self.findbar)

        def act(text, slot, shortcut=None, tip=None, ic=None, bar=None):
            a = QAction(text, self)
            if ic:
                self._icons.append((a, ic))
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
                a.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            a.setToolTip((tip or text) + (f"  ({shortcut})" if shortcut else ""))
            (bar or tb).addAction(a)
            self.addAction(a)
            return a

        self.a_save = act("Save", self.save, "Ctrl+S", None, "save")
        self.saved_lbl = QLabel("")
        self.saved_lbl.setObjectName("savedFlash")
        tb.addWidget(self.saved_lbl)
        tb.addSeparator()
        act("Prev", self.prev_flag, "Shift+F3", "Previous low-confidence word", "prev")
        act("Next flag", self.next_flag, "F3", "Next low-confidence word (selects it so you can type over it)", "flag")
        act("Accept", self.accept_here, "Ctrl+Return", "Accept the word at the cursor (removes its marker) and go to the next", "check")
        act("Delete word", self.delete_here, "Ctrl+Del",
            "Delete the flagged word at the cursor - for junk OCR 'found' in drawings - and go to the next", "delete")
        more = QToolButton()
        more.setText("More")
        more.setPopupMode(QToolButton.InstantPopup)
        mm = QMenu(more)
        mm.addAction("Accept all flagged words on this page", self.accept_page)
        mm.addAction("Accept all flagged words in document…", self.accept_all)
        mm.addSeparator()
        mm.addAction("Remove drawing shapes read as words (this page)…", self.remove_drawing_noise).setToolTip(
            "Delete flagged 'words' whose box is far too big for text - chip outlines, wires and boxes in schematics")
        mm.addAction("Delete garbage lines on this page…", self.delete_garbage_lines).setToolTip(
            "Remove lines that are mostly OCR noise - typical around diagrams and schematics")
        more.setMenu(mm)
        tb.addWidget(more)
        tb.addSeparator()
        act("Find", self.show_find, "Ctrl+H", "Find / replace", "search")
        if self.has_pages:
            stb.addWidget(QLabel(" Page "))
            self.page_spin = QSpinBox()
            self.page_spin.setRange(1, max(1, doc.pages or 1))
            self.page_spin.valueChanged.connect(self.goto_page)
            stb.addWidget(self.page_spin)
            stb.addWidget(QLabel(f" / {doc.pages or 1} "))
            for z in (act("", lambda: self.view.zoom(1 / 1.2), "Ctrl+-", "Zoom out", "zoom_out", stb),
                      act("", lambda: self.view.zoom(1.2), "Ctrl+=", "Zoom in", "zoom_in", stb),
                      act("", self.view.fit_width, None, "Fit width", "fit", stb)):
                b = stb.widgetForAction(z)
                if b:
                    b.setToolButtonStyle(Qt.ToolButtonIconOnly)
            self.a_spot = QAction("Spotlight", self, checkable=True, checked=True)
            self.a_spot.setToolTip("Dim the page except the line you're editing")
            self.a_spot.toggled.connect(lambda _on: (setattr(self, "_last_loc", None), self._sync()))
            stb.addAction(self.a_spot)
            self._icons.append((self.a_spot, "view"))
            stb.addSeparator()
            # Re-OCR split button: click = re-OCR this page; arrow = choose layout mode and cleanup
            from PySide6.QtGui import QActionGroup
            from ..ocr import CLEANUP_CHOICES
            self.reocr_btn = QToolButton()
            self.reocr_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            self.reocr_btn.setPopupMode(QToolButton.MenuButtonPopup)
            menu = QMenu(self.reocr_btn)
            menu.addSection("Layout")
            self._psm_group = QActionGroup(self)
            for v, label in PSM_CHOICES:
                a = menu.addAction(label)
                a.setCheckable(True); a.setData(v); a.setChecked(v == lib.settings.ocr_psm)
                a.toggled.connect(lambda _on: self._update_reocr_label())
                self._psm_group.addAction(a)
            menu.addSection("Scan cleanup")
            self._clean_group = QActionGroup(self)
            for v, label in CLEANUP_CHOICES:
                a = menu.addAction(label)
                a.setCheckable(True); a.setData(v); a.setChecked(v == lib.settings.ocr_cleanup)
                a.toggled.connect(lambda _on: self._update_reocr_label())
                self._clean_group.addAction(a)
            if not self._psm_group.checkedAction():
                self._psm_group.actions()[0].setChecked(True)
            if not self._clean_group.checkedAction():
                self._clean_group.actions()[0].setChecked(True)
            menu.addSeparator()
            go = menu.addAction("Re-OCR this page now")
            go.triggered.connect(self.reocr_page)
            self.reocr_btn.setMenu(menu)
            self.reocr_action = QAction("Re-OCR", self)
            self.reocr_action.triggered.connect(self.reocr_page)
            self.reocr_btn.setDefaultAction(self.reocr_action)
            self._icons.append((self.reocr_action, "refresh"))
            stb.addWidget(self.reocr_btn)
            self._update_reocr_label()
            # Rotate: icon only, by the zoom buttons (the toolbar is narrow); a menu of turns
            self.rotate_btn = QToolButton()
            self.rotate_action = QAction("Rotate", self)
            self.rotate_action.setToolTip("Rotate this page: turn it the right way up and OCR it again. "
                                          "The original file isn't changed.")
            self._icons.append((self.rotate_action, "rotate"))
            self.rotate_btn.setDefaultAction(self.rotate_action)
            self.rotate_btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
            rm = QMenu(self.rotate_btn)
            rm.addAction("Turn clockwise  ↻", lambda: self.rotate_page(90))
            rm.addAction("Turn anticlockwise  ↺", lambda: self.rotate_page(270))
            rm.addAction("Turn upside down", lambda: self.rotate_page(180))
            rm.addSeparator()
            self.unrotate_action = rm.addAction("Back as scanned", lambda: self.rotate_page(None))
            rm.aboutToShow.connect(lambda: self.unrotate_action.setEnabled(
                bool(self.turns.get(self.cur_page or 1))))
            self.rotate_btn.setMenu(rm)
            self.rotate_btn.setPopupMode(QToolButton.InstantPopup)
            stb.insertWidget(self.a_spot, self.rotate_btn)
        tb.addSeparator()
        self.a_reviewed = act("Mark reviewed", self.mark_reviewed, None, "Mark this text as proofread", "accept")
        self.flag_label = QLabel()
        self.flag_label.setObjectName("muted")
        tb.addWidget(self.flag_label)
        self.apply_icons()

        self.load_text()
        self.text.cursorPositionChanged.connect(self._schedule_sync)
        self.text.textChanged.connect(self._on_changed)
        self._sync_timer = QTimer(self, singleShot=True, interval=60, timeout=self._sync)
        self._flash_timer = QTimer(self, interval=120, timeout=self._flash_tick)
        self.text.document().modificationChanged.connect(self.dirty_changed.emit)
        self._count_timer = QTimer(self, singleShot=True, interval=400, timeout=self._update_count)
        if self.has_pages:
            QTimer.singleShot(0, lambda: self._show_page(1))
            self._split_done = False
        if not self.editable:
            self.a_save.setEnabled(False)

    def showEvent(self, e):
        super().showEvent(e)
        if self.has_pages and not getattr(self, "_split_done", True):
            self._split_done = True
            w = max(800, self.width())
            self.split.setSizes([w // 2, w - w // 2])

    def restyle(self):
        """Theme changed: fonts, highlight colours, icons and the scan rendering."""
        self.text.setFont(theme.mono_font(self._font_size))
        self.hl.refresh_formats()
        self.apply_icons()
        if self.has_pages and self.cur_page:
            page, self.cur_page = self.cur_page, None
            self._show_page(page)
            self._sync()

    def apply_icons(self):
        for a, ic in self._icons:
            a.setIcon(theme.icon(ic, theme.T["warn"] if ic == "flag" else None))

    # ------------------------------------------------------------------ text io
    def load_text(self):
        self._loading = True
        self.text.setPlainText(self.lib.read_text(self.doc) if self.doc.text_path else
                               "(No text yet - run OCR on this document.)")
        self.text.document().setModified(False)
        self._loading = False
        self._update_count()

    def _update_rest(self):
        left = pages_needing_ocr(self.text.toPlainText()) if self.doc.kind == "pdf" else []
        if left:
            n, total = len(left), self.doc.pages or len(left)
            self.rest_label.setText(
                (f"<b>{n} of the {total} pages haven't been OCR'd yet</b>" if n < total else
                 "<b>No page has been OCR'd yet</b>") + " - they have no text. <b>Re-OCR</b> only reads the "
                "page you're on; this reads the rest and leaves the pages you've checked alone.")
            self.rest_btn.setText(f"OCR the {n} page{'s' if n > 1 else ''}")
        self.rest.setVisible(bool(left))

    def is_dirty(self) -> bool:
        return self.editable and self.text.document().isModified()

    def save(self) -> bool:
        if not self.editable:
            return False
        text = self.text.toPlainText()
        p = self.lib.abspath(self.doc.text_path)
        p.write_text(text if text.endswith("\n") else text + "\n", "utf-8")
        self.lib.update_document(self.doc.id, text_updated=now_iso(), flagged=count_markers(text))
        self.text.document().setModified(False)
        self.doc = self.lib.document(self.doc.id)
        self.saved.emit(self.doc.id)
        self.saved_lbl.setText("\u2713 Saved")
        QTimer.singleShot(2500, lambda: self.saved_lbl.setText(""))
        return True

    def set_busy(self, text: str | None):
        """A job (e.g. re-OCR) is working on this document: show progress and lock editing meanwhile."""
        if text:
            self.busy.show_busy(text)
            self.text.setReadOnly(True)
            self.rest_btn.setEnabled(False)
            if hasattr(self, "rotate_btn"):
                self.rotate_btn.setEnabled(False)
        else:
            self.busy.clear()
            self.text.setReadOnly(not self.editable)
            self.rest_btn.setEnabled(True)
            if hasattr(self, "rotate_btn"):
                self.rotate_btn.setEnabled(True)

    # ------------------------------------------------------------------ OCR noise
    def delete_garbage_lines(self):
        """Delete lines on the current page that are mostly OCR noise (diagrams, schematics). The text stays in
        step with the scan because words are matched to the OCR boxes by content."""
        lo, hi = self._page_range()
        seg = self.text.toPlainText()[lo:hi]
        out, n = [], 0
        for line in seg.split("\n"):
            if line.strip() and not PAGE_RE.match(line) and not COMMENT_LINE_RE.match(line) \
                    and is_noise_line(line, self.threshold):
                n += 1
                continue
            out.append(line)
        if not n:
            QMessageBox.information(self, "Garbage lines", "No lines on this page look like OCR noise.")
            return
        if QMessageBox.question(self, "Garbage lines",
                                f"Delete {n} line(s) on this page that are mostly OCR noise (typically text OCR "
                                "'found' in diagrams and schematics)?\n\nCtrl+Z undoes it.") == QMessageBox.Yes:
            self._replace_range(lo, hi, "\n".join(out))
    def _on_changed(self):
        if not getattr(self, "_loading", False):
            self._count_timer.start()

    def _update_count(self):
        n = count_markers(self.text.toPlainText())
        status = {"reviewed": "reviewed ✓", "ocr": "OCR (unreviewed)"}.get(self.doc.text_status, self.doc.text_status)
        ro = "" if self.editable else "  ·  read-only (original file)"
        self.flag_label.setText(f"   {n} flagged  ·  {status}{ro}")
        self._update_rest()

    # ------------------------------------------------------------------ flags
    def _find_marker(self, forward: bool = True):
        text = self.text.toPlainText()
        c = self.text.textCursor()
        pos = c.selectionEnd() if forward else c.selectionStart()
        ms = list(MARKER_RE.finditer(text))
        if not ms:
            return None
        if forward:
            # skip the marker we're inside of
            m = next((m for m in ms if m.start() >= pos or (m.start(1) >= pos)), None) or ms[0]
        else:
            m = next((m for m in reversed(ms) if m.end() < pos - 1 and m.start(1) < pos), None) or ms[-1]
        return m

    def _select_marker_word(self, m):
        c = self.text.textCursor()
        c.setPosition(m.start(1))
        c.setPosition(m.end(1), QTextCursor.KeepAnchor)
        self.text.setTextCursor(c)
        self.text.centerCursor()
        self.text.setFocus()

    def next_flag(self):
        m = self._find_marker(True)
        if m:
            self._select_marker_word(m)

    def prev_flag(self):
        m = self._find_marker(False)
        if m:
            self._select_marker_word(m)

    def _marker_at_cursor(self):
        c = self.text.textCursor()
        block = c.block()
        pos = c.position() - block.position()
        for m in MARKER_RE.finditer(block.text()):
            if m.start() <= pos <= m.end():
                return block.position(), m
        return None

    def accept_here(self):
        hit = self._marker_at_cursor()
        if not hit:
            self.next_flag()
            return
        base, m = hit
        c = self.text.textCursor()
        c.setPosition(base + m.start())
        c.setPosition(base + m.end(), QTextCursor.KeepAnchor)
        c.insertText(m.group(1))
        self.text.setTextCursor(c)
        self.next_flag()

    def _page_range(self):
        text = self.text.toPlainText()
        pos = self.text.textCursor().position()
        starts = [m.start() for m in PAGE_RE.finditer(text)]
        lo = max([s for s in starts if s <= pos], default=0)
        hi = min([s for s in starts if s > pos], default=len(text))
        return lo, hi

    def _replace_range(self, lo, hi, new):
        c = self.text.textCursor()
        c.beginEditBlock()
        c.setPosition(lo); c.setPosition(hi, QTextCursor.KeepAnchor)
        c.insertText(new)
        c.endEditBlock()

    def delete_here(self):
        """Delete the flagged word under the cursor (and one neighbouring space), then go to the next flag."""
        hit = self._marker_at_cursor()
        if not hit:
            self.next_flag()
            return
        base, m = hit
        line = self.text.textCursor().block().text()
        start, end = m.start(), m.end()
        if end < len(line) and line[end] == " ":
            end += 1
        elif start > 0 and line[start - 1] == " ":
            start -= 1
        c = self.text.textCursor()
        c.setPosition(base + start)
        c.setPosition(base + end, QTextCursor.KeepAnchor)
        c.removeSelectedText()
        self.text.setTextCursor(c)
        self.next_flag()

    def _page_ref(self, pj: dict) -> float:
        return reference_height([w for l in pj.get("lines", []) for w in l["words"]], self.threshold)

    def remove_drawing_noise(self):
        """Delete flagged words on this page whose OCR box is drawing-sized (chip outlines, wires, boxes)."""
        page = self.cur_page
        pj = self.ocr.get("pages", {}).get(str(page))
        if not pj:
            QMessageBox.information(self, "Drawing shapes", "This page has no OCR word positions.")
            return
        ref = self._page_ref(pj)
        lo, hi = self._page_bounds(page)
        toks = self._alignment(page)
        doomed = [t for t in toks if t.marker and t.ref is not None and
                  is_graphic_word(pj["lines"][t.ref[0]]["words"][t.ref[1]], ref, pj.get("height", 0), self.threshold)]
        if not doomed:
            QMessageBox.information(self, "Drawing shapes", "No drawing-sized 'words' found on this page.")
            return
        if QMessageBox.question(self, "Drawing shapes",
                                f"Delete {len(doomed)} flagged 'word(s)' on this page whose box is far too big for "
                                "text - parts of the drawing that OCR mistook for words?\n\nCtrl+Z undoes it.") \
                != QMessageBox.Yes:
            return
        text = self.text.toPlainText()
        seg = text[lo:hi]
        for t in sorted(doomed, key=lambda t: -t.start):          # cut from the end so offsets stay valid
            a, b = t.start - lo, t.end - lo
            if b < len(seg) and seg[b] == " ":
                b += 1
            elif a > 0 and seg[a - 1] == " ":
                a -= 1
            seg = seg[:a] + seg[b:]
        seg = re.sub(r"\n[ \t]+\n", "\n\n", seg)
        seg = re.sub(r"\n{3,}", "\n\n", seg)
        self._replace_range(lo, hi, seg)
    def accept_page(self):
        lo, hi = self._page_range()
        seg = self.text.toPlainText()[lo:hi]
        if MARKER_RE.search(seg):
            self._replace_range(lo, hi, strip_markers(seg))

    def accept_all(self):
        n = count_markers(self.text.toPlainText())
        if n and QMessageBox.question(self, "Accept all", f"Remove all {n} low-confidence markers, keeping the words "
                                      "as they are?") == QMessageBox.Yes:
            self._replace_range(0, len(self.text.toPlainText()), strip_markers(self.text.toPlainText()))

    def show_find(self):
        self.findbar.show()
        sel = self.text.textCursor().selectedText()
        if sel and " " not in sel:
            self.findbar.find.setText(sel)
        self.findbar.find.setFocus()
        self.findbar.find.selectAll()

    def mark_reviewed(self):
        n = count_markers(self.text.toPlainText())
        if n:
            r = QMessageBox.question(self, "Mark reviewed",
                                     f"{n} flagged word(s) remain. Accept them all and mark the document reviewed?")
            if r != QMessageBox.Yes:
                return
            self._replace_range(0, len(self.text.toPlainText()), strip_markers(self.text.toPlainText()))
        self.save()
        self.lib.update_document(self.doc.id, text_status="reviewed", flagged=0)
        self.doc = self.lib.document(self.doc.id)
        self._update_count()
        self.saved.emit(self.doc.id)

    def _load_ocr(self):
        """The OCR record, without word boxes read with a page turned another way (a page turned since its
        OCR: its boxes would land in the wrong places until it's OCR'd again)."""
        self.ocr = load_ocr_json(self.lib, self.doc)
        self.turns = page_rotations(self.lib, self.doc, self.ocr)
        pages = self.ocr.get("pages") or {}
        self.ocr["pages"] = {k: v for k, v in pages.items()
                             if not k.isdigit() or int(v.get("rotate") or 0) == self.turns.get(int(k), 0)}

    def rotate_page(self, degrees: int | None):
        """Turn the current page `degrees` clockwise (None: back as scanned), show it turned, and OCR it again -
        unless its text is the PDF's own (not OCR), which turning the picture doesn't change."""
        n = self.cur_page or 1
        if self.is_dirty():
            self.save()
        was = self.turns.get(n, 0)
        turn = 0 if degrees is None else (was + degrees) % 360
        if turn == was:
            return
        from ..textutil import split_pages
        body = next((b for p, b in split_pages(self.text.toPlainText()) if p == n), "")
        ocrd = str(n) in (load_ocr_json(self.lib, self.doc).get("pages") or {})
        reread = self.doc.kind == "image" or ocrd or "<!-- no text layer" in body or not body.strip()
        set_page_rotation(self.lib, self.doc, n, turn)
        self._load_ocr()
        self._img_cache.pop(n, None)
        self._align_cache.pop(n, None)
        self.cur_page = None
        self.goto_page(n)
        if reread and hasattr(self, "_psm_group"):
            self.request_reocr.emit(self.doc.id, n, int(self._psm_group.checkedAction().data()),
                                    self._clean_group.checkedAction().data())
        else:
            self.saved_lbl.setText("Turned. Its text is the PDF's own - Re-OCR reads it instead")
            QTimer.singleShot(5000, lambda: self.saved_lbl.setText(""))

    def reocr_page(self):
        if self.is_dirty():
            self.save()
        psm = self._psm_group.checkedAction().data()
        cleanup = self._clean_group.checkedAction().data()
        self.request_reocr.emit(self.doc.id, self.cur_page or 1, int(psm), cleanup)

    def _update_reocr_label(self):
        if not hasattr(self, "reocr_action"):
            return
        p = self._psm_group.checkedAction()
        c = self._clean_group.checkedAction()
        short = f"{PSM_SHORT.get(p.data(), '') if p else ''} · {CLEANUP_SHORT.get(c.data(), '') if c else ''}"
        self.reocr_action.setToolTip(f"Run OCR again on this page  ({short}).  Use the arrow to change layout / cleanup - "
                                     "'Sparse text' suits schematics and pinouts, 'Strong cleanup' speckled photocopies.")

    def reload_after_ocr(self):
        page = self.cur_page
        self.doc = self.lib.document(self.doc.id)
        self._load_ocr()
        self.editable = bool(self.doc.text_path) and self.doc.text_path != self.doc.path
        self.text.setReadOnly(not self.editable)
        self.a_save.setEnabled(self.editable)
        self.load_text()
        self._img_cache.clear()
        self._align_cache.clear()
        self.cur_page = None
        if page:
            self.goto_page(page)

    # ------------------------------------------------------------------ page sync
    def goto_page(self, n: int):
        text = self.text.toPlainText()
        for m in PAGE_RE.finditer(text):
            if int(m.group(1)) == n:
                c = self.text.textCursor()
                c.setPosition(m.end() + 1 if m.end() < len(text) else m.end())
                self.text.setTextCursor(c)
                self.text.centerCursor()
                break
        self._show_page(n)

    def _page_bounds(self, n: int) -> tuple[int, int]:
        """Text range of page n (from its page marker to the next one)."""
        text = self.text.toPlainText()
        ms = list(PAGE_RE.finditer(text))
        for k, m in enumerate(ms):
            if int(m.group(1)) == n:
                return m.start(), (ms[k + 1].start() if k + 1 < len(ms) else len(text))
        return 0, 0

    def _alignment(self, n: int):
        """Words of page n matched to the OCR boxes by content (recomputed only when that page's text changed)."""
        pj = self.ocr.get("pages", {}).get(str(n))
        if not pj:
            return []
        lo, hi = self._page_bounds(n)
        text = self.text.toPlainText()
        seg = text[lo:hi]
        hit = self._align_cache.get(n)
        if hit and hit[0] == seg and hit[2] == lo:
            return hit[1]
        toks = align(page_tokens(text, lo, hi), pj)
        self._align_cache[n] = (seg, toks, lo)
        return toks

    def _draw_flag_boxes(self, n: int):
        """Red boxes for words that are *still* flagged; fixed, accepted or deleted words lose theirs."""
        for it in self._static_overlay:
            try:
                self.view.scene().removeItem(it)
            except RuntimeError:
                pass
        self.view.overlay = [i for i in self.view.overlay if i not in self._static_overlay]
        self._static_overlay = []
        pj = self.ocr.get("pages", {}).get(str(n))
        if not pj:
            return
        ref = self._page_ref(pj)
        before = len(self.view.overlay)
        for t in self._alignment(n):
            if t.marker and t.ref is not None:
                w = pj["lines"][t.ref[0]]["words"][t.ref[1]]
                pen_col, fill = box_colors("flag")
                big = is_graphic_word(w, ref, pj.get("height", 0), self.threshold)
                self.view.add_box(w["b"], pen_col, None if big else fill, 1)
        self._static_overlay = self.view.overlay[before:]
        self._flags_key = (n, self._align_cache.get(n, ("",))[0])

    def _draw_binding(self, pj: dict | None):
        """Hatch the page-edge strips OCR ignored as binding marks or spine shadow (stays until the page changes)."""
        strips = (pj or {}).get("binding") or {}
        if not strips or not self.view.pix_item:
            return
        s, w, h = self.view.scale_factor, pj.get("width", 0), pj.get("height", 0)
        col = QColor(theme.T["accent"]) if theme.is_retro() else QColor(110, 110, 160)
        col.setAlpha(150)
        for side, px in strips.items():
            x0 = 0 if side == "left" else w - px
            it = self.view.scene().addRect(QRectF(x0 * s, 0, px * s, h * s), QPen(Qt.NoPen),
                                           QBrush(col, Qt.BDiagPattern))
            it.setZValue(3)
            it.setToolTip("Ignored by OCR: ring binding, comb holes or spine shadow at the page edge.\n"
                          "(Settings → OCR → Ignore ring binding…)")

    def _show_page(self, n: int):
        if not self.has_pages or n == self.cur_page:
            return
        self.cur_page = n
        if n not in self._img_cache:
            try:
                self._img_cache = {k: v for k, v in list(self._img_cache.items())[-4:]}
                self._img_cache[n] = render_page(self.lib, self.doc, n, dpi=200)
            except Exception:
                self._img_cache[n] = None
        pj = self.ocr.get("pages", {}).get(str(n))
        self._stop_flash()
        self._last_loc = None
        self.view.set_image(self._img_cache[n], pj.get("width") if pj else None)
        self._static_overlay = []              # set_image cleared the scene
        self._draw_binding(pj)
        self._draw_flag_boxes(n)
        if hasattr(self, "page_spin"):
            self.page_spin.blockSignals(True)
            self.page_spin.setValue(n)
            self.page_spin.blockSignals(False)

    def _schedule_sync(self):
        self._sync_timer.start()

    def _cursor_location(self):
        """-> (page, OCR line index, OCR word index) for the cursor, via the content alignment.
        Word index is -1 on text the user typed in (no box on the scan)."""
        c = self.text.textCursor()
        block = c.block()
        b, page = block, None
        while b.isValid():
            m = PAGE_RE.match(b.text())
            if m:
                page = int(m.group(1))
                break
            b = b.previous()
        if page is None:
            return None, -1, -1
        if not block.text().strip() or PAGE_RE.match(block.text()):
            return page, -1, -1
        toks = self._alignment(page)
        tok = token_at(toks, c.position(), block.position(), block.position() + len(block.text()))
        if tok is None:
            return page, -1, -1
        if tok.ref is not None:
            return page, tok.ref[0], tok.ref[1]
        # typed-in word: show the line of the nearest matched word on the same text line
        lo, hi = block.position(), block.position() + len(block.text())
        near = [t for t in toks if t.ref is not None and t.start >= lo and t.end <= hi]
        return (page, near[0].ref[0], -1) if near else (page, -1, -1)
    def _sync(self):
        if not self.has_pages:
            return
        page, li, wi = self._cursor_location()
        if page is None:
            return
        self._show_page(page)
        self._alignment(page)                  # refreshes the cache if this page's text changed
        if self._flags_key != (page, self._align_cache.get(page, ("",))[0]):
            self._draw_flag_boxes(page)        # text changed: drop boxes of words fixed / deleted since
        # dynamic overlay: spotlight + current line + current word
        for it in [i for i in self.view.overlay if i not in self._static_overlay]:
            self.view.scene().removeItem(it)
        self.view.overlay = list(self._static_overlay)
        pj = self.ocr.get("pages", {}).get(str(page))
        if not pj or li < 0 or li >= len(pj.get("lines", [])):
            self._update_loupe(None, None)
            self._set_hint(None, False)
            return
        line = pj["lines"][li]
        if getattr(self, "a_spot", None) is not None and self.a_spot.isChecked():
            self._add_spotlight(line["b"])
        self.view.add_box(line["b"], *box_colors("line"), 2)
        words = line["words"]
        word = words[wi] if 0 <= wi < len(words) else None
        graphic = bool(word) and is_graphic_word(word, self._page_ref(pj), pj.get("height", 0), self.threshold)
        item = None
        if word:
            pen_col, fill = box_colors("word")
            item = self.view.add_box(word["b"], pen_col, None if graphic else fill, 2)
            if graphic:                      # keep the drawing visible: dashed outline, no fill
                pen = QPen(pen_col, 3, Qt.DashLine)
                item.setPen(pen)
        self._set_hint(word, graphic)
        loc = (page, li, wi)
        if loc != self._last_loc:          # moved to another word: flash it and bring it into view
            self._last_loc = loc
            if word:
                self._flash(word["b"], outline_only=graphic)
                vp = self.view.viewport()
                self.view.ensureVisible(item, vp.width() // 4, vp.height() // 4)
        self._update_loupe(line, word)

    # ------------------------------------------------------------------ spotlight / flash / loupe
    def _add_spotlight(self, b):
        s = self.view.scale_factor
        path = QPainterPath()
        path.addRect(self.view.sceneRect())
        hole = QPainterPath()
        hole.addRect(QRectF(b[0] * s - 8, b[1] * s - 6, (b[2] - b[0]) * s + 16, (b[3] - b[1]) * s + 12))
        it = self.view.scene().addPath(path.subtracted(hole), QPen(Qt.NoPen), QBrush(QColor(0, 0, 0, 80)))
        it.setZValue(4)
        self.view.overlay.append(it)

    def _set_hint(self, word, graphic: bool):
        if word and graphic:
            self.hint.setText("<b>This flagged 'word' covers a large part of the drawing</b> - it's almost certainly a "
                              "chip outline, wire or box that OCR mistook for text, not a word. Press <b>Ctrl+Del</b> "
                              "to delete it, or <i>More &rarr; Remove drawing shapes read as words</i> for the whole page.")
            self.hint.show()
        else:
            self.hint.hide()

    def _flash(self, b, outline_only: bool = False):
        self._stop_flash()
        s = self.view.scale_factor
        col = QColor(theme.T["accent"]) if theme.is_retro() else QColor(255, 70, 0)
        fill = QColor(col)
        fill.setAlpha(0 if outline_only else 90)
        r = QRectF(b[0] * s - 6, b[1] * s - 6, (b[2] - b[0]) * s + 12, (b[3] - b[1]) * s + 12)
        self._flash_item = self.view.scene().addRect(r, QPen(col, 4, Qt.DashLine if outline_only else Qt.SolidLine),
                                                     QBrush(fill))
        self._flash_item.setZValue(7)
        self._flash_left = 6
        self._flash_timer.start()

    def _flash_tick(self):
        try:
            if not self._flash_item or self._flash_left <= 0:
                self._stop_flash()
                return
            self._flash_item.setVisible(not self._flash_item.isVisible())
            self._flash_left -= 1
        except RuntimeError:            # scene was cleared (page changed)
            self._flash_item = None
            self._flash_timer.stop()

    def _stop_flash(self):
        self._flash_timer.stop()
        if self._flash_item is not None:
            try:
                self.view.scene().removeItem(self._flash_item)
            except RuntimeError:
                pass
            self._flash_item = None

    def _update_loupe(self, line, word):
        if not self.has_pages:
            return
        img = self._img_cache.get(self.cur_page)
        pj = self.ocr.get("pages", {}).get(str(self.cur_page))
        if img is None or line is None or not pj or not pj.get("width"):
            self.loupe.setPixmap(QPixmap())
            self.loupe.setText("Click a line of text, or press F3, to see it magnified here.")
            return
        sx, sy = img.width / pj["width"], img.height / pj["height"]
        b = list(line["b"])
        if word:   # Tesseract sometimes returns a whole column as one "line": then zoom on the word itself
            wb = word["b"]
            wh = max(1, wb[3] - wb[1])
            if (b[3] - b[1]) > 2.5 * wh:
                b = [wb[0] - 6 * wh, wb[1] - wh * 0.6, wb[2] + 6 * wh, wb[3] + wh * 0.6]
        line_h = max(4, (b[3] - b[1]) * sy)
        pad = int(line_h * 0.5) + 3
        y0, y1 = max(0, int(b[1] * sy) - pad), min(img.height, int(b[3] * sy) + pad)
        avail_w, avail_h = max(50, self.loupe.width() - 10), self.loupe.height() - 10
        mag = min(3.5, avail_h / max(1, y1 - y0))
        win = avail_w / mag                                   # source pixels that fit across the loupe
        x0, x1 = max(0, int(b[0] * sx) - pad), min(img.width, int(b[2] * sx) + pad)
        if x1 - x0 > win:                                     # long line: centre the window on the word
            cx = ((word["b"][0] + word["b"][2]) / 2 * sx) if word else x0 + win / 2
            x0 = int(max(0, min(cx - win / 2, img.width - win)))
            x1 = int(min(img.width, x0 + win))
        crop = img.crop((x0, y0, x1, y1))
        crop = theme.phosphorize(crop) if theme.is_retro() else crop.convert("RGB")
        qimg = QImage(crop.tobytes("raw", "RGB"), crop.width, crop.height, crop.width * 3, QImage.Format_RGB888).copy()
        f = min(mag, avail_w / max(1, crop.width))
        pm = QPixmap.fromImage(qimg).scaled(max(1, int(crop.width * f)), max(1, int(crop.height * f)),
                                            Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if word:
            wb = word["b"]
            p = QPainter(pm)
            col = QColor(theme.T["accent"]) if theme.is_retro() else QColor(255, 70, 0)
            p.setPen(QPen(col, 2.5))
            fill = QColor(col)
            fill.setAlpha(40)
            p.setBrush(fill)
            p.drawRect(QRectF((wb[0] * sx - x0) * f - 3, (wb[1] * sy - y0) * f - 3,
                              (wb[2] - wb[0]) * sx * f + 6, (wb[3] - wb[1]) * sy * f + 6))
            p.end()
        self.loupe.setPixmap(pm)

    def _on_image_click(self, x: float, y: float):
        """Click on the scan: put the cursor on that word in the text (found via the content alignment)."""
        page = self.cur_page
        pj = self.ocr.get("pages", {}).get(str(page))
        if not pj:
            return
        target = None
        for li, line in enumerate(pj.get("lines", [])):
            lb = line["b"]
            if not (lb[0] - 4 <= x <= lb[2] + 4 and lb[1] - 4 <= y <= lb[3] + 4):
                continue
            wi = next((k for k, w in enumerate(line["words"]) if w["b"][0] - 3 <= x <= w["b"][2] + 3), 0)
            target = (li, wi)
            break
        if target is None:
            return
        toks = self._alignment(page)
        tok = next((t for t in toks if t.ref == target), None)
        if tok is None:   # that word was deleted: nearest surviving word of the same line
            same = [t for t in toks if t.ref and t.ref[0] == target[0]]
            tok = min(same, key=lambda t: abs(t.ref[1] - target[1])) if same else None
        if tok is None:
            return
        c = self.text.textCursor()
        c.setPosition(tok.inner_start)
        self.text.setTextCursor(c)
        self.text.setFocus()
