"""Disk and tape images in the app: format picker for systems, an image's contents on the Document tab, a file
viewer, and the Create disk / tape image dialog."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
                               QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import media, media_ops
from ..library import Document, Library
from ..media import MediaError, NewFile
from . import theme


# =========================================================================== format picker (system dialog)
class FormatPicker(QWidget):
    """Tick the disk / tape formats a system uses. Untouched = 'suggested from the name'."""

    def __init__(self, chosen: list[str] | None, suggested: list[str], how: str = "suggested"):
        super().__init__()
        self.touched = chosen is not None and how == "chosen"
        g = QGridLayout(self)
        g.setContentsMargins(0, 0, 0, 0)
        g.setHorizontalSpacing(14)
        g.setVerticalSpacing(2)
        self.boxes: dict[str, QCheckBox] = {}
        fmts = media.all_formats()
        for i, f in enumerate(fmts):
            cb = QCheckBox(f"{f.ext.lstrip('.').upper()}  {f.name.split('(')[0].strip()}")
            cb.setToolTip(f"{f.name}\nFiles: {', '.join(f.exts)}\n{f.notes}")
            cb.toggled.connect(self._touch)
            g.addWidget(cb, i // 2, i % 2)
            self.boxes[f.id] = cb
        self.note = QLabel()
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        g.addWidget(self.note, (len(fmts) + 1) // 2, 0, 1, 2)
        self._set(chosen if chosen is not None else suggested, how)

    def _set(self, ids: list[str], how: str):
        for fid, cb in self.boxes.items():
            cb.blockSignals(True)
            cb.setChecked(fid in ids)
            cb.blockSignals(False)
        self.note.setText({"suggested": "Suggested from the system's name - tick the ones it really uses.",
                           "inherited": "Taken from the family this is a variant of - change them to choose its own.",
                           "chosen": ""}.get(how, ""))

    def _touch(self, _=False):
        self.touched = True
        self.note.setText("")

    def suggest(self, ids: list[str]):
        """Follow the name as it's typed, until the user picks formats themselves."""
        if not self.touched:
            self._set(ids, "suggested")

    def value(self) -> list[str] | None:
        return [fid for fid, cb in self.boxes.items() if cb.isChecked()] if self.touched else None


