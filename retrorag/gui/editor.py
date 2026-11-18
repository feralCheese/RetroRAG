"""OCR proofreading editor: page scan on the left, editable markdown on the right.

Workflow:  F3 jumps to the next low-confidence word and selects it -> type the correction (or leave it) ->
Ctrl+Enter accepts it (removes the {?..|NN?} marker) and jumps to the next one. The scan highlights the
line and word under the cursor; clicking on the scan moves the cursor to that line.
"""
from __future__ import annotations

import re

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QBrush, QColor, QFont, QImage, QKeySequence, QPainter, QPen, QPixmap,
                           QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextDocument)
from PySide6.QtWidgets import (QCheckBox, QComboBox, QMenu, QToolButton, QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene,
                               QGraphicsView, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QSpinBox, QSplitter, QToolBar, QVBoxLayout, QWidget)

from ..library import Document, Library, now_iso
from . import theme
from ..ocr import load_ocr_json, render_page
from ..textutil import COMMENT_LINE_RE, MARKER_RE, PAGE_RE, count_markers, strip_markers

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

    def __init__(self, lib: Library, doc: Document, font_size: int = 11):
        super().__init__()
        self.lib, self.doc = lib, doc
        self.ocr = load_ocr_json(lib, doc)
        self.threshold = int(self.ocr.get("threshold", lib.settings.low_conf_threshold))
        self.cur_page = None
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
        self.split.addWidget(self.text)
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
        tb.addSeparator()
        act("Prev", self.prev_flag, "Shift+F3", "Previous low-confidence word", "prev")
        act("Next flag", self.next_flag, "F3", "Next low-confidence word (selects it so you can type over it)", "flag")
        act("Accept", self.accept_here, "Ctrl+Return", "Accept the word at the cursor (removes its marker) and go to the next", "check")
        more = QToolButton()
        more.setText("More")
        more.setPopupMode(QToolButton.InstantPopup)
        mm = QMenu(more)
        mm.addAction("Accept all flagged words on this page", self.accept_page)
        mm.addAction("Accept all flagged words in document…", self.accept_all)
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
        return True

    def _on_changed(self):
        if not getattr(self, "_loading", False):
            self._count_timer.start()

    def _update_count(self):
        n = count_markers(self.text.toPlainText())
        status = {"reviewed": "reviewed ✓", "ocr": "OCR (unreviewed)"}.get(self.doc.text_status, self.doc.text_status)
        ro = "" if self.editable else "  ·  read-only (original file)"
        self.flag_label.setText(f"   {n} flagged  ·  {status}{ro}")

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
        self.ocr = load_ocr_json(self.lib, self.doc)
        self.editable = bool(self.doc.text_path) and self.doc.text_path != self.doc.path
        self.text.setReadOnly(not self.editable)
        self.a_save.setEnabled(self.editable)
        self.load_text()
        self._img_cache.clear()
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

    def _show_page(self, n: int):
        if not self.has_pages or n == self.cur_page:
            return
        self.cur_page = n
        if n not in self._img_cache:
            try:
                self._img_cache = {k: v for k, v in list(self._img_cache.items())[-4:]}
                self._img_cache[n] = render_page(self.lib, self.doc, n, dpi=150)
            except Exception:
                self._img_cache[n] = None
        pj = self.ocr.get("pages", {}).get(str(n))
        self.view.set_image(self._img_cache[n], pj.get("width") if pj else None)
        if pj:
            for line in pj.get("lines", []):
                for w in line["words"]:
                    if w["c"] < self.threshold:
                        self.view.add_box(w["b"], *box_colors("flag"), 1)
            self._static_overlay = list(self.view.overlay)
        else:
            self._static_overlay = []
        if hasattr(self, "page_spin"):
            self.page_spin.blockSignals(True)
            self.page_spin.setValue(n)
            self.page_spin.blockSignals(False)

    def _schedule_sync(self):
        self._sync_timer.start()

    def _cursor_location(self):
        """-> (page, ocr line index within page, word index within line)"""
        c = self.text.textCursor()
        block = c.block()
        page, idx = None, -1
        b = block
        while b.isValid():
            t = b.text()
            m = PAGE_RE.match(t)
            if m:
                page = int(m.group(1))
                break
            if t.strip() and not COMMENT_LINE_RE.match(t):
                idx += 1
            b = b.previous()
        if page is None:
            return None, -1, -1
        if not block.text().strip() or PAGE_RE.match(block.text()):
            return page, -1, -1
        before = block.text()[: c.position() - block.position()]
        word = len(before.split()) - (0 if before.endswith((" ", "\t")) or not before else 1)
        return page, idx, max(0, word)

    def _sync(self):
        if not self.has_pages:
            return
        page, li, wi = self._cursor_location()
        if page is None:
            return
        self._show_page(page)
        # dynamic overlay: current line + word
        for it in [i for i in self.view.overlay if i not in self._static_overlay]:
            self.view.scene().removeItem(it)
        self.view.overlay = list(self._static_overlay)
        pj = self.ocr.get("pages", {}).get(str(page))
        if not pj or li < 0 or li >= len(pj.get("lines", [])):
            return
        line = pj["lines"][li]
        self.view.add_box(line["b"], *box_colors("line"), 2)
        words = line["words"]
        if 0 <= wi < len(words):
            item = self.view.add_box(words[wi]["b"], *box_colors("word"), 2)
            self.view.ensureVisible(item, 60, 60)

    def _on_image_click(self, x: float, y: float):
        pj = self.ocr.get("pages", {}).get(str(self.cur_page))
        if not pj:
            return
        target = None
        for i, line in enumerate(pj.get("lines", [])):
            b = line["b"]
            if b[0] - 4 <= x <= b[2] + 4 and b[1] - 4 <= y <= b[3] + 4:
                target = i
                break
        if target is None:
            return
        # find that line in the text
        text_doc = self.text.document()
        text = self.text.toPlainText()
        m = next((m for m in PAGE_RE.finditer(text) if int(m.group(1)) == self.cur_page), None)
        if not m:
            return
        b = text_doc.findBlock(m.start()).next()
        idx = -1
        while b.isValid() and not PAGE_RE.match(b.text()):
            if b.text().strip() and not COMMENT_LINE_RE.match(b.text()):
                idx += 1
                if idx == target:
                    words = pj["lines"][target]["words"]
                    wi = next((k for k, w in enumerate(words) if w["b"][0] - 3 <= x <= w["b"][2] + 3), 0)
                    parts = re.finditer(r"\S+", b.text())
                    starts = [p.start() for p in parts]
                    c = self.text.textCursor()
                    c.setPosition(b.position() + (starts[wi] if wi < len(starts) else 0))
                    self.text.setTextCursor(c)
                    self.text.setFocus()
                    return
            b = b.next()
