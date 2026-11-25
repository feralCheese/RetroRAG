"""Document panel (details form + preview pane) and the search tester."""
from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QUrl
from PySide6.QtWidgets import (QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox,
                               QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter, QTextBrowser,
                               QVBoxLayout, QWidget)

from ..config import CATEGORIES
from ..library import Document, Library, now_iso
from . import theme
from .preview import PreviewPane


def human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


STATUS_TEXT = {"none": "no text yet", "extracted": "text extracted", "partial": "partly extracted (some pages need OCR)",
               "ocr": "OCR text (not yet proofread)", "reviewed": "proofread ✓", "info": "info card (binary file)"}

PRIMARY_ACTIONS = ("ocr", "ocr_layer", "open_editor", "open_original")
BAR_ACTIONS = ("ocr", "open_editor", "open_original", "no_ocr")   # buttons; the rest are in More
CHOICE_ACTIONS = ("ocr_layer", "keep_layer")       # under the note that asks: OCR a PDF's own text, or keep it
# in the order of the steps: OCR (or say it isn't needed, or keep the PDF's own text), then proofread
ACTIONS = [("ocr", "Run OCR", "scan"), ("ocr_layer", "OCR with Tesseract", "scan"),
           ("keep_layer", "Keep the PDF's text", "check"), ("no_ocr", "OCR not needed", "check"),
           ("open_editor", "Proofread", "edit"),
           ("ocr_force", "Force OCR (all pages)", "refresh"), ("reextract", "Re-extract text", "text"),
           ("index", "Index now", "sync"), ("open_original", "Original", "view"),
           ("show_folder", "Show in folder", "open_folder"), ("remove", "Remove…", "delete")]