# =========================================================================== viewing a file
class ImageFileDialog(QDialog):
    def __init__(self, parent, img: media.Image, e: media.Entry, title: str = ""):
        super().__init__(parent)
        self.img, self.e = img, e
        self.setWindowTitle(f"{e.name} - {title or img.fmt.name}")
        self.resize(760, 560)
        v = QVBoxLayout(self)
        how, text = media.text_view(img, e)
        self.how, self.text = how, text
        head = QLabel(f"<b>{e.name}</b> &nbsp; {e.describe(img.fmt.hex_prefix)} &nbsp; · &nbsp; {e.size:,} bytes"
                      f" &nbsp; · &nbsp; shown as {how}")
        head.setTextFormat(Qt.RichText)
        v.addWidget(head)
        ed = QPlainTextEdit(text)
        ed.setReadOnly(True)
        ed.setFont(theme.mono_font(10))
        ed.setLineWrapMode(QPlainTextEdit.NoWrap)
        v.addWidget(ed, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        cp = bb.addButton("Copy text", QDialogButtonBox.ActionRole)
        cp.clicked.connect(lambda: QApplication.clipboard().setText(self.text))
        sv = bb.addButton("Save file…", QDialogButtonBox.ActionRole)
        sv.setToolTip("Save the file itself (as stored on the image)")
        sv.clicked.connect(self._save)
        if how != "hex dump":
            st = bb.addButton("Save text…", QDialogButtonBox.ActionRole)
            st.clicked.connect(self._save_text)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _save(self):
        name = media_ops.host_name(self.img, self.e).split("/")[-1]
        fn, _ = QFileDialog.getSaveFileName(self, "Save file", name)
        if fn:
            Path(fn).write_bytes(media_ops.export_bytes(self.img, self.e))

    def _save_text(self):
        name = media_ops.readable_copy(self.img, self.e)[0]
        fn, _ = QFileDialog.getSaveFileName(self, "Save as text", name)
        if fn:
            Path(fn).write_text(self.text, "utf-8")


# =========================================================================== contents on the Document tab
class ImageContents(QGroupBox):
    """The directory of a disk / tape image document, with View / Extract / Add to library."""
    changed = Signal(list)          # ids of documents added to the library
    create_like = Signal(int)       # system id: open the Create dialog

    def __init__(self, lib: Library, standalone: bool = False):
        super().__init__("Disk contents")
        self.lib = lib
        self.doc: Document | None = None
        self.img: media.Image | None = None
        self.title_text = ""
        self.standalone = standalone          # an image file that isn't in the library (Open disk / tape image)
        v = QVBoxLayout(self)
        self.head = QLabel()
        self.head.setObjectName("muted")
        self.head.setWordWrap(True)
        self.head.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.head)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Type", "Size", "Details"])
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.itemDoubleClicked.connect(lambda it, _c: self._view(it))
        self.tree.itemSelectionChanged.connect(self._sel_changed)
        hdr = self.tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        v.addWidget(self.tree)
        row = QHBoxLayout()
        self.b_view = QPushButton("View")
        self.b_view.setToolTip("Show the file - BASIC programs as a listing, text as text, the rest as hex")
        self.b_view.clicked.connect(lambda: self._view(self.tree.currentItem()))
        self.b_extract = QPushButton("Extract…")
        self.b_extract.setToolTip("Save the selected files (or all of them) to a folder, exactly as stored")
        self.b_extract.clicked.connect(self._extract)
        self.b_add = QPushButton("Add to library")
        self.b_add.setToolTip("Add the selected files (or all) as documents of this system, linked to this image")
        self.b_add.clicked.connect(self._add)
        self.b_new = QPushButton("New image…")
        self.b_new.setToolTip("Create a new disk or tape image for this system")
        self.b_new.clicked.connect(lambda: self.doc and self.create_like.emit(self.doc.system_id))
        for b in (self.b_view, self.b_extract, self.b_add):
            row.addWidget(b)
        row.addStretch()
        row.addWidget(self.b_new)
        v.addLayout(row)
        if standalone:
            self.b_add.hide()
            self.b_new.hide()
        self.apply_icons()
        self.hide()

    def apply_icons(self):
        self.b_view.setIcon(theme.icon("view"))
        self.b_extract.setIcon(theme.icon("export"))
        self.b_add.setIcon(theme.icon("add"))
        self.b_new.setIcon(theme.icon("floppy"))

    def show_doc(self, d: Document | None) -> bool:
        self.doc, self.img = d, None
        fmt = media_ops.doc_format(self.lib, d) if d else None
        if fmt is None:
            self.hide()
            return False
        try:
            img = fmt.open(self.lib.abspath(d.path).read_bytes())
        except Exception as e:
            self.setTitle("Disk / tape contents")
            self.head.setText(f"Could not read this {fmt.name}: {e}")
            self.tree.clear()
            self.show()
            return True
        return self.show_image(img, d.title)

    def show_image(self, img: media.Image, title: str) -> bool:
        self.img, self.title_text = img, title
        fmt = img.fmt
        word = media.medium_word(fmt)
        self.setTitle(f"{word} contents - {fmt.name}")
        bits = ([f"<b>{word} name:</b> {img.label}"] if img.label else []) + img.info
        self.head.setText(" · ".join(bits))
        self.tree.clear()
        self.tree.setRootIsDecorated(fmt.dirs)
        items: dict[str, QTreeWidgetItem] = {}
        for e in img.entries():
            parent_path, _, leaf = e.name.rpartition("/")
            parent = items.get(parent_path)
            it = QTreeWidgetItem([leaf if parent is not None else e.name, "folder" if e.is_dir else e.kind,
                                  "" if e.is_dir else f"{e.size:,}",
                                  "" if e.is_dir else e.describe(fmt.hex_prefix, kind=False)])
            it.setData(0, Qt.UserRole, e.name)
            it.setIcon(0, theme.icon("folder" if e.is_dir else "doc", theme.T["muted"]))
            it.setTextAlignment(2, Qt.AlignRight | Qt.AlignVCenter)
            (parent.addChild(it) if parent is not None else self.tree.addTopLevelItem(it))
            if e.is_dir:
                items[e.name] = it
        self.tree.expandAll()
        if not self.standalone:               # on the Document tab the list is as tall as its contents
            n = max(1, min(14, len(img.entries())))
            rowh = max(20, self.tree.sizeHintForRow(0)) if img.entries() else 22
            hdr = max(24, self.tree.header().sizeHint().height())
            self.tree.setFixedHeight(hdr + n * rowh + 2 * self.tree.frameWidth() + 6)
        self._sel_changed()
        self.show()
        return True

    def _names(self) -> list[str]:
        return [it.data(0, Qt.UserRole) for it in self.tree.selectedItems()
                if not self.img.find(it.data(0, Qt.UserRole)).is_dir]

    def _sel_changed(self):
        n = len(self.tree.selectedItems())
        files = bool(self.img and self.img.files())
        self.b_extract.setText(f"Extract {n}…" if n else "Extract all…")
        self.b_add.setText(f"Add {n} to library" if n else "Add all to library")
        self.b_view.setEnabled(n == 1)
        self.b_extract.setEnabled(files)
        self.b_add.setEnabled(files)

    def _view(self, it):
        if not it or not self.img:
            return
        e = self.img.find(it.data(0, Qt.UserRole))
        if e.is_dir:
            return
        try:
            ImageFileDialog(self, self.img, e, self.title_text).exec()
        except Exception as ex:
            QMessageBox.warning(self, "View file", f"Could not read {e.name}: {ex}")

    def _extract(self):
        d = QFileDialog.getExistingDirectory(self, "Extract files to")
        if not d:
            return
        names = self._names() or None
        dest = Path(d) / media.safe_host_name(self.title_text) if not names else Path(d)
        try:
            out = media_ops.extract_to_folder(self.img, names, dest)
        except Exception as e:
            QMessageBox.warning(self, "Extract", str(e))
            return
        from ..platform_util import open_path
        if QMessageBox.information(self, "Extracted", f"Saved {len(out)} file(s) to\n{dest}",
                                   QMessageBox.Open | QMessageBox.Ok) == QMessageBox.Open:
            open_path(dest)

    def _add(self):
        names = self._names() or None
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ids = media_ops.add_entries_to_library(self.lib, self.doc, names, log=lambda m: None)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Add to library", str(e))
            return
        QApplication.restoreOverrideCursor()
        self.changed.emit(ids)


