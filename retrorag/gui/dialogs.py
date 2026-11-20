"""Dialogs: system editor, add files, remove system, settings, connect-AI."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QPushButton, QRadioButton, QSpinBox, QTableWidget,
                               QTableWidgetItem, QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

from ..config import CATEGORIES, EMBED_PRESETS, SYSTEM_KINDS, AppSettings, LibrarySettings, app_root, is_portable
from ..library import Library, System


# =========================================================================== system
def system_choices(lib: Library, exclude: set | None = None) -> list[tuple[str, int]]:
    """(indented label, id) for every system in family-tree order, e.g. 'ZX Spectrum', '    ZX Spectrum 128K'."""
    exclude = exclude or set()
    systems = lib.systems()
    ids = {s.id for s in systems}
    kids: dict = {}
    for s in systems:
        kids.setdefault(s.parent_id if s.parent_id in ids else None, []).append(s)
    out: list[tuple[str, int]] = []

    def walk(parent, depth):
        for s in kids.get(parent, []):
            if s.id in exclude:
                continue
            out.append(("      " * depth + ("\u2514 " if depth else "") + s.name, s.id))
            walk(s.id, depth + 1)
    walk(None, 0)
    return out


class SystemDialog(QDialog):
    def __init__(self, parent=None, system: System | None = None, lib: Library | None = None,
                 parent_id: int | None = None, host_id: int | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit system" if system else
                            "Add peripheral / expansion" if host_id else "Add retro system")
        self.setMinimumWidth(460)
        f = QFormLayout(self)
        self.name = QLineEdit(system.name if system else "")
        self.name.setPlaceholderText("e.g. Commodore 64")
        self.aliases = QLineEdit(system.aliases if system else "")
        self.aliases.setPlaceholderText("e.g. C64, CBM 64, VIC-64  (used for search filters)")
        self.manufacturer = QLineEdit(system.manufacturer if system else "")
        self.year = QLineEdit(system.year if system else "")
        self.kind = QComboBox(); self.kind.setEditable(True); self.kind.addItems(SYSTEM_KINDS)
        if system and system.kind:
            self.kind.setCurrentText(system.kind)
        elif host_id:
            self.kind.setCurrentText("Peripheral")
        if host_id and not system:
            self.name.setPlaceholderText("e.g. 1541 Disk Drive, 6502 Second Processor, Blizzard 1230 IV")
        self.cpu = QLineEdit(system.cpu if system else "")
        self.cpu.setPlaceholderText("e.g. MOS 6510 @ 1 MHz")
        self.notes = QPlainTextEdit(system.notes if system else "")
        self.notes.setFixedHeight(90)
        f.addRow("Name*", self.name)
        self.parent_sys = QComboBox()
        self.parent_sys.addItem("(none - a system or family in its own right)", None)
        if lib is not None:
            exclude = ({system.id} | {d.id for d in lib.descendants(system.id)}) if system else set()
            for label, sid in system_choices(lib, exclude):
                self.parent_sys.addItem(label, sid)
        want = system.parent_id if system else parent_id
        self.parent_sys.setCurrentIndex(max(0, self.parent_sys.findData(want)))
        self.parent_sys.setToolTip("Make this a variant of another system (e.g. ZX Spectrum 128K of ZX Spectrum). "
                                   "Documents on the family apply to all its variants; each variant keeps its own "
                                   "extra documents.")
        f.addRow("Variant of", self.parent_sys)
        # peripherals and expansions: the machines it works with
        from PySide6.QtWidgets import QListWidget, QListWidgetItem
        self.hosts = QListWidget()
        self.hosts.setToolTip("For a peripheral or expansion (disk drive, second processor, accelerator, RAM or IDE "
                              "board...): tick the machines it works with.\nSearching one of those machines then "
                              "includes this one's documents, and the AI is told it's a peripheral with its own "
                              "hardware.\nTick a family (e.g. BBC Micro) to cover all its variants.")
        have = {h.id for h in lib.hosts(system.id)} if (system and lib is not None) else set()
        if host_id:
            have.add(host_id)
        if lib is not None:
            for label, sid in system_choices(lib, {system.id} if system else set()):
                it = QListWidgetItem(label.replace("└ ", "").rstrip())
                it.setData(Qt.UserRole, sid)
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked if sid in have else Qt.Unchecked)
                self.hosts.addItem(it)
        self.hosts.setFixedHeight(min(130, max(48, self.hosts.sizeHintForRow(0) * min(5, max(2, self.hosts.count()))
                                              + 8)))
        self._hosts_note = QLabel("Leave all unticked for a machine in its own right.")
        self._hosts_note.setObjectName("muted")
        hv = QVBoxLayout(); hv.setContentsMargins(0, 0, 0, 0); hv.setSpacing(2)
        hv.addWidget(self.hosts); hv.addWidget(self._hosts_note)
        hw = QWidget(); hw.setLayout(hv)
        f.addRow("Works with", hw)
        if self.hosts.count() == 0:
            hw.hide()
            f.labelForField(hw).hide()
        f.addRow("Aliases", self.aliases)
        f.addRow("Manufacturer", self.manufacturer)
        f.addRow("Year", self.year)
        f.addRow("Type", self.kind)
        f.addRow("CPU", self.cpu)
        from .media_ui import FormatPicker
        from .. import media
        if system is not None and lib is not None:
            ids, how = lib.system_formats(system.id)
            self.formats = FormatPicker(ids if how == "chosen" else None, ids, how)
        else:
            self.formats = FormatPicker(None, media.suggest_for_system(self.name.text(), self.aliases.text()))
            sugg = lambda *_: self.formats.suggest(                                     # noqa: E731
                media.suggest_for_system(self.name.text(), self.aliases.text(),
                                         *([lib.system(self.parent_sys.currentData()).name]
                                           if lib is not None and self.parent_sys.currentData() else []))
                or media.suggest_for_system(self.manufacturer.text()))
            for w in (self.name, self.aliases, self.manufacturer):
                w.textChanged.connect(sugg)
            self.parent_sys.currentIndexChanged.connect(sugg)
        fbox = QGroupBox("Disk / tape formats")
        fbox.setToolTip("The image formats this system uses - offered first when you create a disk or tape image")
        QVBoxLayout(fbox).addWidget(self.formats)
        f.addRow(fbox)
        f.addRow("Notes", self.notes)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _ok(self):
        if not self.name.text().strip():
            QMessageBox.warning(self, "Name required", "Please enter a system name.")
            return
        self.accept()

    def values(self) -> dict:
        fm = self.formats.value()
        hosts = [self.hosts.item(i).data(Qt.UserRole) for i in range(self.hosts.count())
                 if self.hosts.item(i).checkState() == Qt.Checked]
        return {**({"media_formats": fm} if fm is not None else {}), "hosts": hosts,
                "name": self.name.text().strip(), "parent_id": self.parent_sys.currentData(),
                "aliases": self.aliases.text().strip(),
                "manufacturer": self.manufacturer.text().strip(), "year": self.year.text().strip(),
                "kind": self.kind.currentText().strip(), "cpu": self.cpu.text().strip(),
                "notes": self.notes.toPlainText().strip()}


class RemoveSystemDialog(QDialog):
    def __init__(self, parent, system: System, n_docs: int, folder: Path, variants: list | None = None,
                 parent_name: str | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Remove {system.name}")
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"<b>Remove '{system.name}'</b> and its {n_docs} document(s) from the library?"))
        if variants:
            vn = QLabel(f"Its variants ({', '.join(variants)}) are kept, with their documents, and become "
                        + (f"variants of {parent_name}." if parent_name else "systems in their own right."))
            vn.setWordWrap(True); vn.setObjectName("muted")
            v.addWidget(vn)
        self.keep = QRadioButton("Keep the files on disk")
        self.keep.setChecked(True)
        keep_note = QLabel(f"Originals, OCR text and your corrections stay in:<br><code>{folder}</code><br>"
                           "A <code>system.json</code> manifest is saved with them. Adding a system with the same "
                           "name later (or using <i>Import system folder</i>) brings everything back.")
        keep_note.setWordWrap(True); keep_note.setStyleSheet("margin-left:22px;color:gray")
        self.delete = QRadioButton("Delete the files too")
        del_note = QLabel("Permanently deletes the system's folder, including OCR text and corrections. "
                          "Files that were linked in place (outside the library) are never deleted.")
        del_note.setWordWrap(True); del_note.setStyleSheet("margin-left:22px;color:gray")
        for w in (self.keep, keep_note, self.delete, del_note):
            v.addWidget(w)
        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.ok = bb.addButton("Remove", QDialogButtonBox.DestructiveRole)
        self.ok.clicked.connect(self._confirm)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.setMinimumWidth(520)

    def _confirm(self):
        if self.delete.isChecked() and QMessageBox.warning(
                self, "Delete files", "This permanently deletes the files. Continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.accept()

    @property
    def delete_files(self) -> bool:
        return self.delete.isChecked()


class MoveDialog(QDialog):
    """Pick the system (and optionally a new category) to move documents to."""

    def __init__(self, parent, lib: Library, n_docs: int, system_id: int | None = None):
        super().__init__(parent)
        self.setWindowTitle("Move documents")
        self.setMinimumWidth(480)
        f = QFormLayout(self)
        f.addRow(QLabel(f"Move <b>{n_docs}</b> document(s). Files move on disk too, with their OCR text, "
                        "corrections and links."))
        self.system = QComboBox()
        for label, sid in system_choices(lib):
            self.system.addItem(label, sid)
        if system_id is not None:
            self.system.setCurrentIndex(max(0, self.system.findData(system_id)))
        f.addRow("To system", self.system)
        self.category = QComboBox()
        self.category.addItem("(keep each document's category)", None)
        for c in CATEGORIES:
            self.category.addItem(c, c)
        f.addRow("Category", self.category)
        self.top = QCheckBox("Take them out of sub-folders (top level of the category)")
        f.addRow("", self.top)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Move")
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        f.addRow(bb)

    def values(self) -> tuple[int, str | None, str | None]:
        return self.system.currentData(), self.category.currentData(), ("" if self.top.isChecked() else None)


# =========================================================================== add files
BY_TYPE_TIP = ("Each file goes under its own type's category: documents to User Guides & Manuals (or Schematics, "
               "Service & Repair... when the name says so), photos to Photos, disk images to Disk & Tape Images, "
               "source code to Source...")


def category_box(value: str, by_type: bool = False) -> QComboBox:
    """Editable category chooser (type a new name to make a new category). by_type adds 'By file type'."""
    from ..categorize import BY_TYPE
    cb = QComboBox(); cb.setEditable(True)
    if by_type:
        cb.addItem(BY_TYPE)
        cb.setItemData(0, BY_TYPE_TIP, Qt.ToolTipRole)
    cb.addItems(CATEGORIES)
    cb.setCurrentText(value)
    return cb


class AddDialog(QDialog):
    """Choose the system, each file's category (guessed from its type) and copy-vs-link for files being added;
    or the category and file kinds for a folder."""

    def __init__(self, parent, systems: list[System], system: System | None, paths: list[Path], folder: bool):
        super().__init__(parent)
        from ..categorize import BY_TYPE, SOURCE, inspect_file
        self.folder, self.paths = folder, list(paths)
        self.setWindowTitle("Add folder" if folder else "Add files")
        self.setMinimumWidth(520 if folder else 760)
        f = QFormLayout(self)
        self.system = QComboBox()
        for s in systems:
            self.system.addItem(s.name, s.id)
        if system:
            self.system.setCurrentIndex(max(0, self.system.findData(system.id)))
        self.rows: list[tuple[Path, QComboBox]] = []
        self.archive_rows: list[QComboBox] = []
        self.kinds = {}
        if folder:
            lbl = QLabel(str(paths[0])); lbl.setStyleSheet("color:gray"); lbl.setWordWrap(True)
            f.addRow("Adding", lbl)
            f.addRow("System", self.system)
            self.category = category_box(SOURCE, by_type=True)
            self.category.setToolTip("Every file in the folder goes under this category - or choose 'By file type' "
                                     "for a folder of mixed things (manuals, photos, disk images...).")
            f.addRow("Category", self.category)
        else:
            f.addRow("System", self.system)
            QApplication.setOverrideCursor(Qt.WaitCursor)          # archives and pictures are looked inside
            try:
                guesses = [inspect_file(p) for p in paths]
            finally:
                QApplication.restoreOverrideCursor()
            self.table = QTableWidget(len(paths), 3)
            self.table.setHorizontalHeaderLabels(["File", "Type", "Category"])
            self.table.verticalHeader().setVisible(False)
            self.table.setSelectionMode(QAbstractItemView.NoSelection)
            self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            from .. import archives
            self.archive_rows: list[QComboBox] = []
            for r, (p, (cat, what)) in enumerate(zip(paths, guesses)):
                it = QTableWidgetItem(p.name); it.setToolTip(str(p))
                self.table.setItem(r, 0, it)
                self.table.setItem(r, 1, QTableWidgetItem(what))
                is_archive = bool(archives.kind_by_name(p.name)) and what.startswith(("ZIP", "TAR"))
                cb = category_box(cat, by_type=is_archive)
                cb.setToolTip("Guessed from the file's type and name - change it if it's wrong."
                              + ("\n'By file type' files each file in the archive under its own type's category."
                                 if is_archive else ""))
                self.table.setCellWidget(r, 2, cb)
                self.rows.append((p, cb))
                if is_archive:
                    self.archive_rows.append(cb)
                    cb.currentTextChanged.connect(self._cat_changed)
            hh = self.table.horizontalHeader()
            hh.setSectionResizeMode(0, QHeaderView.Stretch)
            hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
            hh.setSectionResizeMode(2, QHeaderView.Interactive)
            self.table.setColumnWidth(2, 230)
            self.table.setMinimumHeight(min(420, 60 + 30 * len(paths)))
            f.addRow(self.table)
            self.all = QComboBox(); self.all.addItem("Set every file to…"); self.all.addItems(CATEGORIES)
            self.all.activated.connect(lambda i: i and self.set_all(self.all.itemText(i)))
            note = QLabel("Each file's category is guessed from its type and name.")
            note.setStyleSheet("color:gray")
            h = QHBoxLayout(); h.addWidget(note, 1); h.addWidget(self.all)
            f.addRow(h)
        self.copy = QCheckBox("Copy into the library (recommended - keeps the library self-contained and portable)")
        self.copy.setChecked(True)
        f.addRow("", self.copy)
        if folder or self.archive_rows:
            box = QGroupBox("Include file types" if folder else "Take these file types out of the archives")
            h = QHBoxLayout(box)
            for k, label, on in (("text", "Text/HTML", True), ("code", "Source code", True), ("pdf", "PDF", True),
                                 ("image", "Images", True), ("binary", "Binaries/ROMs/disk images", True)):
                cb = QCheckBox(label); cb.setChecked(on); h.addWidget(cb); self.kinds[k] = cb
            if folder:
                self.category.currentTextChanged.connect(self._cat_changed)
                self._cat_changed(self.category.currentText())
            else:
                self._cat_changed(self.archive_rows[0].currentText())
            f.addRow(box)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        f.addRow(bb)

    def set_all(self, category: str):
        """Put every file under one category (e.g. files dropped onto a category in the library tree)."""
        if self.folder:
            self.category.setCurrentText(category)
        for _, cb in self.rows:
            cb.setCurrentText(category)

    def _cat_changed(self, cat: str):
        """Source code: text and code only by default (no ROMs, icons or screenshots from the tree)."""
        if not self.kinds:
            return
        src = cat == "Source"
        self.kinds["image"].setChecked(not src)
        self.kinds["binary"].setChecked(not src)

    def values(self) -> dict:
        kinds = {k for k, cb in self.kinds.items() if cb.isChecked()} if self.kinds else None
        out = {"system_id": self.system.currentData(), "copy": self.copy.isChecked(), "kinds": kinds}
        if self.folder:
            out["category"] = self.category.currentText().strip() or "Other"
        else:
            out["categories"] = {str(p): cb.currentText().strip() or "Other" for p, cb in self.rows}
        return out


# =========================================================================== settings
class SettingsDialog(QDialog):
    def __init__(self, parent, app: AppSettings, lib: Library):
        super().__init__(parent)
        self.app, self.lib = app, lib
        st = lib.settings
        self.setWindowTitle("Settings")
        self.setMinimumWidth(640)
        v = QVBoxLayout(self)
        tabs = QTabWidget(); v.addWidget(tabs)

        # --- library
        w = QWidget(); f = QFormLayout(w); tabs.addTab(w, "Library")
        self.lib_path = QLineEdit(str(lib.root))
        b = QPushButton("Browse…"); b.clicked.connect(self._browse_lib)
        h = QHBoxLayout(); h.addWidget(self.lib_path); h.addWidget(b)
        f.addRow("Library folder", h)
        note = QLabel("A library is a self-contained folder (database + files). Copy it to another PC, or point "
                      "this at a new folder to create a new library. Changing it switches library on OK.")
        note.setWordWrap(True); note.setStyleSheet("color:gray")
        f.addRow(note)
        self.portable = QCheckBox("Portable mode (settings + default library stored next to the app)")
        self.portable.setChecked(is_portable())
        self.portable.setToolTip(f"Creates/removes {app_root() / 'portable.flag'}")
        f.addRow(self.portable)
        self.extract_zip = QCheckBox("Unpack archives and compressed files (.zip, .tar.gz, .gz…) when adding")
        self.extract_zip.setChecked(st.extract_archives)
        f.addRow(self.extract_zip)

        # --- OCR
        w = QWidget(); f = QFormLayout(w); tabs.addTab(w, "OCR")
        self.tess = QLineEdit(st.tesseract_path or app.tesseract_path)
        self.tess.setPlaceholderText("auto-detect (PATH, C:\\Program Files\\Tesseract-OCR)")
        b = QPushButton("Browse…"); b.clicked.connect(self._browse_tess)
        bt = QPushButton("Test"); bt.clicked.connect(self._test_tess)
        h = QHBoxLayout(); h.addWidget(self.tess); h.addWidget(b); h.addWidget(bt)
        f.addRow("tesseract.exe", h)
        self.lang = QLineEdit(st.ocr_language); self.lang.setToolTip("e.g. eng, deu, eng+deu, jpn")
        f.addRow("Language(s)", self.lang)
        self.thresh = QSpinBox(); self.thresh.setRange(0, 100); self.thresh.setValue(st.low_conf_threshold)
        self.thresh.setSuffix(" %")
        self.thresh.setToolTip("Words recognised with confidence below this are marked {?word|NN?} for review")
        f.addRow("Flag words below confidence", self.thresh)
        self.dpi = QSpinBox(); self.dpi.setRange(100, 600); self.dpi.setValue(st.ocr_dpi)
        f.addRow("PDF render DPI", self.dpi)
        self.psm = QComboBox()
        from .editor import PSM_CHOICES
        for val, label in PSM_CHOICES:
            self.psm.addItem(label, val)
        self.psm.setCurrentIndex(max(0, self.psm.findData(st.ocr_psm)))
        f.addRow("Default layout mode", self.psm)
        from ..ocr import CLEANUP_CHOICES
        self.cleanup = QComboBox()
        for val, label in CLEANUP_CHOICES:
            self.cleanup.addItem(label, val)
        self.cleanup.setCurrentIndex(max(0, self.cleanup.findData(st.ocr_cleanup)))
        f.addRow("Scan cleanup", self.cleanup)
        self.drop_gfx = QCheckBox("Ignore drawing shapes that OCR mistakes for words (schematics, pinouts)")
        self.drop_gfx.setChecked(st.ocr_drop_graphics)
        f.addRow(self.drop_gfx)
        self.binding = QCheckBox("Ignore ring binding, comb holes and spine shadows at the page edges")
        self.binding.setToolTip("Scanned manuals with ring, wire or comb binding have a column of holes down one "
                                "edge, which OCR reads as junk like 'TT' or '{='. When this is on, those marks (and "
                                "dark spine or scanner-lid shadows at the edge) are blanked before OCR. Margin text "
                                "that Tesseract can read is never removed. The proofreading editor shades what "
                                "was ignored.")
        self.binding.setChecked(st.ocr_ignore_binding)
        f.addRow(self.binding)
        self.upscale =QCheckBox("Upscale small images before OCR"); self.upscale.setChecked(st.ocr_upscale_small_images)
        f.addRow(self.upscale)
        self.min_chars = QSpinBox(); self.min_chars.setRange(0, 1000); self.min_chars.setValue(st.pdf_min_text_chars)
        f.addRow("PDF page counts as scanned if text <", self.min_chars)
        self.tess_status = QLabel(); self.tess_status.setWordWrap(True)
        f.addRow(self.tess_status)

        # --- embeddings
        w = QWidget(); f = QFormLayout(w); tabs.addTab(w, "Embeddings")
        self.preset = QComboBox(); self.preset.addItem("(choose a preset…)")
        self.preset.addItems(list(EMBED_PRESETS))
        self.preset.activated.connect(self._apply_preset)
        f.addRow("Preset", self.preset)
        self.provider = QComboBox(); self.provider.addItems(["builtin", "ollama", "openai", "fastembed", "none"])
        self.provider.setCurrentText(st.embed_provider)
        self.provider.currentTextChanged.connect(self._provider_changed)
        f.addRow("Provider", self.provider)
        self.e_url = QLineEdit(st.embed_url)
        f.addRow("URL", self.e_url)
        self.e_model = QLineEdit(st.embed_model)
        f.addRow("Model", self.e_model)
        self.e_key = QLineEdit(st.embed_api_key); self.e_key.setEchoMode(QLineEdit.Password)
        self.e_key.setPlaceholderText("only for endpoints that need one (Unsloth Studio: Settings > API)")
        f.addRow("API key", self.e_key)
        self.e_dpre = QLineEdit(st.embed_doc_prefix); f.addRow("Document prefix", self.e_dpre)
        self.e_qpre = QLineEdit(st.embed_query_prefix); f.addRow("Query prefix", self.e_qpre)
        self.chunk = QSpinBox(); self.chunk.setRange(300, 8000); self.chunk.setValue(st.chunk_chars); self.chunk.setSuffix(" chars")
        f.addRow("Chunk size", self.chunk)
        self.overlap = QSpinBox(); self.overlap.setRange(0, 2000); self.overlap.setValue(st.chunk_overlap); self.overlap.setSuffix(" chars")
        f.addRow("Chunk overlap", self.overlap)
        self.e_batch = QSpinBox(); self.e_batch.setRange(1, 512); self.e_batch.setValue(st.embed_batch)
        self.e_par = QSpinBox(); self.e_par.setRange(1, 8); self.e_par.setValue(st.embed_parallel)
        h = QHBoxLayout(); h.addWidget(QLabel("batch")); h.addWidget(self.e_batch); h.addWidget(QLabel("requests at once"))
        h.addWidget(self.e_par); h.addStretch()
        f.addRow("Throughput", h)
        self.e_code = QCheckBox("Embed source code too (slow for big emulator trees; keyword search always covers code)")
        self.e_code.setChecked(st.embed_code)
        f.addRow("", self.e_code)
        bt = QPushButton("Test embeddings"); bt.clicked.connect(self._test_embed)
        self.e_status = QLabel(); self.e_status.setWordWrap(True)
        f.addRow(bt, self.e_status)
        n = QLabel("Changing the model means every chunk is re-embedded on the next 'Build index'. "
                   "The machine that answers queries must be able to reach the same model.")
        n.setWordWrap(True); n.setStyleSheet("color:gray"); f.addRow(n)

        # --- servers
        # --- appearance
        from .theme import PHOSPHOR_NAMES
        w = QWidget(); f = QFormLayout(w); tabs.addTab(w, "Appearance")
        self.theme_combo = QComboBox()
        for key, label in (("system", "Follow Windows"), ("light", "Light"), ("dark", "Dark"),
                           ("retro", "Retro - phosphor CRT")):
            self.theme_combo.addItem(label, key)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(app.theme)))
        f.addRow("Theme", self.theme_combo)
        self.phosphor = QComboBox()
        for key, label in PHOSPHOR_NAMES.items():
            self.phosphor.addItem(label, key)
        self.phosphor.setCurrentIndex(max(0, self.phosphor.findData(app.retro_phosphor)))
        f.addRow("Retro phosphor", self.phosphor)
        self.scan = QCheckBox("Simulated scan lines and screen vignette"); self.scan.setChecked(app.retro_scanlines)
        f.addRow("", self.scan)
        self.rfont = QLineEdit(app.retro_font_file)
        self.rfont.setPlaceholderText("built-in RetroRAG Phosphor pixel font")
        bf = QPushButton("Browse…"); bf.clicked.connect(self._browse_font)
        bc = QPushButton("Built-in"); bc.clicked.connect(self.rfont.clear)
        h = QHBoxLayout(); h.addWidget(self.rfont); h.addWidget(bf); h.addWidget(bc)
        f.addRow("Retro font", h)
        n = QLabel("Retro mode draws everything in an 8-bit pixel font on a monochrome CRT. RetroRAG ships its own "
                   "original pixel font; to use a different one - e.g. a PET / VIC-20 style font you have "
                   "downloaded (Kreative Software's free 'Pet Me' family is a popular choice) - pick its .ttf file "
                   "here. Pixel fonts look sharpest when designed on an 8-pixel grid.")
        n.setWordWrap(True); n.setObjectName("muted"); f.addRow(n)

        w = QWidget(); f = QFormLayout(w); tabs.addTab(w, "Servers")
        self.host = QComboBox(); self.host.setEditable(True); self.host.addItems(["127.0.0.1", "0.0.0.0"])
        self.host.setCurrentText(st.api_host)
        self.host.setToolTip("127.0.0.1 = this PC only. 0.0.0.0 = other PCs on your network can connect.")
        f.addRow("Listen on", self.host)
        self.port = QSpinBox(); self.port.setRange(1024, 65535); self.port.setValue(st.api_port)
        f.addRow("REST API port", self.port)
        self.mport = QSpinBox(); self.mport.setRange(1024, 65535); self.mport.setValue(st.mcp_port)
        f.addRow("MCP (HTTP) port", self.mport)
        self.token = QLineEdit(st.api_token); self.token.setPlaceholderText("none (fine for 127.0.0.1)")
        bg = QPushButton("Generate"); bg.clicked.connect(lambda: self.token.setText(secrets.token_urlsafe(24)))
        h = QHBoxLayout(); h.addWidget(self.token); h.addWidget(bg)
        f.addRow("Access token", h)
        n = QLabel("If you listen on 0.0.0.0, set an access token. Clients send it as "
                   "'Authorization: Bearer <token>'. Restart the servers after changing these.")
        n.setWordWrap(True); n.setStyleSheet("color:gray"); f.addRow(n)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _browse_lib(self):
        d = QFileDialog.getExistingDirectory(self, "Library folder", self.lib_path.text())
        if d:
            self.lib_path.setText(d)

    def _browse_font(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Retro font", "", "Fonts (*.ttf *.otf)")
        if fn:
            self.rfont.setText(fn)

    def _browse_tess(self):
        f, _ = QFileDialog.getOpenFileName(self, "tesseract.exe", "C:/Program Files", "tesseract.exe (tesseract.exe)")
        if f:
            self.tess.setText(f)

    def _test_tess(self):
        from ..ocr import tesseract_info
        info = tesseract_info(self.tess.text().strip())
        if info["ok"]:
            self.tess_status.setText(f"✓ Tesseract {info['version']} at {info['path']}\n"
                                     f"Languages: {', '.join(info['languages'])}")
        else:
            self.tess_status.setText("✗ " + info["message"])

    def _provider_changed(self, prov: str):
        """Switching provider: fill in sensible defaults unless the current values already suit it."""
        from ..embeddings import BUILTIN_MODELS, resolve_builtin
        model, url = self.e_model.text().strip(), self.e_url.text().strip()
        defaults = {"builtin": "Built-in - nomic-embed-text", "ollama": "Ollama - nomic-embed-text",
                    "openai": "Unsloth Studio"}
        if prov == "builtin":
            name = resolve_builtin(model)
            if name and name == model and not url:
                return
            if name:     # e.g. 'nomic-embed-text' -> the same model, built in
                spec = BUILTIN_MODELS[name]
                self.e_model.setText(name); self.e_url.setText("")
                self.e_dpre.setText(spec["doc_prefix"]); self.e_qpre.setText(spec["query_prefix"])
                return
        elif prov == "ollama" and url and resolve_builtin(model) != model:
            return
        elif prov == "openai" and url and "11434" not in url:
            return
        elif prov in ("none", "fastembed"):
            return
        key = next((k for k in EMBED_PRESETS if k.startswith(defaults.get(prov, "~"))), None)
        if key:
            p, u, m, dpre, qpre = EMBED_PRESETS[key]
            self.e_url.setText(u); self.e_model.setText(m); self.e_dpre.setText(dpre); self.e_qpre.setText(qpre)

    def _apply_preset(self, i: int):
        if i <= 0:
            return
        prov, url, model, dpre, qpre = EMBED_PRESETS[self.preset.currentText()]
        self.provider.blockSignals(True)
        self.provider.setCurrentText(prov)
        self.provider.blockSignals(False)
        self.e_url.setText(url); self.e_model.setText(model)
        self.e_dpre.setText(dpre); self.e_qpre.setText(qpre)

    def _collect(self, st: LibrarySettings) -> LibrarySettings:
        st.extract_archives = self.extract_zip.isChecked()
        st.tesseract_path = self.tess.text().strip()
        st.ocr_language = self.lang.text().strip() or "eng"
        st.low_conf_threshold = self.thresh.value()
        st.ocr_dpi = self.dpi.value()
        st.ocr_psm = int(self.psm.currentData())
        st.ocr_upscale_small_images = self.upscale.isChecked()
        st.ocr_drop_graphics = self.drop_gfx.isChecked()
        st.ocr_ignore_binding = self.binding.isChecked()
        st.ocr_cleanup = self.cleanup.currentData()
        st.pdf_min_text_chars = self.min_chars.value()
        st.embed_provider = self.provider.currentText()
        st.embed_url = self.e_url.text().strip()
        st.embed_model = self.e_model.text().strip()
        st.embed_api_key = self.e_key.text().strip()
        st.embed_doc_prefix = self.e_dpre.text()
        st.embed_query_prefix = self.e_qpre.text()
        st.normalise_builtin()
        st.chunk_chars = self.chunk.value()
        st.chunk_overlap = self.overlap.value()
        st.embed_batch = self.e_batch.value()
        st.embed_parallel = self.e_par.value()
        st.embed_code = self.e_code.isChecked()
        st.api_host = self.host.currentText().strip() or "127.0.0.1"
        st.api_port = self.port.value()
        st.mcp_port = self.mport.value()
        st.api_token = self.token.text().strip()
        return st

    def _test_embed(self):
        from ..embeddings import BUILTIN_MODELS, find_model, test_embedder
        st = self._collect(LibrarySettings.load(self.lib.root))
        if st.embed_provider == "builtin" and st.embed_model in BUILTIN_MODELS and not find_model(st.embed_model):
            self.e_status.setText(f"✓ Built-in model {st.embed_model} (~{BUILTIN_MODELS[st.embed_model]['size_mb']} MB) "
                                  "downloads automatically the first time you build the index - no server needed.")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.e_status.setText("✓ " + test_embedder(self._collect(LibrarySettings.load(self.lib.root))))
        except Exception as e:
            self.e_status.setText(f"✗ {e}")
        finally:
            QApplication.restoreOverrideCursor()

    def apply(self) -> tuple[bool, str]:
        """Save settings. Returns (chunking_changed, new_library_path)."""
        old = LibrarySettings.load(self.lib.root)
        st = self._collect(LibrarySettings.load(self.lib.root))
        st.save(self.lib.root)
        self.lib.reload_settings()
        self.app.tesseract_path = st.tesseract_path
        self.app.theme = self.theme_combo.currentData()
        self.app.retro_phosphor = self.phosphor.currentData()
        self.app.retro_scanlines = self.scan.isChecked()
        self.app.retro_font_file = self.rfont.text().strip()
        flag = app_root() / "portable.flag"
        try:
            if self.portable.isChecked() and not flag.exists():
                flag.write_text("RetroRAG portable mode: settings live in ./settings\n")
            elif not self.portable.isChecked() and flag.exists():
                flag.unlink()
        except OSError as e:
            QMessageBox.warning(self, "Portable mode", f"Could not change portable mode: {e}")
        changed = (old.chunk_chars, old.chunk_overlap) != (st.chunk_chars, st.chunk_overlap)
        return changed, self.lib_path.text().strip()


# =========================================================================== connect
def claude_desktop_config_paths() -> list[Path]:
    from ..platform_util import claude_desktop_config_paths as paths
    return paths()


def mcp_command(lib: Library) -> tuple[str, list[str], dict]:
    """Command line for launching the stdio MCP server on *this* machine."""
    if getattr(sys, "frozen", False):
        from ..platform_util import exe_name
        cli = Path(sys.executable).with_name(exe_name("retrorag-cli"))
        return str(cli if cli.exists() else sys.executable), ["mcp", "--library", str(lib.root)], {}
    py = Path(sys.executable)
    if py.name.lower() == "pythonw.exe":
        py = py.with_name("python.exe")
    return str(py), ["-m", "retrorag", "mcp", "--library", str(lib.root)], {"PYTHONPATH": str(app_root())}


class ConnectDialog(QDialog):
    def __init__(self, parent, lib: Library, servers_running: bool):
        super().__init__(parent)
        self.lib = lib
        self.setWindowTitle("Connect AI tools")
        self.resize(800, 700)
        st = lib.settings
        v = QVBoxLayout(self)
        cmd, args, env = mcp_command(lib)
        entry = {"command": cmd, "args": args}
        if env:
            entry["env"] = env
        self.entry = entry
        host = "127.0.0.1" if st.api_host in ("0.0.0.0", "") else st.api_host
        rest = f"http://{host}:{st.api_port}"
        mcp_url = f"http://{host}:{st.mcp_port}/mcp"
        auth = f"\nHeader:  Authorization: Bearer {st.api_token}" if st.api_token else ""
        from ..platform_util import lan_address
        lan = lan_address() if st.api_host == "0.0.0.0" else None
        lan_url = f"http://{lan}:{st.mcp_port}/mcp" if lan else None
        self.header_value = f"Bearer {st.api_token}" if st.api_token else ""
        row = "<tr><td style='padding-right:14px;color:gray'>{}</td><td>{}</td></tr>"
        uns_rows = [row.format("Display name", "<code>RetroRAG</code>"),
                    row.format("URL", f"<code>{mcp_url}</code>" + (" &nbsp;(Studio on this PC)" if lan_url else ""))]
        if lan_url:
            uns_rows.append(row.format("", f"<code>{lan_url}</code> &nbsp;(Studio on another PC)"))
        if st.api_token:
            uns_rows.append(row.format("Custom headers", "click <b>Add header</b>, then:"))
            uns_rows.append(row.format("&nbsp;&nbsp;header name", "<code>Authorization</code>"))
            uns_rows.append(row.format("&nbsp;&nbsp;header value", f"<code>Bearer {st.api_token}</code> &nbsp;"
                                       "(the word Bearer, a space, then the token)"))
        uns_table = "<table cellspacing=2>" + "".join(uns_rows) + "</table>"
        # "--" quoted: PowerShell drops a bare -- on its way to an npm-installed command's .ps1 launcher
        claude_code = "claude mcp add retrorag --scope user " + " ".join(
            [f'-e {k}="{v}"' for k, v in env.items()] + ['"--"', f'"{cmd}"'] + [f'"{a}"' if " " in a else a for a in args])
        from ..ai_clients import opencode_command, opencode_config_file, opencode_http_command
        self.mcp = (cmd, args, env)
        self.oc_cmd = opencode_command(cmd, args, env)
        from html import escape
        oc_file = escape(str(opencode_config_file()).replace(str(Path.home()), "~"))
        oc_remote = (f"<p>opencode on another PC: <code>{escape(opencode_http_command(lan_url, st.api_token))}</code></p>"
                     if lan_url else "")
        state = ("<span style='color:green'>running</span>" if servers_running
                 else "<span style='color:#c60'>not running - click <b>Start servers</b> in the toolbar</span>")
        html = f"""
