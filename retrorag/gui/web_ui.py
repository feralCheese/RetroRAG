"""Links on systems / peripherals: adding and editing one, and the Links window (re-copy reminders, gone sites)."""
from __future__ import annotations

from urllib.parse import urlsplit

from PySide6.QtCore import Qt, Signal, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QRadioButton, QSpinBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from ..config import CATEGORIES
from ..library import LINK_MODES, Library, Link
from .. import webcopy
from . import theme

MODE_TEXT = {
    "link": "Just save the link - the AI is told about it (fine for Claude, which can browse; local AIs can't)",
    "page": "Copy this page into the library (or the PDF / text file the link points at)",
    "site": "Copy this page and the pages under it on the same site",
    "repo": "Copy the GitHub repository (imported like a source tree)",
}


SHORT_MODES = {"link": "Saved link", "page": "Copy: page", "site": "Copy: site section", "repo": "Copy: GitHub repo"}


def status_text(l: Link) -> tuple[str, str]:
    """(short status, colour key) for lists."""
    if l.status == "gone":
        return "gone", "danger"
    if l.status in ("refused", "error"):
        return "needs attention", "warn"
    if l.status == "unreachable":
        return "couldn't reach", "warn"
    if l.is_due():
        return ("re-check due" if l.last_fetched else ("not copied yet" if l.copies else "not checked yet")), "warn"
    if l.status == "ok":
        return "✓", "ok"
    return "", "muted"


def link_tooltip(lib: Library, l: Link) -> str:
    due = l.next_due()
    lines = [l.url, LINK_MODES.get(l.mode, l.mode)]
    if l.copies:
        lines.append(f"{len(lib.link_doc_ids(l.id))} document(s) copied into {l.category or 'the library'}")
    if l.last_fetched:
        lines.append(f"Last {'copied' if l.copies else 'worked'}: {l.last_fetched[:10]}")
    lines.append("Next check: " + (due.date().isoformat() if due else "none") +
                 f" ({webcopy.refresh_label(l.refresh_days)})")
    if l.status_msg:
        lines.append(l.status_msg)
    if l.note:
        lines.append("Note: " + l.note)
    return "\n".join(lines)


