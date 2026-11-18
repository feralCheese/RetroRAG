"""Preview pane shown beside the Document form.

  * Scanned PDFs / images with OCR or extracted text: toggle between Original (the page image) and Processed
    (the text that goes into the RAG, with low-confidence words highlighted).
  * Plain text, source files and pictures without OCR text: Original only.
  * Binaries (firmware, ROMs, disk/tape images) and files that are part of an emulator/firmware source tree:
    no preview - the pane is hidden.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel, QPlainTextEdit, QStackedWidget, QToolButton,
                               QVBoxLayout, QWidget)

from ..library import Document, Library
from ..ocr import render_page
from ..textutil import PAGE_RE, count_markers, read_text_file
from . import theme
from .editor import MarkerHighlighter, PageView

MAX_PREVIEW_CHARS = 400_000
SOURCE_TREE_CATEGORIES = ("Emulator Source", "Firmware & ROMs")


def wants_preview(d: Document | None) -> bool:
    if d is None or d.kind == "binary":                       # firmware, ROMs, disk & tape images
        return False
    if d.category in SOURCE_TREE_CATEGORIES and d.group_path:  # one file of a multi-file source tree
        return False
    return True


def has_processed(d: Document) -> bool:
    """A separate processed (OCR / extracted) text exists for this scan."""
    return d.kind in ("pdf", "image") and bool(d.text_path) and d.text_status != "none"


class PreviewImageView(PageView):
    """Whole page fitted in view until the user zooms (Ctrl+wheel); a new page re-fits."""

    def __init__(self):
        super().__init__(fit_mode="page")

    def set_image(self, img, ocr_width=None):
        self.user_zoomed = False
        super().set_image(img, ocr_width)


class PreviewPane(QWidget):
    open_editor = Signal(int)

    def __init__(self, lib: Library):
        super().__init__()
        self.lib = lib
        self.doc: Document | None = None
        self.mode = "original"          # remembered across documents
        self.page = 1
        self._text_loaded_for = None    # (doc id, mode, text_updated) currently in the text view

        v = QVBoxLayout(self)
        v.setContentsMargins(4, 0, 0, 0)
        bar = QHBoxLayout()
        self.caption = QLabel("<b>Preview</b>")
        self.caption.setContentsMargins(0, 0, 6, 0)
        bar.addWidget(self.caption)
        self.btn_orig = QToolButton(text="Original", checkable=True)
        self.btn_proc = QToolButton(text="Processed", checkable=True)
        self.btn_orig.setObjectName("segLeft")
        self.btn_proc.setObjectName("segRight")
        self.btn_orig.setToolTip("The original file as added")
        self.btn_proc.setToolTip("The text that goes into the RAG (OCR or extracted). Highlighted words are low-confidence.")
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        for b in (self.btn_orig, self.btn_proc):
            grp.addButton(b)
            bar.addWidget(b)
        self.btn_orig.clicked.connect(lambda: self.set_mode("original"))
        self.btn_proc.clicked.connect(lambda: self.set_mode("processed"))
        bar.addSpacing(8)
        bar.addStretch()
        self.btn_prev = QToolButton()
        self.btn_prev.setToolTip("Previous page")
        self.page_label = QLabel()
        self.page_label.setObjectName("muted")
        self.btn_next = QToolButton()
        self.btn_next.setToolTip("Next page")
        self.btn_prev.clicked.connect(lambda: self.goto_page(self.page - 1))
        self.btn_next.clicked.connect(lambda: self.goto_page(self.page + 1))
        for w in (self.btn_prev, self.page_label, self.btn_next):
            bar.addWidget(w)
        self.btn_edit = QToolButton(text="Open in editor")
        self.btn_edit.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_edit.clicked.connect(lambda: self.doc and self.open_editor.emit(self.doc.id))
        bar.addWidget(self.btn_edit)
        v.addLayout(bar)

        self.stack = QStackedWidget()
        self.image = PreviewImageView()
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(theme.mono_font(10))
        self.hl = MarkerHighlighter(self.text.document())
        self.stack.addWidget(self.image)
        self.stack.addWidget(self.text)
        v.addWidget(self.stack, 1)
        self.note = QLabel()
        self.note.setObjectName("muted")
        v.addWidget(self.note)
        self.apply_icons()

    def restyle(self):
        self.text.setFont(theme.mono_font(10))
        self.hl.refresh_formats()
        self.apply_icons()
        self._text_loaded_for = None
        if self.doc:
            self.show_doc(self.doc)

    def apply_icons(self):
        self.btn_prev.setIcon(theme.icon("prev"))
        self.btn_next.setIcon(theme.icon("next"))
        self.btn_edit.setIcon(theme.icon("edit"))

    # ------------------------------------------------------------------ public
    def show_doc(self, d: Document | None) -> None:
        """Show a document (or hide the pane). Re-showing the same document keeps page and mode."""
        if not wants_preview(d):
            self.doc = None
            self.hide()
            return
        same = self.doc is not None and self.doc.id == d.id
        self.doc = d
        if not same:
            self.page = 1
            self._text_loaded_for = None
        self.page = max(1, min(self.page, self.page_count()))
        proc = has_processed(d)
        self.btn_orig.setVisible(proc)
        self.btn_proc.setVisible(proc)
        self.caption.setText("<b>Preview</b>" if proc else "<b>Original</b>")
        self.btn_edit.setVisible(bool(d.text_path) or d.kind in ("pdf", "image"))
        self.show()
        self._render(self.mode if proc else "original")

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        if self.doc:
            self._render(mode if has_processed(self.doc) else "original")

    def goto_page(self, n: int) -> None:
        if not self.doc:
            return
        self.page = max(1, min(n, self.page_count()))
        self._render(self.mode if has_processed(self.doc) else "original")

    def page_count(self) -> int:
        return max(1, self.doc.pages or 1) if self.doc and self.doc.kind in ("pdf", "image") else 1

    # ------------------------------------------------------------------ rendering
    def _render(self, mode: str) -> None:
        d = self.doc
        self.btn_orig.setChecked(mode == "original")
        self.btn_proc.setChecked(mode == "processed")
        paged = d.kind in ("pdf", "image") and self.page_count() > 1
        for w in (self.btn_prev, self.page_label, self.btn_next):
            w.setVisible(paged)
        self.page_label.setText(f"page {self.page} / {self.page_count()}")
        self.btn_prev.setEnabled(self.page > 1)
        self.btn_next.setEnabled(self.page < self.page_count())
        note = ""
        if mode == "original" and d.kind in ("pdf", "image"):
            try:
                img = render_page(self.lib, d, self.page, dpi=120)
            except Exception as e:
                img, note = None, f"Could not render: {e}"
            self.image.set_image(img, None)
            self.stack.setCurrentWidget(self.image)
            if not note and not has_processed(d):
                note = "No text yet - run OCR to create a processed version."
        else:
            converted = d.kind == "text" and d.text_path and d.text_path != d.path    # RTF / HTML
            if mode == "original" and converted:
                path, key = self.lib.abspath(d.text_path), (d.id, "converted", d.text_updated)
                note = f"Converted from {d.ext.lstrip('.').upper()} - open the original file to see its formatting."
            elif mode == "original":           # text / code: the file itself
                path, key = self.lib.abspath(d.path), (d.id, "original", None)
            else:
                path, key = self.lib.abspath(d.text_path), (d.id, "processed", d.text_updated)
            if key != self._text_loaded_for:
                try:
                    txt = read_text_file(path)
                except Exception as e:
                    txt = f"(Could not read {path.name}: {e})"
                if len(txt) > MAX_PREVIEW_CHARS:
                    txt = txt[:MAX_PREVIEW_CHARS] + "\n\n[... preview truncated - open in the editor for the full text]"
                self.text.setPlainText(txt)
                self._text_loaded_for = key
            self.stack.setCurrentWidget(self.text)
            if mode == "processed":
                n = count_markers(self.text.toPlainText())
                note = f"{n} low-confidence word(s) highlighted" if n else "Text used for search (no flagged words)"
                self._scroll_to_page(self.page)
        self.note.setText(note)
        self.note.setVisible(bool(note))

    def _scroll_to_page(self, n: int) -> None:
        text = self.text.toPlainText()
        m = next((m for m in PAGE_RE.finditer(text) if int(m.group(1)) == n), None)
        c = self.text.textCursor()
        c.setPosition(m.start() if m else 0)
        self.text.setTextCursor(c)
        # put the page marker at the top of the view
        self.text.moveCursor(QTextCursor.End)
        self.text.setTextCursor(c)
        self.text.ensureCursorVisible()