# =========================================================================== any image file, not in the library
def image_file_filter() -> str:
    exts = sorted(media.media_exts())
    return (f"Disk and tape images ({' '.join('*' + e for e in exts)} *.zip);;"
            + ";;".join(f"{f.name} ({' '.join('*' + e for e in f.exts)})" for f in media.all_formats())
            + ";;All files (*)")


def image_from_file(parent, path: Path):
    """Open an image file (or the image inside a zip) -> (image, title, path of the image file) or None."""
    import tempfile
    import zipfile
    path = Path(path)
    if path.suffix.lower() == ".zip" and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            members = [m for m in zf.namelist() if Path(m).suffix.lower() in media.media_exts() | {".md"}]
            found = []
            for m in members:
                data = zf.read(m)
                if media.detect(data, Path(m).suffix):
                    found.append((m, data))
        if not found:
            QMessageBox.information(parent, "Open disk / tape image", f"There's no disk or tape image in {path.name}.")
            return None
        pick = found[0]
        if len(found) > 1:
            from PySide6.QtWidgets import QInputDialog
            name, ok = QInputDialog.getItem(parent, "Open disk / tape image", f"{path.name} holds {len(found)} images:",
                                            [m for m, _ in found], 0, False)
            if not ok:
                return None
            pick = next(f for f in found if f[0] == name)
        tmp = Path(tempfile.mkdtemp(prefix="retrorag-open-"))
        import atexit
        import shutil
        atexit.register(shutil.rmtree, tmp, True)          # kept until RetroRAG closes (it may be added later)
        target = tmp / Path(pick[0]).name
        target.write_bytes(pick[1])
        path = target
    try:
        img = media.open_image(path)
    except Exception as e:
        QMessageBox.warning(parent, "Open disk / tape image", f"Can't read {path.name}: {e}")
        return None
    return img, path.stem, path