<h3>Claude Desktop</h3>
<p>Adds RetroRAG as an MCP server (stdio). Click <b>Add to Claude Desktop</b> below, then restart Claude Desktop.
Or paste this into <code>claude_desktop_config.json</code> under <code>"mcpServers"</code>:</p>
<pre>{json.dumps({"retrorag": entry}, indent=2)}</pre>
<h3>Claude Code</h3><pre>{claude_code}</pre>
<h3>opencode</h3>
<p>Click <b>Add to opencode</b> below. It adds RetroRAG to <code>{oc_file}</code>, for all your projects, and keeps a
<code>.bak</code>. Or run this in a terminal:</p>
<pre>{escape(self.oc_cmd)}</pre>
<p>A running opencode picks it up by itself: <code>/mcps</code> shows it connected.</p>{oc_remote}
<h3>Unsloth Studio</h3>
<p>HTTP servers are {state}. In Studio chat: <b>MCP → Add custom MCP</b>, then fill in:</p>
{uns_table}
<p>Test connection → Add server, and make sure <b>Use MCP Servers</b> is on. (HTTP is recommended: Studio disables local
stdio commands when LAN/remote access is enabled.)""" + ("" if lan_url else """ If Studio runs on another PC, set
<i>Listen on</i> to 0.0.0.0 and an access token in Settings → Servers, restart the servers and open this dialog again
for the address to use.""") + f"""</p>
<p>To use Unsloth for <b>embeddings</b> too: Settings → Embeddings → preset <i>Unsloth Studio</i>, paste a Studio API key.</p>
<h3>Other MCP clients over HTTP</h3>
<p>LM Studio, Open WebUI, Cursor, Continue, Jan, AnythingLLM…: streamable-HTTP MCP URL <code>{mcp_url}</code>{f' (from another PC: <code>{lan_url}</code>)' if lan_url else ''}{auth}</p>
<h3>REST / OpenAPI</h3>
<p>Search: <code>{rest}/search?q=SID+filter&amp;system=C64</code> · Docs: <code>{rest}/docs</code> ·
Spec: <code>{rest}/openapi.json</code> (Open WebUI → Settings → Tools → add OpenAPI server <code>{rest}</code>)</p>
<h3>Ollama / any LLM without tool support</h3>
<p>Use <b>Export</b> → Markdown corpus to load the documents into the chat app's own knowledge base, or call the REST
search endpoint from your own scripts and paste the passages into the prompt.</p>
<p style='color:gray'>The paths above are for <i>this</i> PC. On another PC, run RetroRAG there and open this dialog again
to get the right paths.</p>"""
        tb = QTextBrowser(); tb.setHtml(html); tb.setOpenExternalLinks(True)
        v.addWidget(tb)
        h = QHBoxLayout()
        b1 = QPushButton("Add to Claude Desktop"); b1.clicked.connect(self.install_claude_desktop)
        b2 = QPushButton("Copy Claude Desktop JSON")
        b2.clicked.connect(lambda: QApplication.clipboard().setText(json.dumps({"retrorag": entry}, indent=2)))
        b3 = QPushButton("Copy Claude Code command"); b3.clicked.connect(lambda: QApplication.clipboard().setText(claude_code))
        b4 = QPushButton("Copy MCP URL")
        b4.clicked.connect(lambda: QApplication.clipboard().setText(lan_url or mcp_url))
        if lan_url:
            b4.setToolTip(f"Copies the network address ({lan_url}), which works from this PC and others")
        for b in (b1, b2, b3):            # Claude, then opencode, then HTTP clients (Unsloth etc.)
            h.addWidget(b)
        h.addStretch()
        v.addLayout(h)
        ho = QHBoxLayout()
        b6 = QPushButton("Add to opencode"); b6.clicked.connect(self.install_opencode)
        b7 = QPushButton("Copy opencode command")
        b7.clicked.connect(lambda: QApplication.clipboard().setText(self.oc_cmd))
        ho.addWidget(b6); ho.addWidget(b7); ho.addStretch()
        v.addLayout(ho)
        h2 = QHBoxLayout()
        h2.addWidget(b4)
        if self.header_value:
            b5 = QPushButton("Copy header value")
            b5.setToolTip("Copies 'Bearer <token>' - paste it as the value of a header named Authorization")
            b5.clicked.connect(lambda: QApplication.clipboard().setText(self.header_value))
            h2.addWidget(b5)
        h2.addStretch()
        close = QPushButton("Close"); close.clicked.connect(self.accept); h2.addWidget(close)
        v.addLayout(h2)
        # the focused button is drawn bold (default-button style) but sized for normal text: leave room for bold
        from PySide6.QtGui import QFont, QFontMetrics
        for b in self.findChildren(QPushButton):
            f = QFont(b.font())
            f.setWeight(QFont.DemiBold)
            b.setMinimumWidth(QFontMetrics(f).horizontalAdvance(b.text()) + 36)

    def install_claude_desktop(self):
        paths = [p for p in claude_desktop_config_paths() if p.parent.exists()] or claude_desktop_config_paths()[:1]
        if not paths:
            QMessageBox.warning(self, "Claude Desktop", "Could not locate Claude Desktop's config folder.")
            return
        done = []
        for p in paths:
            try:
                cfg = json.loads(p.read_text("utf-8")) if p.exists() else {}
                if p.exists():
                    shutil.copy2(p, p.with_suffix(".json.bak"))
                cfg.setdefault("mcpServers", {})["retrorag"] = self.entry
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(cfg, indent=2), "utf-8")
                done.append(str(p))
            except Exception as e:
                QMessageBox.warning(self, "Claude Desktop", f"Could not update {p}:\n{e}")
        if done:
            QMessageBox.information(self, "Claude Desktop", "Added 'retrorag' to:\n" + "\n".join(done) +
                                    "\n\n(backup saved as .json.bak)\nRestart Claude Desktop to load it.")

    def install_opencode(self):
        from ..ai_clients import SetupError, install_opencode
        QApplication.setOverrideCursor(Qt.WaitCursor)      # asks the opencode command for its version
        try:
            path, bak = install_opencode(*self.mcp)
        except SetupError as e:
            QApplication.restoreOverrideCursor()
            QApplication.clipboard().setText(self.oc_cmd)
            QMessageBox.warning(self, "opencode", f"{e}\n\nInstead, paste this command into a terminal (it's on the "
                                f"clipboard):\n\n{self.oc_cmd}")
            return
        QApplication.restoreOverrideCursor()
        QMessageBox.information(self, "opencode", f"Added 'retrorag' to:\n{path}" +
                                (f"\n\n(backup saved as {bak.name})" if bak else "") +
                                "\n\nA running opencode loads it by itself; /mcps in opencode shows it connected.")