class LinkDialog(QDialog):
    def __init__(self, parent, lib: Library, link: Link | None = None, system_id: int | None = None):
        super().__init__(parent)
        from .dialogs import system_choices
        self.lib, self.link = lib, link
        self.setWindowTitle("Edit link" if link else "Add link")
        self.setMinimumWidth(620)
        v = QVBoxLayout(self)
        f = QFormLayout()
        self.url = QLineEdit(link.url if link else "")
        self.url.setPlaceholderText("https://...  - a manual, a docs site, a wiki page, a GitHub repository")
        self.hint = QLabel()
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        self.title = QLineEdit(link.title if link else "")
        self.title.setPlaceholderText("optional - taken from the page if left empty")
        self.system = QComboBox()
        for label, sid in system_choices(lib):
            self.system.addItem(label, sid)
        self.system.setCurrentIndex(max(0, self.system.findData(link.system_id if link else system_id)))
        self.note = QPlainTextEdit(link.note if link else "")
        self.note.setFixedHeight(56)
        self.note.setPlaceholderText("What it is and why it's useful - the AI sees this too")
        f.addRow("Address", self.url)
        f.addRow("", self.hint)
        f.addRow("Title", self.title)
        f.addRow("System", self.system)
        f.addRow("Note", self.note)
        v.addLayout(f)

        box = QGroupBox("Keep as")
        bv = QVBoxLayout(box)
        self.modes = QButtonGroup(self)
        self.radio: dict[str, QRadioButton] = {}
        for key in ("link", "page", "site", "repo"):
            r = QRadioButton(MODE_TEXT[key])
            self.modes.addButton(r)
            self.radio[key] = r
            bv.addWidget(r)
            if key == "site":
                row = QHBoxLayout()
                row.setContentsMargins(24, 0, 0, 0)
                self.max_pages = QSpinBox(); self.max_pages.setRange(2, 2000)
                self.max_pages.setValue(link.max_pages if link else 100)
                self.max_pages.setPrefix("up to "); self.max_pages.setSuffix(" pages")
                self.files = QCheckBox("and the PDFs and text files they link to on that site")
                self.files.setChecked(bool(link.include_files) if link else True)
                row.addWidget(self.max_pages); row.addWidget(self.files); row.addStretch()
                bv.addLayout(row)
                self.scope = QLabel()
                self.scope.setObjectName("muted")
                self.scope.setWordWrap(True)
                self.scope.setContentsMargins(24, 0, 0, 0)
                bv.addWidget(self.scope)
            if key == "repo":
                row = QHBoxLayout()
                row.setContentsMargins(24, 0, 0, 0)
                self.branch = QLineEdit(link.branch if link else "")
                self.branch.setPlaceholderText("branch, tag or commit (empty = the default branch)")
                row.addWidget(QLabel("Branch / tag")); row.addWidget(self.branch, 1)
                bv.addLayout(row)
        v.addWidget(box)

        g = QFormLayout()
        self.category = QComboBox(); self.category.setEditable(True)
        self.category.addItems([c for c in CATEGORIES if c != "My Notes"])
        self.refresh = QComboBox()
        for days, label in webcopy.REFRESH_CHOICES:
            self.refresh.addItem("never" if days == 0 else f"every {label}", days)
        g.addRow("Copies go in", self.category)
        g.addRow("Remind me to re-check", self.refresh)
        self.refresh_hint = QLabel("Re-copying picks up changes. A site that's gone is marked as gone - its copy "
                                   "stays in the library and stops counting on the Home page.")
        self.refresh_hint.setObjectName("muted")
        self.refresh_hint.setWordWrap(True)
        g.addRow("", self.refresh_hint)
        v.addLayout(g)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok = bb.button(QDialogButtonBox.Ok)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

        mode = link.mode if link else None
        self._user_mode = bool(link)
        if link:
            self.category.setCurrentText(link.category or self._default_category(link.mode))
            self.refresh.setCurrentIndex(max(0, self.refresh.findData(link.refresh_days)))
        self.radio[mode or "page"].setChecked(True)
        self.modes.buttonClicked.connect(self._mode_clicked)
        self.url.textChanged.connect(self._url_changed)
        self._url_changed()
        self._update()

    # ------------------------------------------------------------------ behaviour
    def mode(self) -> str:
        for k, r in self.radio.items():
            if r.isChecked():
                return k
        return "link"

    @staticmethod
    def _default_category(mode: str) -> str:
        return "Emulator Source" if mode == "repo" else "User Guides & Manuals"

    def _mode_clicked(self, _b):
        self._user_mode = True
        if not self.link:
            self.category.setCurrentText(self._default_category(self.mode()))
            self.refresh.setCurrentIndex(max(0, self.refresh.findData(365 if self.mode() == "link" else 180)))
        self._update()

    def _url_changed(self):
        url = self.url.text().strip()
        gh = webcopy.github_repo(url if "://" in url else "https://" + url) if url else None
        is_repo = bool(gh and not gh["blob"])
        self.radio["repo"].setEnabled(is_repo)
        if not self._user_mode and url:
            self.radio["repo" if is_repo else "page"].setChecked(True)
            if not self.link:
                self.category.setCurrentText(self._default_category(self.mode()))
                self.refresh.setCurrentIndex(max(0, self.refresh.findData(180)))
        if not is_repo and self.radio["repo"].isChecked():
            self.radio["page"].setChecked(True)
        if gh:
            self.hint.setText(f"GitHub repository {gh['owner']}/{gh['repo']}" +
                              (f", {gh['ref']}" if gh["ref"] else "") + (f", folder {gh['path']}" if gh["path"] else "")
                              if not gh["blob"] else "A file in a GitHub repository - its raw text is copied.")
        else:
            self.hint.setText("")
        self.hint.setVisible(bool(self.hint.text()))
        self._update()

    def _update(self):
        m = self.mode()
        site = m == "site"
        for w in (self.max_pages, self.files):
            w.setEnabled(site)
        self.branch.setEnabled(m == "repo")
        self.category.setEnabled(m != "link")
        url = self.url.text().strip()
        if site and url:
            try:
                full = url if "://" in url else "https://" + url
                pre = webcopy.scope_prefix(full)
                self.scope.setText(f"Pages whose address starts with {pre} - nothing from other sites. One page a "
                                   "second, only what the site's robots.txt allows.")
            except ValueError:
                self.scope.setText("")
        else:
            self.scope.setText("")
        self.scope.setVisible(site and bool(url))
        self._fit()
        self.ok.setText(("Save and copy" if not self.link else "Save and re-copy") if m != "link" else "Save")

    def _fit(self):
        """Grow to show wrapped hint text in full (it changes as the address is typed)."""
        lay = self.layout()
        if lay is None:
            return
        lay.activate()
        want = lay.totalHeightForWidth(self.width()) if lay.hasHeightForWidth() else self.sizeHint().height()
        if self.isVisible() and want > self.height():
            self.resize(self.width(), want)

    def _ok(self):
        url = self.url.text().strip()
        if not url:
            QMessageBox.warning(self, "Link", "Enter the address of the page, site or repository.")
            return
        if self.system.currentData() is None:
            QMessageBox.warning(self, "Link", "Add a system first - links belong to a system or peripheral.")
            return
        full = url if "://" in url else "https://" + url
        if not urlsplit(full).netloc or urlsplit(full).scheme not in ("http", "https"):
            QMessageBox.warning(self, "Link", "That doesn't look like a web address (https://...).")
            return
        self.accept()

    def values(self) -> dict:
        m = self.mode()
        return {"url": self.url.text().strip(), "title": self.title.text().strip(), "note": self.note.toPlainText().strip(),
                "system_id": self.system.currentData(), "mode": m,
                "category": self.category.currentText().strip() if m != "link" else "",
                "max_pages": self.max_pages.value(), "include_files": int(self.files.isChecked()),
                "branch": self.branch.text().strip() if m == "repo" else "", "refresh_days": self.refresh.currentData()}


