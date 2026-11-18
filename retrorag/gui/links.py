"""Dialogs for linking documents: add a link by hand, and review suggested links."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..library import Library
from ..relations import KINDS, SYMMETRIC, RelationError, Suggestion, add_relation, dismiss
from . import theme


def phrases() -> list[tuple[str, str, bool]]:
    """Every way to read a link from 'this' document's side: (label, kind, this_is_source)."""
    out = []
    for kind, (fwd, inv) in KINDS.items():
        out.append((fwd, kind, True))
        if kind not in SYMMETRIC:
            out.append((inv, kind, False))
    return out


def phrase_combo(kind: str = "related", this_is_src: bool = True) -> QComboBox:
    cb = QComboBox()
    for label, k, fwd in phrases():
        cb.addItem(label, (k, fwd))
    for i in range(cb.count()):
        k, fwd = cb.itemData(i)
        if k == kind and (fwd == this_is_src or k in SYMMETRIC):
            cb.setCurrentIndex(i)
            break
    return cb


class LinkDialog(QDialog):
    """Link the current document to another one."""

    def __init__(self, parent, lib: Library, doc_id: int):
        super().__init__(parent)
        self.lib, self.doc_id = lib, doc_id
        d = lib.document(doc_id)
        self.setWindowTitle("Link document")
        self.resize(640, 520)
        v = QVBoxLayout(self)
        head = QLabel(f"<b>{d.title}</b>")
        v.addWidget(head)
        row = QHBoxLayout()
        row.addWidget(QLabel("is"))
        self.kind = phrase_combo()
        row.addWidget(self.kind, 1)
        v.addLayout(row)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Find the other document by title, system or category…")
        self.filter.addAction(theme.icon("search", theme.T["muted"]), QLineEdit.LeadingPosition)
        self.filter.textChanged.connect(self._refill)
        v.addWidget(self.filter)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _i: self._ok())
        v.addWidget(self.list, 1)
        f = QFormLayout()
        self.note = QLineEdit()
        self.note.setPlaceholderText("optional, e.g. 'chapter 4 covers the registers', 'Rev B board'")
        f.addRow("Note", self.note)
        v.addLayout(f)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Link")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        systems = {s.id: s.name for s in lib.systems()}
        self._docs = [(x.id, f"{x.title}", f"{systems.get(x.system_id, '')} · {x.category}"
                       + (f" · {x.group_path}" if x.group_path else ""))
                      for x in lib.documents() if x.id != doc_id]
        self._refill("")
        self.filter.setFocus()

    def _refill(self, text: str):
        t = text.lower().strip()
        self.list.clear()
        shown = 0
        for did, title, where in self._docs:
            if t and t not in title.lower() and t not in where.lower():
                continue
            it = QListWidgetItem(f"{title}\n{where}")
            it.setData(Qt.UserRole, did)
            self.list.addItem(it)
            shown += 1
            if shown >= 500:
                break
        if self.list.count():
            self.list.setCurrentRow(0)

    def _ok(self):
        it = self.list.currentItem()
        if not it:
            QMessageBox.information(self, "Link document", "Choose the document to link to.")
            return
        other = it.data(Qt.UserRole)
        kind, this_is_src = self.kind.currentData()
        src, dst = (self.doc_id, other) if this_is_src else (other, self.doc_id)
        try:
            add_relation(self.lib, src, dst, kind, self.note.text())
        except RelationError as e:
            QMessageBox.warning(self, "Link document", str(e))
            return
        self.accept()


class SuggestionsDialog(QDialog):
    """Review suggested links: tick the ones to keep, adjust the link type, dismiss the wrong ones."""

    def __init__(self, parent, lib: Library, suggestions: list[Suggestion], title: str = "Suggested links"):
        super().__init__(parent)
        self.lib = lib
        self.sugs = suggestions
        self.changed = False
        self.setWindowTitle(title)
        self.resize(1100, 560)
        v = QVBoxLayout(self)
        intro = QLabel("RetroRAG compared chip names, part numbers, register addresses and content. Tick the links "
                       "worth keeping (check the link type), then <b>Link selected</b>. <b>Dismiss</b> hides a "
                       "suggestion for good. Linked documents are re-indexed so the AI sees the connection.")
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        v.addWidget(intro)
        self.table = QTableWidget(len(suggestions), 5)
        self.table.setHorizontalHeaderLabels(["Document", "is", "Document", "Why", "Score"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Interactive)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.combos: list[QComboBox] = []
        for r, s in enumerate(suggestions):
            left = QTableWidgetItem(s.src_title)
            left.setFlags(left.flags() | Qt.ItemIsUserCheckable)
            left.setCheckState(Qt.Checked if s.score >= 4 else Qt.Unchecked)
            left.setToolTip(s.src_system)
            self.table.setItem(r, 0, left)
            cb = phrase_combo(s.kind, True)
            self.table.setCellWidget(r, 1, cb)
            self.combos.append(cb)
            right = QTableWidgetItem(s.dst_title)
            right.setToolTip(s.dst_system)
            self.table.setItem(r, 2, right)
            why = QTableWidgetItem(s.reason_text())
            why.setToolTip(s.reason_text())
            self.table.setItem(r, 3, why)
            sc = QTableWidgetItem(f"{s.score:.1f}")
            sc.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(r, 4, sc)
        self.table.setColumnWidth(0, 230)
        self.table.setColumnWidth(2, 230)
        v.addWidget(self.table, 1)
        h = QHBoxLayout()
        self.all = QCheckBox("Tick all")
        self.all.toggled.connect(lambda on: [self.table.item(r, 0).setCheckState(Qt.Checked if on else Qt.Unchecked)
                                             for r in range(self.table.rowCount())])
        h.addWidget(self.all)
        h.addStretch()
        b_link = QPushButton("Link selected"); b_link.setObjectName("primary"); b_link.clicked.connect(self._link)
        b_dis = QPushButton("Dismiss selected"); b_dis.clicked.connect(self._dismiss)
        b_close = QPushButton("Close"); b_close.clicked.connect(self.accept)
        for b in (b_link, b_dis, b_close):
            h.addWidget(b)
        v.addLayout(h)
        if not suggestions:
            self.table.hide()
            intro.setText("No new links to suggest. Suggestions come from shared chip names/part numbers and "
                          "register addresses, and from very similar content - add notes and tags to scans "
                          "with little text (schematics, pinouts) so they can be matched.")

    def _checked_rows(self) -> list[int]:
        return [r for r in range(self.table.rowCount())
                if self.table.item(r, 0) and self.table.item(r, 0).checkState() == Qt.Checked]

    def _remove_rows(self, rows: list[int]):
        for r in sorted(rows, reverse=True):
            self.table.removeRow(r)
            del self.combos[r]
            del self.sugs[r]

    def _link(self):
        rows = self._checked_rows()
        if not rows:
            QMessageBox.information(self, "Link", "Tick the suggestions to link first.")
            return
        for r in rows:
            s = self.sugs[r]
            kind, left_is_src = self.combos[r].currentData()
            src, dst = (s.src, s.dst) if left_is_src else (s.dst, s.src)
            try:
                add_relation(self.lib, src, dst, kind, origin="suggested")
            except RelationError:
                pass
        self.changed = True
        self._remove_rows(rows)

    def _dismiss(self):
        rows = self._checked_rows() or sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "Dismiss", "Tick (or select) the suggestions to dismiss.")
            return
        for r in rows:
            dismiss(self.lib, self.sugs[r].src, self.sugs[r].dst)
        self._remove_rows(rows)
