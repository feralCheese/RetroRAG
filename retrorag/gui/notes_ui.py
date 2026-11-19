"""Writing and editing the user's own notes ("My Notes") about a system or peripheral."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QVBoxLayout)

from ..library import Library
from .. import notes
from . import theme


class NoteDialog(QDialog):
    saved = Signal(int)          # doc id
    deleted = Signal(int)

    def __init__(self, parent, lib: Library, system_id: int | None = None, doc_id: int | None = None):
        super().__init__(parent)
        from .dialogs import system_choices
        self.lib = lib
        self.doc = lib.document(doc_id) if doc_id else None
        self.setWindowTitle("Edit note" if self.doc else "New note")
        self.resize(720, 560)
        v = QVBoxLayout(self)
        f = QFormLayout()
        self.title = QLineEdit(self.doc.title if self.doc else "")
        self.title.setPlaceholderText("e.g. Fixing a 1541 that bumps on every load")
        self.system = QComboBox()
        for label, sid in system_choices(lib):
            self.system.addItem(label, sid)
        want = self.doc.system_id if self.doc else system_id
        self.system.setCurrentIndex(max(0, self.system.findData(want)))
        self.tags = QLineEdit(self.doc.tags if self.doc else "")
        self.tags.setPlaceholderText("comma separated, e.g. repair, alignment, 1541-II")
        f.addRow("Title", self.title)
        f.addRow("System", self.system)
        f.addRow("Tags", self.tags)
        v.addLayout(f)
        self.body = QPlainTextEdit(lib.read_text(self.doc) if self.doc else "")
        self.body.setFont(theme.mono_font(10))
        self.body.setPlaceholderText("Your findings: what you tried, what worked, settings, part numbers, "
                                     "addresses, links...\n\nMarkdown is fine (# headings, - lists, ``` code).")
        v.addWidget(self.body, 1)
        hint = QLabel("Notes are searchable like any other document, and the AI is told they're your own "
                      "first-hand findings.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        v.addWidget(hint)
        if self.doc and self.doc.source_url == notes.AI_SOURCE:
            ai = QLabel("This note was written by an AI in a chat.")
            ai.setObjectName("muted")
            v.addWidget(ai)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        if self.doc:
            dl = QPushButton("Delete note…")
            dl.setIcon(theme.icon("delete", theme.T["danger"]))
            dl.clicked.connect(self._delete)
            bb.addButton(dl, QDialogButtonBox.DestructiveRole)
        v.addWidget(bb)
        (self.body if self.doc else self.title).setFocus()

    def _save(self):
        text = self.body.toPlainText()
        if not text.strip():
            QMessageBox.warning(self, "Note", "Write something in the note first.")
            return
        sid = self.system.currentData()
        if sid is None:
            QMessageBox.warning(self, "Note", "Add a system first - notes belong to a system or peripheral.")
            return
        title = self.title.text().strip() or text.strip().splitlines()[0].lstrip("# ")[:80]
        try:
            if self.doc:
                notes.update_note(self.lib, self.doc.id, title, text, tags=self.tags.text().strip(), system_id=sid)
                doc_id = self.doc.id
            else:
                doc_id = notes.add_note(self.lib, self.lib.system(sid), title, text, tags=self.tags.text().strip())
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Note", f"Couldn't save the note: {e}")
            return
        self.saved.emit(doc_id)
        self.accept()

    def _delete(self):
        if QMessageBox.question(self, "Delete note", f"Delete the note '{self.doc.title}'?\n\nIts file is deleted "
                                "too - this can't be undone.") != QMessageBox.Yes:
            return
        self.lib.delete_document(self.doc.id, delete_files=True)
        self.deleted.emit(self.doc.id)
        self.accept()