def save_link_dialog(parent, lib: Library, link: Link | None = None, system_id: int | None = None) -> int | None:
    """Show the dialog; save the link. -> id of a link that now needs copying / checking, or None."""
    dlg = LinkDialog(parent, lib, link, system_id)
    if not dlg.exec():
        return None
    v = dlg.values()
    try:
        if link is None:
            new = lib.add_link(v.pop("system_id"), v.pop("url"), **v)
            return new.id
        changed = any(str(getattr(link, k)) != str(v[k]) for k in ("url", "mode", "branch", "max_pages",
                                                                   "include_files"))
        if changed or v["system_id"] != link.system_id:
            v.update(status="", status_msg="", fail_count=0, first_failed=None, version="")
        lib.update_link(link.id, **v)
        return link.id if changed or link.status in ("gone", "refused", "error", "") else None
    except ValueError as e:
        QMessageBox.warning(parent, "Link", str(e))
        return None


def remove_link_dialog(parent, lib: Library, link: Link) -> tuple[bool, bool] | None:
    """-> (removed, documents deleted) or None if cancelled."""
    n = len(lib.link_doc_ids(link.id))
    box = QMessageBox(parent)
    box.setWindowTitle("Remove link")
    box.setText(f"Remove the link '{link.label}'?")
    keep = delete = None
    if n:
        box.setInformativeText(f"{n} document(s) were copied from it. You can keep them in the library as ordinary "
                               "documents, or delete them and their files (that can't be undone).")
        keep = box.addButton("Remove link, keep documents", QMessageBox.AcceptRole)
        delete = box.addButton(f"Remove link and delete {n} document(s)", QMessageBox.DestructiveRole)
    else:
        keep = box.addButton("Remove", QMessageBox.AcceptRole)
    box.addButton(QMessageBox.Cancel)
    box.exec()
    if box.clickedButton() not in (keep, delete):
        return None
    lib.delete_link(link.id, delete_docs=box.clickedButton() is delete)
    return True, box.clickedButton() is delete