class DocPanel(QWidget):
    action = Signal(str, int)     # action name, doc id
    changed = Signal(int)
    docs_added = Signal(list)     # documents added from a disk / tape image
    create_image = Signal(int)    # system id
    open_doc = Signal(int)        # navigate to a linked document
    links_changed = Signal()
    cancel_job = Signal()

    def __init__(self, lib: Library):
        super().__init__()
        self.lib = lib
        self.doc: Document | None = None
        outer = QHBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        self.split = QSplitter(Qt.Horizontal)
        self.split.setChildrenCollapsible(False)
        outer.addWidget(self.split)

        # ---------------- left: form (scrollable so it works in narrow windows)
        form_host = QWidget()
        v = QVBoxLayout(form_host)
        v.setContentsMargins(0, 0, 6, 0)
        v.setSpacing(4)
        self.title_lbl = QLabel("Select a document")
        self.title_lbl.setMinimumWidth(10)
        self.title_lbl.setObjectName("heading")
        self.title_lbl.setWordWrap(True)
        self.sub_lbl = QLabel("Choose a document in the library tree to see its details and preview.")
        self.sub_lbl.setObjectName("muted")
        self.sub_lbl.setWordWrap(True)
        self.sub_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.title_lbl)
        v.addWidget(self.sub_lbl)
        from .widgets import BusyBanner
        self.busy = BusyBanner()
        self.busy.cancel.connect(self.cancel_job.emit)
        v.addWidget(self.busy)
        self.callout = QLabel()
        self.callout.setObjectName("callout")
        self.callout.setWordWrap(True)
        self.callout.setTextFormat(Qt.RichText)
        self.callout.hide()
        v.addWidget(self.callout)
        self.choice_row = QWidget()                  # the answers to the note's question, right under it
        choice_l = QHBoxLayout(self.choice_row)
        choice_l.setContentsMargins(0, 2, 0, 2)
        choice_l.setSpacing(6)
        self.choice_row.hide()
        v.addWidget(self.choice_row)
        self.busy_map: dict = {}          # {doc id: what a job is doing to it}, maintained by the main window
        self.busy_ids: set = set()

        box = QGroupBox("Details")
        f = QFormLayout(box)
        f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        f.setHorizontalSpacing(12)
        f.setVerticalSpacing(8)
        self.title = QLineEdit()
        self.system_box = QComboBox()
        self.system_box.setToolTip("Change this (and Save) to move the document to another system or variant")
        self.category = QComboBox(); self.category.setEditable(True); self.category.addItems(CATEGORIES)
        self.tags = QLineEdit(); self.tags.setPlaceholderText("comma separated, e.g. VIC-II, raster, sprites")
        self.source = QLineEdit(); self.source.setPlaceholderText("where it came from (URL, book, archive)")
        self.notes = QPlainTextEdit(); self.notes.setFixedHeight(76)
        self.notes.setPlaceholderText("Notes are indexed too - e.g. 'Rev B board, PAL', 'errata on p.12'")
        f.addRow("Title", self.title); f.addRow("System", self.system_box); f.addRow("Category", self.category)
        f.addRow("Tags", self.tags)
        f.addRow("Source", self.source); f.addRow("Notes", self.notes)
        self.save_btn = QPushButton("Save details")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self.save)
        self.saved_lbl = QLabel("")
        self.saved_lbl.setObjectName("savedFlash")
        row = QHBoxLayout(); row.addStretch(); row.addWidget(self.saved_lbl); row.addWidget(self.save_btn)
        f.addRow(row)

        # command bar: the common actions as buttons, the rest under "More"
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.buttons: dict = {}
        self._button_icons: dict[str, str] = {}
        self._more_actions: dict = {}
        for key, label, ic in ACTIONS:
            self._button_icons[key] = ic
            if key in BAR_ACTIONS or key in CHOICE_ACTIONS:
                b = QPushButton(label)
                b.clicked.connect(lambda _=False, k=key: self.doc and self.action.emit(k, self.doc.id))
                (choice_l if key in CHOICE_ACTIONS else bar).addWidget(b)
                self.buttons[key] = b
        choice_l.addStretch()
        more = QPushButton("More")
        menu = QMenu(more)
        self.buttons["no_ocr"].setToolTip("Nothing worth reading in it (a photo of a board, a logo...): take it off "
                                          "the list of documents that need OCR. Run OCR still works on it later.")
        self.buttons["ocr_layer"].setToolTip("Read the scanned pages again with Tesseract, which flags the words it "
                                             "isn't sure of for you to check. Pages you've edited are left alone.")
        self.buttons["keep_layer"].setToolTip("Use the text that came with the PDF as it is. You can still OCR "
                                              "those pages later: More > OCR the pages that came with text.")
        for key, label, ic in ACTIONS:
            if key not in BAR_ACTIONS and key not in CHOICE_ACTIONS:
                if key == "remove":
                    menu.addSeparator()
                a = menu.addAction(label)
                a.triggered.connect(lambda _=False, k=key: self.doc and self.action.emit(k, self.doc.id))
                self.buttons[key] = a
                if key == "ocr_force":       # after 'Keep the PDF's text', the way to change your mind
                    self.layer_menu = menu.addAction("OCR the pages that came with text")
                    self.layer_menu.triggered.connect(
                        lambda _=False: self.doc and self.action.emit("ocr_layer", self.doc.id))
        more.setMenu(menu)
        self.more_btn = more
        bar.addWidget(more)
        bar.addStretch()
        v.addSpacing(6)
        v.addLayout(bar)
        v.addWidget(box)

        rel_box = QGroupBox("Related documents")
        rv = QVBoxLayout(rel_box)
        self.rel_list = QListWidget()
        self.rel_list.setToolTip("Double-click to open the linked document")
        self.rel_list.itemDoubleClicked.connect(lambda it: self.open_doc.emit(it.data(Qt.UserRole)))
        rb = QHBoxLayout()
        self.rel_add = QPushButton("Link…")
        self.rel_add.setToolTip("Link this document to another (pinout, datasheet, source code, errata, revision…)")
        self.rel_add.clicked.connect(self._add_link)
        self.rel_suggest = QPushButton("Suggest links")
        self.rel_suggest.setToolTip("Find documents that mention the same chips, part numbers or register addresses")
        self.rel_suggest.clicked.connect(self._suggest_links)
        self.rel_remove = QPushButton("Unlink")
        self.rel_remove.clicked.connect(self._remove_link)
        for b in (self.rel_add, self.rel_suggest, self.rel_remove):
            rb.addWidget(b)
        rb.addStretch()
        rv.addLayout(rb)
        rv.addWidget(self.rel_list)
        v.addWidget(rel_box)

        from .media_ui import ImageContents
        self.image_box = ImageContents(lib)
        self.image_box.changed.connect(self.docs_added.emit)
        self.image_box.create_like.connect(self.create_image.emit)
        v.addWidget(self.image_box)

        info_box = QGroupBox("File information")
        iv = QVBoxLayout(info_box)
        self.info = QTextBrowser()
        self.info.setFrameShape(QTextBrowser.NoFrame)
        self.info.setStyleSheet("QTextBrowser { border: none; padding: 0; background: transparent; }")
        iv.addWidget(self.info)
        v.addWidget(info_box, 1)

        scroll = QScrollArea()
        scroll.setWidget(form_host)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; } QScrollArea > QWidget > QWidget { background: transparent; }")
        scroll.setMinimumWidth(360)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.split.addWidget(scroll)

        # ---------------- right: preview
        self.preview = PreviewPane(lib)
        self.preview.open_editor.connect(lambda i: self.action.emit("open_editor", i))
        self.preview.setMinimumWidth(300)
        self.split.addWidget(self.preview)
        self.split.setStretchFactor(0, 2)
        self.split.setStretchFactor(1, 3)
        self.preview.hide()
        self._set_form_enabled(False)
        self.apply_icons()

    def refresh_systems(self):
        from .dialogs import system_choices
        cur = self.system_box.currentData()
        self.system_box.blockSignals(True)
        self.system_box.clear()
        for label, sid in system_choices(self.lib):
            self.system_box.addItem(label, sid)
        want = self.doc.system_id if self.doc else cur
        self.system_box.setCurrentIndex(max(0, self.system_box.findData(want)))
        self.system_box.blockSignals(False)

    def apply_icons(self):
        for key, b in self.buttons.items():
            b.setIcon(theme.icon(self._button_icons[key], theme.T["danger"] if key == "remove" else None))
        self.rel_add.setIcon(theme.icon("link"))
        self.rel_suggest.setIcon(theme.icon("search"))
        self.rel_remove.setIcon(theme.icon("delete"))
        self.image_box.apply_icons()
        if self.doc:
            self._fill_links(self.doc)

    # ------------------------------------------------------------------ links
    def _fill_links(self, d: Document):
        from ..relations import related
        self.rel_list.clear()
        links = related(self.lib, d.id)
        for l in links:
            other_sys = "" if (s := self.lib.system(d.system_id)) and s.name == l["system"] else f"  [{l['system']}]"
            it = QListWidgetItem(theme.icon("link", theme.T["muted"]),
                                 f"{l['relation']}:  {l['title']}{other_sys}" + (f"  - {l['note']}" if l["note"] else ""))
            it.setData(Qt.UserRole, l["doc_id"])
            it.setData(Qt.UserRole + 1, l["rel_id"])
            it.setToolTip(f"{l['relation']} {l['title']}\n{l['system']} · {l['category']}"
                          + (f"\nNote: {l['note']}" if l["note"] else "")
                          + ("\n(accepted suggestion)" if l["origin"] == "suggested" else ""))
            self.rel_list.addItem(it)
        if not links:
            it = QListWidgetItem("No links yet - use Link… or Suggest links.")
            it.setFlags(Qt.NoItemFlags)
            self.rel_list.addItem(it)
        self.rel_remove.setEnabled(bool(links))
        row_h = max(22, self.rel_list.sizeHintForRow(0))
        self.rel_list.setFixedHeight(min(6, max(1, len(links))) * row_h + 8)

    def _add_link(self):
        if not self.doc:
            return
        from .links import LinkDialog
        if LinkDialog(self, self.lib, self.doc.id).exec():
            self._fill_links(self.doc)
            self.links_changed.emit()

    def _remove_link(self):
        it = self.rel_list.currentItem()
        rel_id = it.data(Qt.UserRole + 1) if it else None
        if not rel_id:
            QMessageBox.information(self, "Unlink", "Select a link in the list first.")
            return
        from ..relations import remove_relation
        remove_relation(self.lib, rel_id)
        self._fill_links(self.doc)
        self.links_changed.emit()

    def _suggest_links(self):
        if not self.doc:
            return
        from PySide6.QtWidgets import QApplication
        from ..relations import suggest
        from .links import SuggestionsDialog
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            sugs = suggest(self.lib, self.doc.id, limit=30, log=lambda m: None)
        finally:
            QApplication.restoreOverrideCursor()
        dlg = SuggestionsDialog(self, self.lib, sugs, f"Suggested links for {self.doc.title}")
        dlg.exec()
        if dlg.changed:
            self._fill_links(self.doc)
            self.links_changed.emit()

    def _set_form_enabled(self, on: bool):
        for w in (self.title, self.system_box, self.category, self.tags, self.source, self.notes, self.save_btn, self.more_btn,
                  self.rel_add, self.rel_suggest, self.rel_remove, self.rel_list, *self.buttons.values()):
            w.setEnabled(on)

    # ------------------------------------------------------------------ show
    def show_doc(self, d: Document | None):
        self.doc = d
        self._set_form_enabled(d is not None)
        if not d:
            self.title_lbl.setText("Select a document")
            self.sub_lbl.setText("Choose a document in the library tree to see its details and preview.")
            for w in (self.title, self.tags, self.source):
                w.clear()
            self.notes.clear()
            self.info.clear()
            self.rel_list.clear()
            self.system_box.setCurrentIndex(-1)
            self.callout.hide()
            self.choice_row.hide()
            self.busy.clear()
            self.image_box.show_doc(None)
            self.preview.show_doc(None)
            return
        self.title.setText(d.title); self.category.setCurrentText(d.category)
        if self.system_box.findData(d.system_id) < 0:
            self.refresh_systems()
        self.system_box.setCurrentIndex(max(0, self.system_box.findData(d.system_id)))
        self.tags.setText(d.tags); self.source.setText(d.source_url); self.notes.setPlainText(d.notes)
        self._fill_info(d)
        self._fill_links(d)
        self.image_box.lib = self.lib
        self.image_box.show_doc(d)
        self.preview.show_doc(d)

    def refresh_current(self):
        """Re-read the current document after OCR / edits without discarding unsaved form edits."""
        if not self.doc:
            return
        d = self.lib.document(self.doc.id)
        if not d:
            self.show_doc(None)
            return
        self.doc = d
        self._fill_info(d)
        self._fill_links(d)
        self.image_box.lib = self.lib
        self.image_box.show_doc(d)
        self.preview.show_doc(d)

    # ------------------------------------------------------------------ guidance
    def _set_primary(self, key: str | None):
        """Highlight the button for the next sensible step (OCR before proofreading)."""
        for k in PRIMARY_ACTIONS:
            b = self.buttons[k]
            want = "primary" if k == key else ""
            if b.objectName() != want:
                b.setObjectName(want)
                b.style().unpolish(b)
                b.style().polish(b)

    def _guide(self, d: Document):
        """Label the actions for this document's state and say what to do next."""
        scan = d.kind in ("pdf", "image")
        busy = d.id in self.busy_ids
        no_text = scan and d.text_status == "none"
        b_edit, b_ocr = self.buttons["open_editor"], self.buttons["ocr"]
        from ..notes import is_note
        b_edit.setText(f"Proofread ({d.flagged} flagged)" if scan and d.flagged else "Proofread" if scan else
                       "Edit note" if is_note(d) else "Open text")
        b_edit.setEnabled(not busy and (bool(d.text_path) and not no_text))
        b_edit.setToolTip("Run OCR first - there is no text to proofread yet." if no_text else
                          "Open the text editor (scan on the left, text on the right).")
        b_ocr.setText("Run OCR" if (d.needs_ocr or no_text) else "Re-run OCR")
        b_ocr.setEnabled(scan and not busy)
        from ..ingest import text_layer_state
        layer = text_layer_state(self.lib, d) if d.kind == "pdf" else {}
        pending = layer.get("pending") or []
        choose = bool(pending) and not layer.get("keep") and not (d.needs_ocr or no_text)
        b_ocr.setVisible(not choose)
        for k in CHOICE_ACTIONS:
            self.buttons[k].setEnabled(not busy)
        self.choice_row.setVisible(choose and not busy)
        self.layer_menu.setVisible(bool(pending))
        self.layer_menu.setEnabled(not busy)
        self.buttons["no_ocr"].setVisible(scan and bool(d.needs_ocr))
        self.buttons["no_ocr"].setEnabled(not busy)
        self.buttons["ocr_force"].setEnabled(scan and not busy)
        self.buttons["reextract"].setEnabled(not busy)
        t = theme.T
        step = lambda n, of, title: f"<b>Step {n} of {of} - {title}.</b> "
        state = ""
        if busy:
            self.callout.hide()
            self._set_primary(None)
            return
        from ..categorize import PHOTOS
        if no_text and not d.needs_ocr:          # a photo, or marked 'OCR not needed'
            self._set_primary(None)
            b_ocr.setText("Run OCR")
            what = ("<b>A photo.</b> Photos aren't OCR'd" if d.kind == "image" and d.category == PHOTOS else
                    "<b>No OCR needed.</b> It's marked as having nothing worth reading")
            msg, state = (what + ": an AI that can see pictures looks at it with <b>view_page</b>. To help "
                          "searches find it, describe what it shows in <b>Notes</b>, such as the board revision, "
                          "chip markings or jumper settings. If it has writing worth reading, such as a label, "
                          "<b>Run OCR</b> still works."), "done"
        elif scan and (d.needs_ocr or no_text):
            self._set_primary("ocr")
            from ..ocr import pages_left
            left = pages_left(self.lib, d) if d.text_status == "ocr" else []      # some pages OCR'd already
            msg = (step(1, 2, "Run OCR") + (
                f"{len(left)} of the {d.pages} pages haven't been OCR'd yet. <b>Run OCR</b> reads just those and "
                "leaves the pages already read (and anything you've corrected) alone. "
                if left and len(left) < (d.pages or 0) else
                "This is a scan with no text yet. " if no_text else "Some pages are scans without a text layer. ") +
                   "OCR reads the pages and flags any words it isn't sure about; you check those in step 2 "
                   "(Proofread).")
            if pending and not layer.get("keep"):
                msg += (f" The {len(pending)} scanned page(s) that came with their own text are left as they are "
                        "for now: you choose what to do with those next.")
        elif choose:
            self._set_primary("ocr_layer")
            by = html.escape(layer.get("by") or "")
            msg = ("<b>This PDF came with its own text.</b> "
                   + (f"{len(pending)} of its {d.pages} pages are scans" if len(pending) < (d.pages or 0) else
                      f"Its {len(pending)} pages are scans")
                   + " that already carry text from " + (f"the OCR in <i>{by}</i>" if by else "the scanner's OCR")
                   + ", and RetroRAG is using that. It's usually close, but its mistakes aren't flagged, so "
                   "Proofread can't point you to them.<br><b>OCR with Tesseract</b> reads those pages again (about "
                   "a second a page) and flags the words it isn't sure of for you to check. <b>Keep the PDF's "
                   "text</b> uses it as it is.")
        elif scan and d.flagged:
            self._set_primary("open_editor")
            msg = (step(2, 2, "Proofread") + f"OCR flagged {d.flagged} word(s) it wasn't sure about. Click "
                   "<b>Proofread</b>, press <b>F3</b> to jump to each one, type the fix, <b>Ctrl+Enter</b> to "
                   "accept it.")
        elif scan and d.text_status == "ocr":
            self._set_primary("open_editor")
            msg = ("<b>Almost done.</b> No flagged words left. Have a final look in <b>Proofread</b> and click "
                   "<b>Mark reviewed</b> when you're happy.")
        else:
            self._set_primary(None)
            msg, state = ("&#10003; Proofread. " if d.text_status == "reviewed" else "") + (
                "The AI can search this document." if not d.is_stale else ""), "done"
        if d.is_stale:
            msg += ("<br>" if msg else "") + (f"<span style='color:{t['muted']}'>Changes aren't searchable yet - "
                                              "indexing runs automatically, or press F5.</span>")
        self.callout.setProperty("state", state)
        self.callout.style().unpolish(self.callout)
        self.callout.style().polish(self.callout)
        self.callout.setText(msg)
        self.callout.setVisible(bool(msg))

    def set_busy(self, busy: dict):
        """Main window tells us which documents jobs are working on, or waiting to: {doc id: what's happening}."""
        self.busy_map, self.busy_ids = dict(busy), set(busy)
        if self.doc and self.doc.id in self.busy_ids:
            self.busy.show_busy(self.busy_map[self.doc.id] or "Working…")
        else:
            self.busy.clear()
        if self.doc:
            self._guide(self.doc)

    def _fill_info(self, d: Document):
        if d.id in self.busy_ids:
            self.busy.show_busy(self.busy_map[d.id] or "Working…")
        else:
            self.busy.clear()
        s = self.lib.system(d.system_id)
        self.title_lbl.setText(d.title)
        self.sub_lbl.setText(" · ".join(x for x in (s.name if s else "", d.category, d.group_path) if x))
        self._guide(d)
        t = theme.T
        p = self.lib.abspath(d.path)
        linked = Path(d.path).is_absolute()
        stale = f"  <span style='color:{t['warn']}'>● out of date</span>" if d.is_stale else ""
        rows = [("File", html.escape(p.name) + (f" <i style='color:{t['muted']}'>(linked in place)</i>" if linked else "")),
                ("Location", html.escape(str(p.parent))), ("Type", f"{d.kind} ({d.ext or 'no extension'})"),
                ("Size", human_size(d.size)), ("Pages", str(d.pages or "–")),
                ("Text", STATUS_TEXT.get(d.text_status, d.text_status)), ("Needs OCR", "yes" if d.needs_ocr else "no")]
        if d.kind == "pdf" and d.text_layer:
            rows.append(("PDF's own text", self._layer_text(d)))
        if d.ocr_mean_conf is not None:
            rows.append(("OCR confidence", f"{d.ocr_mean_conf:.1f}% mean"))
        rows.append(("Flagged words", str(d.flagged)))
        n = self.lib.conn().execute("SELECT COUNT(*) FROM chunks WHERE doc_id=?", (d.id,)).fetchone()[0]
        rows.append(("Indexed", f"{(d.indexed or 'never').replace('T', ' ')[:16]} · {n} chunks{stale}"))
        rows += [("CRC32", d.crc32), ("MD5", d.md5), ("SHA1", d.sha1), ("Added", (d.added or "").replace("T", " ")[:16])]
        self.info.setHtml(f"<table cellspacing=3 style='color:{t['text']}'>" + "".join(
            f"<tr><td style='color:{t['muted']};padding-right:10px'>{k}</td><td>{v}</td></tr>" for k, v in rows)
            + "</table>")

    def _layer_text(self, d: Document) -> str:
        """The Details row on where a PDF's text came from."""
        from ..ingest import text_layer_state
        st = text_layer_state(self.lib, d)
        if "error" in st:
            return "couldn't tell"
        own, scans, n = st.get("text", 0), len(st.get("scans") or []), d.pages or 0
        by = html.escape(st.get("by") or "")
        if not own:
            return "none: every page needs OCR" if n else "none"
        of = lambda k: f"all {k} pages" if k == n else f"{k} of {n} pages"
        parts = []
        if scans:
            parts.append(f"{of(scans)} are scans carrying OCR text" + (f" from <i>{by}</i>" if by else ""))
            if st.get("redone"):
                parts.append(f"{st['redone']} of them read again by Tesseract")
            if st.get("pending") and st.get("keep"):
                parts.append("the rest kept as they are (your choice)")
        if own - scans:
            parts.append(f"{of(own - scans)} have digital text" + (f" (<i>{by}</i>)" if by and not scans else "")
                         + ": exact, no OCR needed")
        return "; ".join(parts)

    def save(self):
        if not self.doc:
            return
        new_sys = self.system_box.currentData() or self.doc.system_id
        new_cat = self.category.currentText().strip() or self.doc.category
        self.lib.update_document(self.doc.id, title=self.title.text().strip() or self.doc.title,
                                 tags=self.tags.text().strip(), source_url=self.source.text().strip(),
                                 notes=self.notes.toPlainText().strip(), text_updated=now_iso())
        if new_sys != self.doc.system_id or new_cat != self.doc.category:
            # files follow: systems/<system>/<category>/...
            self.lib.move_documents([self.doc.id], new_sys, new_cat, log=lambda m: None)
        d = self.lib.document(self.doc.id)
        if d.kind == "binary":
            from ..ingest import refresh_info_cards
            refresh_info_cards(self.lib, d)
        self.show_doc(self.lib.document(d.id))
        self.changed.emit(d.id)
        from PySide6.QtCore import QTimer
        self.saved_lbl.setText("\u2713 Saved")
        QTimer.singleShot(2500, lambda: self.saved_lbl.setText(""))