class ImageBrowserDialog(QDialog):
    """Look inside a disk / tape image file and extract from it, without adding it to the library."""
    add_to_library = Signal(str)          # path of the image file

    def __init__(self, parent, lib: Library, img: media.Image, title: str, path: Path):
        super().__init__(parent)
        self.path = path
        self.setWindowTitle(f"{path.name} - {img.fmt.name}")
        self.resize(820, 600)
        v = QVBoxLayout(self)
        note = QLabel("This image isn't in the library. View or extract its files here, or add it to the library.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        v.addWidget(note)
        self.contents = ImageContents(lib, standalone=True)
        self.contents.show_image(img, title)
        v.addWidget(self.contents, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        add = bb.addButton("Add image to library…", QDialogButtonBox.ActionRole)
        add.setIcon(theme.icon("add"))
        add.clicked.connect(lambda: (self.add_to_library.emit(str(self.path)), self.accept()))
        bb.rejected.connect(self.reject)
        v.addWidget(bb)


# =========================================================================== creating an image
class _TextDialog(QDialog):
    """Type or paste a BASIC program or a text file."""

    def __init__(self, parent, fmt: media.MediaFormat, basic: bool, name: str = "", text: str = ""):
        super().__init__(parent)
        self.fmt, self.basic = fmt, basic
        self.setWindowTitle("New BASIC program" if basic else "New text file")
        self.resize(640, 480)
        v = QVBoxLayout(self)
        f = QFormLayout()
        self.name = QLineEdit(name)
        if fmt.max_name:
            self.name.setMaxLength(fmt.max_name)
        f.addRow("Name on the " + fmt.medium, self.name)
        v.addLayout(f)
        hint = QLabel({"cbm": "Commodore BASIC V2. Type keywords in CAPITALS (a program in all lower case is "
                              "read as capitals). Codes like {clr}, {rvs on}, {$93} put control characters in strings.",
                       "zx": "Sinclair BASIC (48K keywords). Write keywords as words, e.g. GO TO, PRINT AT 0,0;. "
                             "Numbers get their hidden 5-byte form, so LOAD \"\" runs it. Autostart = the first line.",
                       "bbc": "BBC BASIC. Saved with load &FF1900 / exec &FF8023 so CHAIN works."}
                      .get(fmt.basic, "") if basic else
                      "Plain text - converted to the machine's own characters and line endings.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        v.addWidget(hint)
        self.ed = QPlainTextEdit(text)
        self.ed.setFont(theme.mono_font(10))
        self.ed.setPlaceholderText('10 PRINT "HELLO"\n20 GOTO 10' if basic else "")
        v.addWidget(self.ed, 1)
        self.err = QLabel()
        self.err.setStyleSheet(f"color: {theme.T['danger']}")
        self.err.setWordWrap(True)
        v.addWidget(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _ok(self):
        if not self.name.text().strip():
            self.err.setText("Give it a name.")
            return
        if self.basic:
            try:
                media.program_from_text(self.fmt, self.ed.toPlainText())
            except Exception as e:
                self.err.setText(str(e))
                return
        self.accept()


class _DocPicker(QDialog):
    def __init__(self, parent, lib: Library, system_id: int):
        super().__init__(parent)
        self.setWindowTitle("Add files from the library")
        self.resize(560, 480)
        v = QVBoxLayout(self)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by title or category…")
        self.filter.textChanged.connect(self._refill)
        v.addWidget(self.filter)
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.ExtendedSelection)
        v.addWidget(self.list, 1)
        self.docs = [d for sid in lib.system_scope(system_id) for d in lib.documents(sid)]
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Add")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._refill()

    def _refill(self):
        q = self.filter.text().strip().lower()
        self.list.clear()
        for d in self.docs:
            if q and q not in d.title.lower() and q not in d.category.lower():
                continue
            it = QListWidgetItem(f"{d.title}   ({d.category}, {Path(d.path).name})")
            it.setData(Qt.UserRole, d.id)
            self.list.addItem(it)

    def ids(self) -> list[int]:
        return [it.data(Qt.UserRole) for it in self.list.selectedItems()]


COL_NAME, COL_TYPE, COL_A, COL_B, COL_SIZE, COL_FROM = range(6)


class CreateImageDialog(QDialog):
    """Build a disk / tape image for a system from files, library documents, BASIC programs or text."""

    def __init__(self, parent, lib: Library, system_id: int | None = None, doc_ids: list[int] | None = None):
        super().__init__(parent)
        from .dialogs import system_choices
        self.lib = lib
        self.created_doc: int | None = None
        self.rows: list[dict] = []
        self.setWindowTitle("Create disk / tape image")
        self.resize(900, 640)
        self.setAcceptDrops(True)
        v = QVBoxLayout(self)
        f = QFormLayout()
        self.system = QComboBox()
        for label, sid in system_choices(lib):
            self.system.addItem(label, sid)
        self.system.addItem("(none - just Save to file)", None)
        self.system.setCurrentIndex(self.system.findData(system_id) if system_id is not None
                                    else self.system.count() - 1)
        self.system.currentIndexChanged.connect(self._fill_formats)
        self.fmt_box = QComboBox()
        self.fmt_box.currentIndexChanged.connect(self._format_changed)
        self.label = QLineEdit()
        self.label.textChanged.connect(self._schedule)
        self.title = QLineEdit()
        self.title.setPlaceholderText("Title in the library (default: the disk name)")
        f.addRow("System", self.system)
        f.addRow("Format", self.fmt_box)
        self.label_lbl = QLabel("Disk name")
        f.addRow(self.label_lbl, self.label)
        f.addRow("Library title", self.title)
        self.form = f
        self.opts: dict[str, QComboBox] = {}
        v.addLayout(f)
        self.notes = QLabel()
        self.notes.setObjectName("muted")
        self.notes.setWordWrap(True)
        v.addWidget(self.notes)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Name on image", "Type", "Load", "Exec", "Size", "From"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        for c in (COL_TYPE, COL_A, COL_B, COL_SIZE):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(COL_FROM, QHeaderView.Interactive)
        self.table.setColumnWidth(COL_FROM, 200)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.itemChanged.connect(lambda _it: self._schedule())
        v.addWidget(self.table, 1)
        row = QHBoxLayout()
        for text, slot, ic in (("Add files…", self._add_files, "doc"), ("From library…", self._add_docs, "library"),
                               ("New BASIC program…", lambda: self._add_text(True), "code"),
                               ("New text file…", lambda: self._add_text(False), "text"),
                               ("Remove", self._remove, "delete")):
            b = QPushButton(text)
            b.setIcon(theme.icon(ic))
            b.clicked.connect(slot)
            row.addWidget(b)
            if text.startswith("New BASIC"):
                self.b_basic = b
        row.addStretch()
        v.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        bb = QDialogButtonBox()
        self.b_create = bb.addButton("Create in library", QDialogButtonBox.AcceptRole)
        self.b_create.setObjectName("primary")
        self.b_save = bb.addButton("Save to file…", QDialogButtonBox.ActionRole)
        bb.addButton(QDialogButtonBox.Cancel)
        self.b_create.clicked.connect(self._create)
        self.b_save.clicked.connect(self._save_file)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._timer = QTimer(self, singleShot=True, interval=250, timeout=self._validate)
        self.fmt = None
        self._fill_formats()
        for did in doc_ids or []:
            self._add_doc(did)

    # ------------------------------------------------------------------ formats & options
    def _fill_formats(self):
        sid = self.system.currentData()
        ids, how = self.lib.system_formats(sid) if sid else ([], "suggested")
        cur = self.fmt_box.currentData()
        self.fmt_box.blockSignals(True)
        self.fmt_box.clear()
        for fid in ids:
            f = media.get(fid)
            if f.writable:
                self.fmt_box.addItem(theme.icon("floppy"), f"{f.name}", f.id)
        if ids:
            self.fmt_box.insertSeparator(self.fmt_box.count())
        for f in media.all_formats():
            if f.id not in ids and f.writable:
                self.fmt_box.addItem(f.name + "   (not set for this system)", f.id)
        self.fmt_box.setCurrentIndex(max(0, self.fmt_box.findData(cur)) if cur in ids else 0)
        self.fmt_box.blockSignals(False)
        self._format_changed()

    def _format_changed(self):
        fid = self.fmt_box.currentData()
        if not fid:
            return
        old, self.fmt = self.fmt, media.get(fid)
        f = self.fmt
        word = media.medium_word(f)
        self.label_lbl.setText(f"{word} name")
        self.label.setMaxLength(f.max_label or 60)
        self.label.setEnabled(f.max_label != 0)
        self.label.setPlaceholderText("(tapes have no name)" if f.max_label == 0 else f"up to {f.max_label} characters")
        for cb in self.opts.values():                 # options go straight into the form, after the name
            self.form.removeRow(cb)
        self.opts = {}
        at = 3
        for o in f.options:
            cb = QComboBox()
            for lab, val in o.choices:
                cb.addItem(lab, val)
            cb.setToolTip(o.help)
            if o.key == "disk_id":
                cb.setEditable(True)
                cb.lineEdit().setMaxLength(2)
            cb.currentIndexChanged.connect(self._schedule)
            if cb.isEditable():
                cb.editTextChanged.connect(self._schedule)
            self.form.insertRow(at, o.label, cb)
            at += 1
            self.opts[o.key] = cb
        self.notes.setText(f.notes + ("  BASIC programs can be typed in with New BASIC program."
                                      if f.basic else ""))
        self.b_basic.setEnabled(bool(f.basic))
        heads = {"load": ("Load address", ""), "load+exec": ("Load (hex)", "Exec (hex)"),
                 "start+autostart": ("Start (Bytes)", "Autostart line"), "": ("", "")}[f.addresses]
        self.table.setHorizontalHeaderItem(COL_A, QTableWidgetItem(heads[0]))
        self.table.setHorizontalHeaderItem(COL_B, QTableWidgetItem(heads[1]))
        self.table.setColumnHidden(COL_A, not heads[0])
        self.table.setColumnHidden(COL_B, not heads[1])
        self.table.setColumnHidden(COL_TYPE, len(f.kinds) <= 1)
        if old is not None and old.id != f.id:
            for r in self.rows:
                self._guess(r, keep_name=r.get("named", False))
        self._refresh_table()

    def _opt_values(self) -> dict:
        out = {}
        for key, cb in self.opts.items():
            if cb.isEditable():
                out[key] = cb.currentText().strip() or "RR"
            else:
                out[key] = cb.currentData()
        return out

    # ------------------------------------------------------------------ rows
    def _guess(self, r: dict, keep_name: bool = False):
        f = self.fmt
        name = r.get("name")
        if r["src"] == "basic":
            prog, kind = media.program_from_text(f, r["text"]) if f.basic else (b"", "")
            nf = NewFile(r["name"], prog, kind)
            if f.basic == "zx":
                nf.exec = int.from_bytes(prog[:2], "big")
        elif r["src"] == "text":
            nf = NewFile(r["name"], media_ops.text_to_bytes(f, r["text"]), "SEQ" if f.basic == "cbm" else "")
        else:
            nf = media.prepare(f, r["host"], r["data"])
            if f.id in ("ssd", "dsd", "adl") and r.get("path"):
                load, exe = media.read_inf(Path(r["path"]))
                if load is not None:
                    nf.load, nf.exec = load, exe
        if keep_name and name:
            nf.name = name
        if f.max_name and not f.dirs:
            nf.name = nf.name[:f.max_name]
        r["nf"] = nf
        r["name"] = nf.name
        r["cbm_header"] = f.basic == "cbm" and nf.kind == "PRG" and nf.load is None and len(nf.data) >= 2

    def _add_row(self, r: dict):
        self._guess(r)
        self.rows.append(r)
        self._refresh_table()

    def _add_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Files to put on the image")
        for fn in files:
            self._add_path(Path(fn))

    def _add_path(self, p: Path):
        if p.suffix.lower() == ".inf" or not p.is_file():
            return
        self._add_row({"src": "file", "host": p.name, "data": p.read_bytes(), "from": str(p), "path": str(p)})

    def _add_docs(self):
        sid = self.system.currentData()
        if not sid:
            QMessageBox.information(self, "From library", "Choose a system first - its documents are listed.")
            return
        dlg = _DocPicker(self, self.lib, sid)
        if dlg.exec():
            for did in dlg.ids():
                self._add_doc(did)

    def _add_doc(self, did: int):
        d = self.lib.document(did)
        if not d:
            return
        p = self.lib.abspath(d.path)
        self._add_row({"src": "doc", "doc_id": d.id, "host": p.name, "data": p.read_bytes(),
                       "from": f"library: {d.title}", "path": str(p)})

    def _add_text(self, basic: bool):
        dlg = _TextDialog(self, self.fmt, basic)
        if dlg.exec():
            self._add_row({"src": "basic" if basic else "text", "name": dlg.name.text().strip(),
                           "text": dlg.ed.toPlainText(), "from": "BASIC program" if basic else "text",
                           "named": True})

    def _remove(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            del self.rows[r]
        self._refresh_table()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            p = Path(u.toLocalFile())
            if p.is_dir():
                for q in sorted(p.rglob("*")):
                    if q.is_file():
                        self._add_row({"src": "file", "host": q.relative_to(p.parent).as_posix() if self.fmt.dirs
                                       else q.name, "data": q.read_bytes(), "from": str(q), "path": str(q)})
            else:
                self._add_path(p)

    def _fmt_a(self, nf: NewFile, r: dict) -> str:
        f = self.fmt
        if f.addresses == "start+autostart":
            return "" if nf.kind == "Program" else str(nf.load if nf.load is not None else 32768)
        if r.get("cbm_header"):
            return media.fmt_addr(nf.data[0] | nf.data[1] << 8, "$")
        return media.fmt_addr(nf.load, f.hex_prefix) if nf.load is not None else ""

    def _fmt_b(self, nf: NewFile) -> str:
        f = self.fmt
        if f.addresses == "start+autostart":
            return str(nf.exec) if nf.kind == "Program" and nf.exec is not None else ""
        return media.fmt_addr(nf.exec, f.hex_prefix) if nf.exec is not None else ""

    def _refresh_table(self):
        f = self.fmt
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        for i, r in enumerate(self.rows):
            nf = r["nf"]
            name_it = QTableWidgetItem(nf.name)
            self.table.setItem(i, COL_NAME, name_it)
            if len(f.kinds) > 1:
                cb = QComboBox()
                cb.addItems(list(f.kinds))
                cb.setCurrentText(nf.kind if nf.kind in f.kinds else f.kinds[0])
                cb.setEnabled(r["src"] != "basic")
                cb.currentTextChanged.connect(lambda t, row=i: self._kind_changed(row, t))
                self.table.setCellWidget(i, COL_TYPE, cb)
            else:
                self.table.removeCellWidget(i, COL_TYPE)
            a, b = QTableWidgetItem(self._fmt_a(nf, r)), QTableWidgetItem(self._fmt_b(nf))
            kind = nf.kind
            a_on = not (f.addresses == "start+autostart" and kind == "Program") and not (
                f.basic == "cbm" and kind != "PRG")
            b_on = f.addresses == "load+exec" or (f.addresses == "start+autostart" and kind == "Program")
            for it, on in ((a, a_on), (b, b_on)):
                if not on:
                    it.setFlags(it.flags() & ~Qt.ItemIsEnabled & ~Qt.ItemIsEditable)
            self.table.setItem(i, COL_A, a)
            self.table.setItem(i, COL_B, b)
            sz = QTableWidgetItem(f"{len(nf.data):,}")
            sz.setFlags(sz.flags() & ~Qt.ItemIsEditable)
            sz.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(i, COL_SIZE, sz)
            fr = QTableWidgetItem(r["from"])
            fr.setFlags(fr.flags() & ~Qt.ItemIsEditable)
            fr.setToolTip(r["from"])
            self.table.setItem(i, COL_FROM, fr)
        self.table.blockSignals(False)
        self._schedule()

    def _kind_changed(self, row: int, kind: str):
        if 0 <= row < len(self.rows):
            self.rows[row]["nf"].kind = kind
            self._read_table()
            self._refresh_table()

    def _read_table(self):
        """Edits in the table -> the rows' NewFiles."""
        f = self.fmt
        for i, r in enumerate(self.rows):
            nf = r["nf"]
            it = self.table.item(i, COL_NAME)
            if it and it.text().strip() and it.text().strip() != nf.name:
                nf.name = it.text().strip()
                r["named"] = True
                r["name"] = nf.name
            cb = self.table.cellWidget(i, COL_TYPE)
            if isinstance(cb, QComboBox):
                nf.kind = cb.currentText()
            a = (self.table.item(i, COL_A).text().strip() if self.table.item(i, COL_A) else "")
            b = (self.table.item(i, COL_B).text().strip() if self.table.item(i, COL_B) else "")
            if f.addresses == "start+autostart":
                if nf.kind == "Program":
                    nf.exec = media_ops.parse_number(b, False) if b else None
                else:
                    nf.load = media_ops.parse_number(a, False) if a else None
            elif f.basic == "cbm":
                if nf.kind == "PRG" and a:
                    v = media_ops.parse_number(a)
                    if r.get("cbm_header"):
                        nf.data = bytes([v & 0xFF, v >> 8 & 0xFF]) + bytes(nf.data[2:])
                    else:
                        nf.load = v
            elif f.addresses:
                nf.load = media_ops.parse_number(a) if a else None
                nf.exec = media_ops.parse_number(b) if b else None

    def _files(self) -> list[NewFile]:
        self._read_table()
        return [NewFile(r["nf"].name, r["nf"].data, r["nf"].kind, r["nf"].load, r["nf"].exec) for r in self.rows]

    # ------------------------------------------------------------------ build
    def _schedule(self, *_):
        self._timer.start()

    def _build(self) -> bytes:
        files = self._files()
        if not files:
            raise MediaError("Add some files: Add files…, From library…, or New BASIC program…")
        return self.fmt.create(files, self.label.text().strip(), **self._opt_values())

    def _validate(self):
        t = theme.T
        try:
            raw = self._build()
        except Exception as e:
            self.status.setText(f"<span style='color:{t['warn']}'>&#9888; {e}</span>")
            self.b_create.setEnabled(False)
            self.b_save.setEnabled(False)
            return False
        total = sum(len(r["nf"].data) for r in self.rows)
        self.status.setText(f"<span style='color:{t['ok']}'>&#10003; Ready: "
                            f"{len(self.rows)} file(s), {total:,} bytes of data, image {len(raw) // 1024:,} KB</span>"
                            + ("" if self.system.currentData() else
                               "  - choose a system to create it in the library, or use Save to file…"))
        self.b_create.setEnabled(self.system.currentData() is not None)
        self.b_save.setEnabled(True)
        return True

    def _default_name(self) -> str:
        return (self.title.text().strip() or self.label.text().strip() or "New " + self.fmt.medium)

    def _save_file(self):
        try:
            raw = self._build()
        except Exception as e:
            QMessageBox.warning(self, "Save image", str(e))
            return
        ext = self.fmt.ext
        if self.fmt.id == "adl" and self._opt_values().get("size") in ("M", "S"):
            ext = ".ad" + self._opt_values()["size"].lower()
        fn, _ = QFileDialog.getSaveFileName(self, "Save image", self._default_name() + ext,
                                            f"{self.fmt.name} (*{' *'.join(self.fmt.exts)});;All files (*)")
        if fn:
            Path(fn).write_bytes(raw)
            self.status.setText(f"&#10003; Saved to {fn}")

    def _create(self):
        if not self._validate() or self.system.currentData() is None:
            return
        s = self.lib.system(self.system.currentData())
        try:
            self.created_doc = media_ops.create_image_doc(
                self.lib, s, self.fmt.id, self._files(), label=self.label.text().strip(),
                title=self._default_name(), options=self._opt_values(),
                source_doc_ids=[r["doc_id"] for r in self.rows if r.get("doc_id")], log=lambda m: None)
        except Exception as e:
            QMessageBox.warning(self, "Create image", str(e))
            return
        self.accept()
