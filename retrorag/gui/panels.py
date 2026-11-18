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

PRIMARY_ACTIONS = ("open_editor", "ocr", "open_original")
ACTIONS = [("open_editor", "Proofread", "edit"), ("ocr", "Run OCR", "scan"),
           ("ocr_force", "Force OCR (all pages)", "refresh"), ("reextract", "Re-extract text", "text"),
           ("index", "Index now", "sync"), ("open_original", "Original", "view"),
           ("show_folder", "Show in folder", "open_folder"), ("remove", "Remove…", "delete")]


class DocPanel(QWidget):
    action = Signal(str, int)     # action name, doc id
    changed = Signal(int)
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
        self.busy_ids: set = set()        # maintained by the main window
        self.busy_text = ""

        box = QGroupBox("Details")
        f = QFormLayout(box)
        f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        f.setHorizontalSpacing(12)
        f.setVerticalSpacing(8)
        self.title = QLineEdit()
        self.category = QComboBox(); self.category.setEditable(True); self.category.addItems(CATEGORIES)
        self.tags = QLineEdit(); self.tags.setPlaceholderText("comma separated, e.g. VIC-II, raster, sprites")
        self.source = QLineEdit(); self.source.setPlaceholderText("where it came from (URL, book, archive)")
        self.notes = QPlainTextEdit(); self.notes.setFixedHeight(76)
        self.notes.setPlaceholderText("Notes are indexed too - e.g. 'Rev B board, PAL', 'errata on p.12'")
        f.addRow("Title", self.title); f.addRow("Category", self.category); f.addRow("Tags", self.tags)
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
            if key in PRIMARY_ACTIONS:
                b = QPushButton(label)
                b.clicked.connect(lambda _=False, k=key: self.doc and self.action.emit(k, self.doc.id))
                bar.addWidget(b)
                self.buttons[key] = b
        more = QPushButton("More")
        menu = QMenu(more)
        for key, label, ic in ACTIONS:
            if key not in PRIMARY_ACTIONS:
                if key == "remove":
                    menu.addSeparator()
                a = menu.addAction(label)
                a.triggered.connect(lambda _=False, k=key: self.doc and self.action.emit(k, self.doc.id))
                self.buttons[key] = a
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

    def apply_icons(self):
        for key, b in self.buttons.items():
            b.setIcon(theme.icon(self._button_icons[key], theme.T["danger"] if key == "remove" else None))
        self.rel_add.setIcon(theme.icon("link"))
        self.rel_suggest.setIcon(theme.icon("search"))
        self.rel_remove.setIcon(theme.icon("delete"))
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
        for w in (self.title, self.category, self.tags, self.source, self.notes, self.save_btn, self.more_btn,
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
            self.callout.hide()
            self.busy.clear()
            self.preview.show_doc(None)
            return
        self.title.setText(d.title); self.category.setCurrentText(d.category)
        self.tags.setText(d.tags); self.source.setText(d.source_url); self.notes.setPlainText(d.notes)
        self._fill_info(d)
        self._fill_links(d)
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
        b_edit.setText(f"Proofread ({d.flagged} flagged)" if scan and d.flagged else "Proofread" if scan else "Open text")
        b_edit.setEnabled(not busy and (bool(d.text_path) and not no_text))
        b_edit.setToolTip("Run OCR first - there is no text to proofread yet." if no_text else
                          "Open the text editor (scan on the left, text on the right).")
        b_ocr.setText("Run OCR" if (d.needs_ocr or no_text) else "Re-run OCR")
        b_ocr.setEnabled(scan and not busy)
        self.buttons["ocr_force"].setEnabled(scan and not busy)
        self.buttons["reextract"].setEnabled(not busy)
        t = theme.T
        step = lambda n, of, title: f"<b>Step {n} of {of} - {title}.</b> "
        state = ""
        if busy:
            self.callout.hide()
            self._set_primary(None)
            return
        if scan and (d.needs_ocr or no_text):
            self._set_primary("ocr")
            msg = (step(1, 2, "Run OCR") + ("This is a scan with no text yet. " if no_text else
                                            "Some pages are scans without a text layer. ") +
                   "OCR reads the pages and flags any words it isn't sure about; you check those in step 2 "
                   "(Proofread).")
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

    def set_busy(self, doc_ids: set, text: str = ""):
        """Main window tells us which documents a running job is working on."""
        self.busy_ids, self.busy_text = set(doc_ids), text
        if self.doc and self.doc.id in self.busy_ids:
            self.busy.show_busy(text or "Working…")
        else:
            self.busy.clear()
        if self.doc:
            self._guide(self.doc)

    def _fill_info(self, d: Document):
        if d.id in self.busy_ids:
            self.busy.show_busy(self.busy_text or "Working…")
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
        if d.ocr_mean_conf is not None:
            rows.append(("OCR confidence", f"{d.ocr_mean_conf:.1f}% mean"))
        rows.append(("Flagged words", str(d.flagged)))
        n = self.lib.conn().execute("SELECT COUNT(*) FROM chunks WHERE doc_id=?", (d.id,)).fetchone()[0]
        rows.append(("Indexed", f"{(d.indexed or 'never').replace('T', ' ')[:16]} · {n} chunks{stale}"))
        rows += [("CRC32", d.crc32), ("MD5", d.md5), ("SHA1", d.sha1), ("Added", (d.added or "").replace("T", " ")[:16])]
        self.info.setHtml(f"<table cellspacing=3 style='color:{t['text']}'>" + "".join(
            f"<tr><td style='color:{t['muted']};padding-right:10px'>{k}</td><td>{v}</td></tr>" for k, v in rows)
            + "</table>")

    def save(self):
        if not self.doc:
            return
        self.lib.update_document(self.doc.id, title=self.title.text().strip() or self.doc.title,
                                 category=self.category.currentText().strip() or self.doc.category,
                                 tags=self.tags.text().strip(), source_url=self.source.text().strip(),
                                 notes=self.notes.toPlainText().strip(), text_updated=now_iso())
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
        for s in self.lib.systems():
            self.system.addItem(s.name, s.name)
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