class SearchPanel(QWidget):
    open_doc = Signal(int, int)    # doc id, page

    def __init__(self, lib: Library):
        super().__init__()
        self.lib = lib
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)
        title = QLabel("Search test"); title.setObjectName("heading")
        sub = QLabel("Exactly what Claude or your local AI receives when it searches the library.")
        sub.setObjectName("muted")
        v.addWidget(title); v.addWidget(sub)
        h = QHBoxLayout()
        self.q = QLineEdit(); self.q.setPlaceholderText("e.g.  How do I set up a raster interrupt?   or   $D418")
        self.search_action = self.q.addAction(theme.icon("search"), QLineEdit.LeadingPosition)
        self.q.returnPressed.connect(self.run)
        self.system = QComboBox(); self.category = QComboBox()
        self.mode = QComboBox(); self.mode.addItems(["hybrid", "keyword", "semantic"])
        self.k = QSpinBox(); self.k.setRange(1, 50); self.k.setValue(8); self.k.setPrefix("top ")
        b = QPushButton("Search"); b.setObjectName("primary"); b.clicked.connect(self.run)
        h.addWidget(self.q, 3); h.addWidget(self.system, 1); h.addWidget(self.category, 1)
        h.addWidget(self.mode); h.addWidget(self.k); h.addWidget(b)
        v.addLayout(h)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        v.addWidget(self.status)
        self.out = QTextBrowser(); self.out.setOpenLinks(False)
        self.out.anchorClicked.connect(self._link)
        v.addWidget(self.out, 1)
        self.refresh_filters()

    def apply_icons(self):
        self.search_action.setIcon(theme.icon("search", theme.T["muted"]))

    def refresh_filters(self):
        cur_s, cur_c = self.system.currentText(), self.category.currentText()
        self.system.clear(); self.system.addItem("All systems", None)
        from .dialogs import system_choices
        for label, sid in system_choices(self.lib):
            self.system.addItem(label, self.lib.system(sid).name)
        self.system.setToolTip("A variant includes the documents common to its family")
        self.category.clear(); self.category.addItem("All categories", None)
        for c in CATEGORIES:
            self.category.addItem(c, c)
        self.system.setCurrentIndex(max(0, self.system.findText(cur_s)))
        self.category.setCurrentIndex(max(0, self.category.findText(cur_c)))

    def run(self):
        from ..search import search
        q = self.q.text().strip()
        if not q:
            return
        self.lib.reload_settings()
        t = theme.T
        try:
            res = search(self.lib, q, k=self.k.value(), system=self.system.currentData(),
                         category=self.category.currentData(), mode=self.mode.currentText())
        except Exception as e:
            self.out.setHtml(f"<p style='color:{t['danger']}'>{html.escape(str(e))}</p>")
            return
        emb = self.lib.get_meta("embed_signature", "") or "none"
        self.status.setText(f"{len(res)} result(s)  ·  embeddings: {emb}")
        badge_col = {"both": t["ok"], "keyword": t["accent"], "semantic": t["warn"]}
        parts = []
        for r in res:
            parts.append(
                f"<table width='100%' cellpadding=10 cellspacing=0 style='background:{t['surface']};margin-bottom:10px'>"
                f"<tr><td><a style='color:{t['accent']};text-decoration:none' href='doc:{r.doc_id}:{r.page or 0}'>"
                f"<b>{html.escape(r.citation())}</b></a>&nbsp;&nbsp;"
                f"<span style='color:{badge_col.get(r.matched, t['muted'])}'>● {r.matched}</span>"
                f"<br><span style='color:{t['muted']}'>{html.escape(r.category)}"
                f"{' · ' + html.escape(r.heading) if r.heading else ''} · score {r.score}</span>"
                f"<pre style='white-space:pre-wrap;margin-top:6px;color:{t['text']};font-family:\"{t['mono']}\"'>"
                f"{html.escape(r.text[:1500])}</pre></td></tr></table>")
        self.out.setHtml("".join(parts) or f"<p style='color:{t['muted']}'>No results.</p>")

    def _link(self, url: QUrl):
        s = url.toString()
        if s.startswith("doc:"):
            _, did, page = s.split(":")
            self.open_doc.emit(int(did), int(page))