class LinksWindow(QDialog):
    """Every link (or one system's): what's due for a re-check, what's gone, and the actions."""
    run_links = Signal(list)         # link ids to copy / check
    changed = Signal()
    docs_deleted = Signal(list)

    COLS = ["Link", "System", "Kept as", "Docs", "Last worked", "Next check", "Status"]

    def __init__(self, parent, lib: Library, system_id: int | None = None, due_only: bool = False):
        super().__init__(parent)
        self.lib, self.system_id = lib, system_id
        self.setWindowTitle("Links")
        self.setModal(False)
        self.resize(1040, 520)
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.heading = QLabel()
        self.heading.setObjectName("heading")
        top.addWidget(self.heading, 1)
        self.only_due = QCheckBox("Only links due for a re-check")
        self.only_due.setChecked(due_only)
        self.only_due.toggled.connect(self.refresh)
        self.all_systems = QCheckBox("All systems")
        self.all_systems.setChecked(system_id is None)
        self.all_systems.setVisible(system_id is not None)
        self.all_systems.toggled.connect(self.refresh)
        top.addWidget(self.all_systems)
        top.addWidget(self.only_due)
        v.addLayout(top)
        self.sub = QLabel()
        self.sub.setObjectName("muted")
        self.sub.setWordWrap(True)
        v.addWidget(self.sub)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(self.COLS)
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.itemDoubleClicked.connect(lambda *_: self._edit())
        self.tree.itemSelectionChanged.connect(self._sel)
        h = self.tree.header()
        h.setStretchLastSection(True)
        h.resizeSection(0, 300)
        h.resizeSection(1, 150)
        h.resizeSection(2, 150)
        h.resizeSection(3, 50)
        h.resizeSection(4, 90)
        h.resizeSection(5, 90)
        v.addWidget(self.tree, 1)
        self.detail = QLabel()
        self.detail.setObjectName("muted")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.detail)
        bar = QHBoxLayout()

        def B(text, slot, ic=None, tip=""):
            b = QPushButton(text)
            if ic:
                b.setIcon(theme.icon(ic))
            b.setToolTip(tip)
            b.clicked.connect(slot)
            bar.addWidget(b)
            return b
        B("Add…", self._add, "add")
        self.b_edit = B("Edit…", self._edit, "edit")
        self.b_open = B("Open in browser", self._open, "view")
        self.b_run = B("Re-copy / check now", self._run_selected, "sync",
                       "Copy again (only what changed is updated), or check a saved link still works")
        self.b_due = B("Re-check all due", self._run_due, "refresh")
        self.b_gone = B("Mark as gone", self._toggle_gone, "warning",
                        "Gone links stay listed (with their copies) but stop counting as due")
        bar.addStretch()
        self.b_remove = B("Remove…", self._remove, "delete")
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        bar.addWidget(close)
        v.addLayout(bar)
        self.refresh()

    # ------------------------------------------------------------------ list
    def _links(self) -> list[Link]:
        sid = None if self.all_systems.isChecked() else self.system_id
        links = self.lib.links(sid)
        return [l for l in links if l.is_due()] if self.only_due.isChecked() else links

    def refresh(self):
        sel = {it.data(0, Qt.UserRole) for it in self.tree.selectedItems()}
        self.tree.clear()
        t = theme.T
        links = self._links()
        names = {s.id: s.name for s in self.lib.systems()}
        for l in links:
            st, col = status_text(l)
            due = l.next_due()
            it = QTreeWidgetItem([l.label, names.get(l.system_id, "?"), SHORT_MODES.get(l.mode, l.mode),
                                  str(len(self.lib.link_doc_ids(l.id))) if l.copies else "",
                                  (l.last_fetched or "")[:10], due.date().isoformat() if due else "-", st])
            it.setData(0, Qt.UserRole, l.id)
            it.setIcon(0, theme.icon("link", t["muted"]))
            it.setForeground(6, QColor(t.get(col, t["muted"])))
            tip = link_tooltip(self.lib, l)
            for c in range(len(self.COLS)):
                it.setToolTip(c, tip)
            if l.status == "gone":
                f = it.font(0); f.setStrikeOut(True); it.setFont(0, f)
            self.tree.addTopLevelItem(it)
            if l.id in sel:
                it.setSelected(True)
        all_links = self.lib.links(None if self.all_systems.isChecked() else self.system_id)
        n_due = sum(1 for l in all_links if l.is_due())
        n_gone = sum(1 for l in all_links if l.status == "gone")
        where = "" if self.all_systems.isChecked() or self.system_id is None else \
            f" - {names.get(self.system_id, '')}"
        self.heading.setText(f"Links{where}")
        self.sub.setText(f"{len(all_links)} link(s) · {n_due} due for a re-check · {n_gone} gone. "
                         "Copies are re-fetched politely (one page a second); only what changed is updated.")
        self.b_due.setEnabled(n_due > 0)
        self._sel()

    def selected(self) -> list[Link]:
        return [l for it in self.tree.selectedItems() if (l := self.lib.link(it.data(0, Qt.UserRole)))]

    def _sel(self):
        ls = self.selected()
        one = len(ls) == 1
        for b in (self.b_edit, self.b_open):
            b.setEnabled(one)
        for b in (self.b_run, self.b_remove, self.b_gone):
            b.setEnabled(bool(ls))
        self.b_gone.setText("Mark as working" if ls and all(l.status == "gone" for l in ls) else "Mark as gone")
        self.detail.setText(link_tooltip(self.lib, ls[0]).replace("\n", "  ·  ") if one else "")

    # ------------------------------------------------------------------ actions
    def _add(self):
        lid = save_link_dialog(self, self.lib, None, self.system_id)
        if lid:
            self.changed.emit()
            self.refresh()
            self.run_links.emit([lid])

    def _edit(self):
        ls = self.selected()
        if len(ls) != 1:
            return
        lid = save_link_dialog(self, self.lib, ls[0])
        self.changed.emit()
        self.refresh()
        if lid:
            self.run_links.emit([lid])

    def _open(self):
        ls = self.selected()
        if ls:
            QDesktopServices.openUrl(QUrl(ls[0].url))

    def _run_selected(self):
        ids = [l.id for l in self.selected()]
        if ids:
            self.run_links.emit(ids)

    def _run_due(self):
        ids = [l.id for l in self.lib.links(None if self.all_systems.isChecked() else self.system_id) if l.is_due()]
        if ids:
            self.run_links.emit(ids)

    def _toggle_gone(self):
        ls = self.selected()
        back = ls and all(l.status == "gone" for l in ls)
        for l in ls:
            if back:
                self.lib.update_link(l.id, status="", status_msg="", fail_count=0, first_failed=None)
            else:
                self.lib.update_link(l.id, status="gone", status_msg="Marked as gone by you.")
        self.changed.emit()
        self.refresh()
        if back:
            self.run_links.emit([l.id for l in ls])

    def _remove(self):
        ls = self.selected()
        deleted = []
        for l in ls:
            ids = self.lib.link_doc_ids(l.id)
            r = remove_link_dialog(self, self.lib, l)
            if r is None:
                break
            if r[1]:
                deleted += ids
        if deleted:
            self.docs_deleted.emit(deleted)
        self.changed.emit()
        self.refresh()
