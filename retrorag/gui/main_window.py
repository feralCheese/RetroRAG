"""Main window: library tree, document panel, search tester, proofreading editors, job log."""
from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence
from PySide6.QtWidgets import (QComboBox, QDockWidget, QFileDialog, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QTabWidget, QTextBrowser, QToolBar, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from .. import ingest
from ..platform_util import open_path, reveal, tesseract_install_command
from ..config import APP_NAME, APP_VERSION, CATEGORIES, AppSettings, is_portable, resolve_library_path, store_library_path
from ..library import Document, Library
from . import theme
from .dialogs import AddDialog, ConnectDialog, RemoveSystemDialog, SettingsDialog, SystemDialog
from .editor import ProofEditor
from .panels import DocPanel, SearchPanel
from .workers import JobRunner

ROLE = Qt.UserRole
KIND_ICON = {"pdf": "pdf", "image": "image", "text": "doc", "code": "code", "binary": "chip"}


def kind_icon(d: Document) -> str:
    if d.kind == "binary" and d.category == "Disk & Tape Images":
        return "floppy"
    return KIND_ICON.get(d.kind, "doc")


class LibraryTree(QTreeWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.setHeaderHidden(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QTreeWidget.DropOnly)
        self.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.setUniformRowHeights(True)
        self.setColumnCount(2)
        self.setIndentation(16)
        self.setAnimated(True)
        h = self.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, h.ResizeMode.Stretch)
        h.setSectionResizeMode(1, h.ResizeMode.ResizeToContents)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.toLocalFile()]
        it = self.itemAt(e.position().toPoint())
        self.win.add_paths(paths, it)
        e.acceptProposedAction()


class MainWindow(QMainWindow):
    def __init__(self, library: str | None = None):
        super().__init__()
        self.app_settings = AppSettings.load()
        lib_path = Path(library) if library else resolve_library_path(self.app_settings.library_path)
        self.lib = Library(lib_path)
        self.jobs = JobRunner()
        self.servers = []
        self.editors: dict[int, ProofEditor] = {}
        self._index_timer = QTimer(self, singleShot=True, interval=1500, timeout=self._auto_index_now)

        self.setWindowTitle(f"{APP_NAME} - {self.lib.root}")
        self.resize(1400, 880)
        self._build_ui()
        self._wire_jobs()
        self.refresh_all()
        if self.app_settings.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(self.app_settings.window_geometry.encode()))
        if self.app_settings.window_state:
            self.restoreState(QByteArray.fromBase64(self.app_settings.window_state.encode()))
        if self.app_settings.doc_split:
            self.doc_panel.split.setSizes(self.app_settings.doc_split)
        else:
            self.doc_panel.split.setSizes([480, 640])
        try:
            from PySide6.QtWidgets import QApplication
            QApplication.instance().styleHints().colorSchemeChanged.connect(
                lambda _s: self.app_settings.theme == "system" and self.set_theme("system"))
        except Exception:
            pass
        if self.app_settings.start_servers:
            QTimer.singleShot(500, self.toggle_servers)
        QTimer.singleShot(300, self._first_run_checks)

    # ================================================================== UI
    def _build_ui(self):
        # ---- left: tree
        left = QWidget()
        lv = QVBoxLayout(left); lv.setContentsMargins(8, 2, 4, 4); lv.setSpacing(6)
        fh = QHBoxLayout()
        self.filter = QLineEdit(); self.filter.setPlaceholderText("Filter documents…")
        self.filter.setClearButtonEnabled(True)
        self.filter_action = self.filter.addAction(theme.icon("filter"), QLineEdit.LeadingPosition)
        self.filter_kind = QComboBox()
        self.filter_kind.addItems(["All", "Needs OCR", "Flagged words", "Not proofread", "Out of date"])
        fh.addWidget(self.filter, 1); fh.addWidget(self.filter_kind)
        lv.addLayout(fh)
        self.tree = LibraryTree(self)
        lv.addWidget(self.tree, 1)
        self._filter_timer = QTimer(self, singleShot=True, interval=300, timeout=self.refresh_tree)
        self.filter.textChanged.connect(lambda: self._filter_timer.start())
        self.filter_kind.currentIndexChanged.connect(lambda: self.refresh_tree())
        self.tree.itemExpanded.connect(self._expand)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        dock = QDockWidget("Library", self); dock.setObjectName("library_dock")
        dock.setWidget(left); dock.setFeatures(QDockWidget.DockWidgetMovable)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

        # ---- center tabs
        self.tabs = QTabWidget(); self.tabs.setTabsClosable(True); self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.home = QTextBrowser(); self.home.setOpenLinks(False)
        self.home.anchorClicked.connect(self._home_link)
        self.doc_panel = DocPanel(self.lib)
        self.doc_panel.action.connect(self.doc_action)
        self.doc_panel.changed.connect(lambda _id: (self.refresh_tree(), self.schedule_index()))
        self.doc_panel.open_doc.connect(self.select_doc)
        self.doc_panel.links_changed.connect(lambda: (self.refresh_stats(), self.schedule_index()))
        self.search_panel = SearchPanel(self.lib)
        self.search_panel.open_doc.connect(self.open_editor)
        self.tabs.addTab(self.home, "Home")
        self.tabs.addTab(self.doc_panel, "Document")
        self.tabs.addTab(self.search_panel, "Search test")
        for i in range(3):
            self.tabs.tabBar().setTabButton(i, self.tabs.tabBar().ButtonPosition.RightSide, None)
        self.setCentralWidget(self.tabs)

        # ---- bottom: log
        bottom = QWidget(); bv = QVBoxLayout(bottom); bv.setContentsMargins(8, 2, 8, 6)
        ph = QHBoxLayout()
        self.job_label = QLabel("Idle")
        self.job_label.setObjectName("muted")
        self.progress = QProgressBar(); self.progress.setTextVisible(False); self.progress.setFixedWidth(260)
        self.progress.setRange(0, 1); self.progress.setValue(0); self.progress.hide()
        self.cancel_btn = QPushButton("Cancel"); self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.jobs.cancel)
        ph.addWidget(self.job_label, 1); ph.addWidget(self.progress, 2); ph.addWidget(self.cancel_btn)
        bv.addLayout(ph)
        self.log_view = QPlainTextEdit(); self.log_view.setReadOnly(True); self.log_view.setMaximumBlockCount(5000)
        self.log_view.setFont(theme.mono_font(9))
        self.log_view.setObjectName("log")
        bv.addWidget(self.log_view)
        ldock = QDockWidget("Activity", self); ldock.setObjectName("log_dock"); ldock.setWidget(bottom)
        ldock.setFeatures(QDockWidget.DockWidgetMovable)
        self.addDockWidget(Qt.BottomDockWidgetArea, ldock)
        self.resizeDocks([dock], [380], Qt.Horizontal)
        self.resizeDocks([ldock], [170], Qt.Vertical)

        # ---- actions
        self._action_icons: list = []

        def A(text, slot, shortcut=None, tip=None, ic=None):
            a = QAction(text, self); a.triggered.connect(slot)
            if ic:
                self._action_icons.append((a, ic))
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            if tip:
                a.setToolTip(tip); a.setStatusTip(tip)
            return a

        self.a_add_system = A("New system", self.add_system, "Ctrl+Shift+N", "Add a retro system", "add")
        self.a_add_files = A("Add files", self.add_files_dialog, "Ctrl+O", "Add documents, images, ROMs, disk images…", "doc")
        self.a_add_folder = A("Add folder", self.add_folder_dialog, None, "Add a whole folder, e.g. emulator source", "folder")
        self.a_ocr = A("Run OCR", self.ocr_selected_or_pending, "Ctrl+R",
                       "OCR the selected documents (or every document that needs it)", "scan")
        self.a_index = A("Build index", lambda: self.build_index(False), "F5", "Chunk + embed new/changed documents", "sync")
        self.a_servers = A("Start servers", self.toggle_servers, None, "Start REST API + MCP HTTP servers", "play")
        self.a_connect = A("Connect AI", self.show_connect, None, "How to connect Claude, Unsloth Studio, LM Studio…", "link")
        self.a_settings = A("Settings", self.show_settings, "Ctrl+,", None, "settings")

        tb = QToolBar("Main"); tb.setObjectName("main_toolbar"); tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        self.addToolBar(tb)
        for a in (self.a_add_system, self.a_add_files, self.a_add_folder, None, self.a_ocr, self.a_index, None,
                  self.a_servers, self.a_connect):
            tb.addSeparator() if a is None else tb.addAction(a)
        exp = QToolButton(); exp.setText("Export"); exp.setPopupMode(QToolButton.InstantPopup)
        exp.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.export_btn = exp
        em = QMenu(exp)
        em.addAction("Markdown corpus (for Claude Projects, Open WebUI, AnythingLLM…)", self.export_markdown)
        em.addAction("Chunks as JSONL", lambda: self.export_jsonl(False))
        em.addAction("Chunks as JSONL + embeddings", lambda: self.export_jsonl(True))
        exp.setMenu(em); tb.addWidget(exp)
        tb.addSeparator(); tb.addAction(self.a_settings)

        mb = self.menuBar()
        m = mb.addMenu("&File")
        m.addAction("Open library folder…", self.open_library)
        m.addAction("Import system folder…", self.import_system_folder_dialog)
        m.addAction("Copy linked files into library (make portable)", self.consolidate)
        from ..platform_util import file_manager_name
        m.addAction(f"Show library in {file_manager_name()}", lambda: open_path(self.lib.root))
        m.addSeparator(); m.addAction(self.a_settings)
        m.addSeparator(); m.addAction("Exit", self.close)
        m = mb.addMenu("&System")
        m.addAction(self.a_add_system)
        m.addAction("Edit selected system…", self.edit_system)
        m.addAction("Remove selected system…", self.remove_system)
        m = mb.addMenu("&Documents")
        for a in (self.a_add_files, self.a_add_folder, self.a_ocr):
            m.addAction(a)
        m.addAction("Force OCR on selected (all pages)", lambda: self.ocr_docs(self.selected_doc_ids(), force=True))
        m.addAction("Remove selected documents…", self.remove_selected_docs)
        m = mb.addMenu("&Index")
        m.addAction(self.a_index)
        m.addAction("Rebuild everything", lambda: self.build_index(True))
        m.addSeparator()
        m.addAction("Suggest links across library…", self.suggest_links_library)
        self.a_auto = QAction("Auto-index after changes", self, checkable=True, checked=self.app_settings.auto_index)
        self.a_auto.toggled.connect(lambda v: setattr(self.app_settings, "auto_index", v))
        m.addAction(self.a_auto)
        m = mb.addMenu("&AI")
        m.addAction(self.a_servers); m.addAction(self.a_connect)
        self.a_autostart = QAction("Start servers when RetroRAG opens", self, checkable=True,
                                   checked=self.app_settings.start_servers)
        self.a_autostart.toggled.connect(lambda v: setattr(self.app_settings, "start_servers", v))
        m.addAction(self.a_autostart)
        m = mb.addMenu("&View")
        tm = m.addMenu("Theme")
        from PySide6.QtGui import QActionGroup
        grp = QActionGroup(self)
        self._theme_actions = {}
        for key, label in (("system", "Follow Windows"), ("light", "Light"), ("dark", "Dark"),
                           ("retro", "Retro - phosphor CRT")):
            a = QAction(label, self, checkable=True, checked=self.app_settings.theme == key)
            a.triggered.connect(lambda _=False, k=key: self.set_theme(k))
            grp.addAction(a); tm.addAction(a)
            self._theme_actions[key] = a
        self.a_scanlines = QAction("Retro scan lines", self, checkable=True, checked=self.app_settings.retro_scanlines)
        self.a_scanlines.toggled.connect(lambda v: (setattr(self.app_settings, "retro_scanlines", v),
                                                    self.set_theme(self.app_settings.theme)))
        tm.addSeparator(); tm.addAction(self.a_scanlines)
        m = mb.addMenu("&Help")
        m.addAction("Getting started", lambda: self.tabs.setCurrentIndex(0))
        m.addAction("About", lambda: QMessageBox.about(self, APP_NAME, f"{APP_NAME} {APP_VERSION}\n"
                                                       "Retro computing documentation RAG builder."))
        self.server_label = QLabel("Servers: stopped")
        self.stats_label = QLabel()
        self.statusBar().addPermanentWidget(self.stats_label)
        self.statusBar().addPermanentWidget(self.server_label)
        self.apply_icons()

    def apply_icons(self):
        for a, ic in self._action_icons:
            a.setIcon(theme.icon(ic))
        self.export_btn.setIcon(theme.icon("export"))
        self.filter_action.setIcon(theme.icon("filter", theme.T["muted"]))
        self.doc_panel.apply_icons()
        self.doc_panel.preview.apply_icons()
        self.search_panel.apply_icons()
        self._update_server_action()
        for ed in self.editors.values():
            ed.apply_icons()

    def set_theme(self, mode: str):
        from PySide6.QtWidgets import QApplication
        s = self.app_settings
        s.theme = mode
        theme.apply_theme(QApplication.instance(), mode, s.retro_phosphor, s.retro_scanlines, s.retro_font_file)
        if mode in getattr(self, "_theme_actions", {}):
            self._theme_actions[mode].setChecked(True)
        self.apply_icons()
        self.log_view.setFont(theme.mono_font(9))
        self.refresh_tree()
        self.refresh_stats()
        self.doc_panel.preview.restyle()
        self.doc_panel.refresh_current()
        for ed in self.editors.values():
            ed.restyle()

    def _update_server_action(self):
        running = bool(self.servers)
        self.a_servers.setText("Stop servers" if running else "Start servers")
        self.a_servers.setIcon(theme.icon("stop" if running else "play", theme.T["danger"] if running else None))

    def _wire_jobs(self):
        self.jobs.log.connect(self.log)
        self.jobs.started.connect(self._on_job_started)
        self.jobs.progress.connect(self._on_progress)
        self.jobs.finished.connect(self._on_job_finished)

    def _on_job_started(self, name):
        self._job_name = name
        self.job_label.setText(f"{name}…")
        self.cancel_btn.setEnabled(True)
        self.progress.show()
        self.progress.setRange(0, 0)

    def _on_progress(self, i, n, msg):
        if n > 0:
            self.progress.setRange(0, n); self.progress.setValue(i)
        name = getattr(self, "_job_name", "")
        self.job_label.setText(f"{name}  ·  {i}/{n}  {msg[:70]}" if n else f"{name}  {msg[:70]}")

    def _on_job_finished(self, name, result, err):
        if not self.jobs.busy and self.jobs._q.empty():
            self.job_label.setText("Idle" if not err else f"⚠ {name}: {err[:80]}")
            self.cancel_btn.setEnabled(False)
            self.progress.setRange(0, 1); self.progress.setValue(1 if not err else 0); self.progress.hide()
        self.refresh_stats()

    def log(self, msg: str):
        self.log_view.appendPlainText(msg)

    # ================================================================== refresh
    def refresh_all(self):
        self.refresh_tree()
        self.refresh_stats()
        self.search_panel.lib = self.lib
        self.search_panel.refresh_filters()
        self.doc_panel.lib = self.lib
        self.doc_panel.preview.lib = self.lib
        self.doc_panel.refresh_current()

    def refresh_stats(self):
        st = self.lib.stats()
        self.stats_label.setText(f"{st['systems']} systems · {st['documents']} docs · {st['chunks']} chunks · "
                                 f"{st['needs_ocr']} need OCR · {st['stale']} out of date")
        self._render_home(st)

    def _render_home(self, st):
        from ..ocr import tesseract_info
        if not hasattr(self, "_tess"):
            self._tess = tesseract_info(self.lib.settings.tesseract_path or self.app_settings.tesseract_path)
        tinfo = self._tess
        t = theme.T
        ok = lambda msg: f"<span style='color:{t['ok']}'>●</span>&nbsp; {msg}"
        bad = lambda msg: f"<span style='color:{t['warn']}'>●</span>&nbsp; {msg}"
        tess = (ok(f"Tesseract {tinfo['version']} &nbsp;<span style='color:{t['muted']}'>"
                   f"({', '.join(tinfo['languages'][:8])})</span>") if tinfo["ok"] else
                bad(f"Tesseract not found - install with <code>{tesseract_install_command()}</code>, then restart"))
        es = self.lib.settings
        emb_txt = f"{es.embed_provider} / {es.embed_model}" if es.embed_provider != "none" else "off - keyword search only"
        emb = (ok if es.embed_provider != "none" else bad)(
            html.escape(emb_txt) + f" &nbsp;<span style='color:{t['muted']}'>indexed with: "
            f"{html.escape(st['embed_signature'] or 'nothing yet')}</span>")
        srv = (ok("REST + MCP servers running") if self.servers else
               bad("AI servers stopped - <b>Start servers</b> for Unsloth Studio and other HTTP clients"))
        mode = "Portable" if is_portable() else "Installed"

        def tile(n, label, col=None, goto=None):
            num = (f"<span style='font-size:{t['big']};font-weight:{t['bold']};color:{col or t['text']}'>{n}</span>")
            lab = f"<span style='color:{t['muted']}'>{label}</span>"
            if goto and n:   # clickable: jump to the first document behind this number
                return (f"<td width='16%' style='background:{t['surface']};padding:14px 16px'>"
                        f"<a href='goto:{goto}' style='text-decoration:none'>{num}<br>{lab}<br>"
                        f"<span style='color:{t['accent']}'>fix ›</span></a></td>")
            return f"<td width='16%' style='background:{t['surface']};padding:14px 16px'>{num}<br>{lab}</td>"
        tiles = "".join([tile(st["systems"], "systems"), tile(st["documents"], "documents"),
                         tile(st["chunks"], f"chunks · {st['embedded']} embedded"),
                         tile(st["needs_ocr"], "need OCR", t["warn"] if st["needs_ocr"] else None, "needs_ocr"),
                         tile(st["flagged_docs"], "with flagged words", t["warn"] if st["flagged_docs"] else None,
                              "flagged"),
                         tile(st["stale"], "out of date", t["warn"] if st["stale"] else None, "stale")])

        def step(n, title, body):
            return (f"<tr><td valign='top' style='padding:6px 12px 6px 0'><span style='background:{t['accent_soft']};"
                    f"color:{t['accent']};font-weight:{t['bold']}'>&nbsp;{n}&nbsp;</span></td>"
                    f"<td style='padding:6px 0'><b>{title}</b><br><span style='color:{t['muted']}'>{body}</span></td></tr>")
        steps = "".join([
            step(1, "New system", "Add a machine, e.g. <i>Commodore 64</i> with aliases <i>C64, CBM 64</i>."),
            step(2, "Add files or folders", "Or drag &amp; drop onto a system or category: manuals, programming "
                 "references, schematics, pinouts, datasheets (PDF, GIF, JPG, PNG, TIFF, TXT, HTML), emulator source "
                 "trees, ROMs/firmware, disk &amp; tape images (D64, ADF, TAP, TZX, DSK…). Zips are unpacked."),
            step(3, "Run OCR", "Words Tesseract is unsure about are written as <code>{?w0rd|42?}</code>."),
            step(4, "Proofread", "Double-click a document. <b>F3</b> selects the next flagged word - type the fix - "
                 "<b>Ctrl+Enter</b> accepts it and moves on. The scan highlights the line &amp; word you're on."),
            step(5, "Build index (F5)", "Chunks and embeds everything new or changed. Try queries in <b>Search test</b>."),
            step(6, "Connect AI", "Claude Desktop / Claude Code (MCP), Unsloth Studio &amp; other MCP clients (HTTP), "
                 "REST/OpenAPI.")])
        self.home.setHtml(f"""
<div style='color:{t['text']}'>
<p style='font-size:{t['h1']};font-weight:{t['bold']};margin:4px 0 0 0'>{APP_NAME}</p>
<p style='color:{t['muted']};margin-top:2px'>{html.escape(str(self.lib.root))} &nbsp;·&nbsp; {mode}</p>
<table width='100%' cellspacing=8 cellpadding=0><tr>{tiles}</tr></table>
<table cellspacing=0 cellpadding=3 style='margin-top:6px'>
<tr><td style='color:{t['muted']};padding-right:14px'>OCR</td><td>{tess}</td></tr>
<tr><td style='color:{t['muted']};padding-right:14px'>Embeddings</td><td>{emb}</td></tr>
<tr><td style='color:{t['muted']};padding-right:14px'>AI access</td><td>{srv}</td></tr></table>
<p style='font-size:{t['h2']};font-weight:{t['bold']};margin-top:18px'>Getting started</p>
<table cellspacing=0 cellpadding=0>{steps}</table>
<p style='color:{t['muted']};margin-top:14px'>Tip: for schematics and pinouts, re-OCR a page in the editor with
layout mode <i>11 - Sparse text</i>, and use the Notes field - notes and tags are searchable even when OCR can't
read a drawing.</p>
</div>""")

    # ------------------------------------------------------------------ home page shortcuts
    GOTO = {  # home tile -> (tree filter, what to tell the user)
        "needs_ocr": ("Needs OCR", "{n} document(s) need OCR - Run OCR (Ctrl+R) does them all, or pick one in the tree."),
        "flagged": ("Flagged words", "{n} document(s) have flagged words - F3 jumps to the next one, type the fix, "
                                     "Ctrl+Enter accepts it."),
        "stale": ("Out of date", "{n} document(s) changed since the last index build - Build index (F5) updates them."),
    }

    def _home_link(self, url):
        s = url.toString()
        if s.startswith("goto:"):
            self.goto_problem(s[5:])
        else:
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(url)

    def goto_problem(self, kind: str) -> int | None:
        """Filter the tree to the documents behind a Home-page number and open the first one, ready to fix."""
        filt, msg = self.GOTO[kind]
        self.filter.blockSignals(True); self.filter.clear(); self.filter.blockSignals(False)
        if self.filter_kind.currentText() != filt:
            self.filter_kind.setCurrentText(filt)      # triggers refresh_tree
        else:
            self.refresh_tree()
        docs = [it for it in self._iter_items() if (k := it.data(0, ROLE)) and k[0] == "doc"]
        if not docs:
            self.statusBar().showMessage("Nothing to fix there any more.", 5000)
            return None
        first = docs[0]
        doc_id = first.data(0, ROLE)[1]
        self.tree.setCurrentItem(first)
        self.tree.scrollToItem(first)
        self.doc_panel.show_doc(self.lib.document(doc_id))
        self.tabs.setCurrentWidget(self.doc_panel)
        if kind == "flagged":
            self.open_editor(doc_id)
            ed = self.editors.get(doc_id)
            if ed:
                QTimer.singleShot(120, ed.next_flag)
        self.statusBar().showMessage(msg.format(n=len(docs)) + "  (Set the filter back to 'All' to see everything.)",
                                     15000)
        return doc_id

    def refresh_tree(self):
        expanded = self._expanded_keys()
        sel = self._selected_key()
        self.tree.clear()
        flt = self.filter.text().strip().lower()
        mode = self.filter_kind.currentText()
        c = self.lib.conn()
        for s in self.lib.systems():
            counts = {r[0]: r[1] for r in c.execute(
                "SELECT category, COUNT(*) FROM documents WHERE system_id=? GROUP BY category", (s.id,))}
            si = QTreeWidgetItem([s.name, str(sum(counts.values()))])
            si.setData(0, ROLE, ("system", s.id))
            si.setIcon(0, theme.icon("system", theme.T["accent"]))
            si.setForeground(1, QColor(theme.T["muted"]))
            f = si.font(0); f.setBold(True); si.setFont(0, f)
            si.setToolTip(0, ", ".join(x for x in (s.manufacturer, s.year, s.cpu, s.aliases) if x))
            self.tree.addTopLevelItem(si)
            cats = [cat for cat in CATEGORIES if cat in counts] + sorted(set(counts) - set(CATEGORIES))
            for cat in cats:
                ci = QTreeWidgetItem([cat, str(counts[cat])])
                ci.setData(0, ROLE, ("category", s.id, cat))
                ci.setIcon(0, theme.icon("folder", theme.T["accent"]))
                ci.setForeground(1, QColor(theme.T["muted"]))
                si.addChild(ci)
                if flt or mode != "All":
                    self._populate_category(ci, flt, mode)
                    if ci.childCount() == 0:
                        si.removeChild(ci)
                    else:
                        ci.setExpanded(True)
                else:
                    ci.addChild(QTreeWidgetItem(["…"]))   # lazy placeholder
            if (flt or mode != "All") and si.childCount() == 0:
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(si))
                continue
            if (flt or mode != "All") or ("system", s.id) in expanded:
                si.setExpanded(True)
        # restore expansions
        it = self._iter_items()
        for item in it:
            key = item.data(0, ROLE)
            if key and key in expanded and not item.isExpanded():
                item.setExpanded(True)
            if key and key == sel:
                self.tree.setCurrentItem(item)

    def _iter_items(self):
        out = []

        def walk(it):
            for i in range(it.childCount()):
                ch = it.child(i)
                out.append(ch)
                walk(ch)
        for i in range(self.tree.topLevelItemCount()):
            t = self.tree.topLevelItem(i)
            out.append(t)
            walk(t)
        return out

    def _expanded_keys(self) -> set:
        return {tuple(it.data(0, ROLE)) for it in self._iter_items() if it.isExpanded() and it.data(0, ROLE)}

    def _selected_key(self):
        it = self.tree.currentItem()
        return tuple(it.data(0, ROLE)) if it and it.data(0, ROLE) else None

    def _expand(self, item: QTreeWidgetItem):
        key = item.data(0, ROLE)
        if key and key[0] == "category" and item.childCount() == 1 and item.child(0).data(0, ROLE) is None:
            item.takeChildren()
            self._populate_category(item, "", "All")

    def _populate_category(self, ci: QTreeWidgetItem, flt: str, mode: str):
        _, sid, cat = ci.data(0, ROLE)
        where, params = "", []
        if mode == "Needs OCR":
            where = "needs_ocr=1"
        elif mode == "Flagged words":
            where = "flagged>0"
        elif mode == "Not proofread":
            where = "text_status='ocr'"
        elif mode == "Out of date":
            where = "(indexed IS NULL OR text_updated > indexed)"
        docs = self.lib.documents(sid, cat, where=where, params=params)
        if flt:
            docs = [d for d in docs if flt in d.title.lower() or flt in d.group_path.lower() or flt in d.tags.lower()]
        folders: dict[str, QTreeWidgetItem] = {}

        def folder_item(path: str) -> QTreeWidgetItem:
            if not path:
                return ci
            if path in folders:
                return folders[path]
            parent_path, _, name = path.rpartition("/")
            parent = folder_item(parent_path)
            fi = QTreeWidgetItem([name])
            fi.setData(0, ROLE, ("folder", sid, cat, path))
            fi.setIcon(0, theme.icon("folder", theme.T["muted"]))
            parent.addChild(fi)
            folders[path] = fi
            return fi

        for d in docs[:20000]:
            parent = folder_item(d.group_path)
            parent.addChild(self._doc_item(d))

    def _doc_item(self, d: Document) -> QTreeWidgetItem:
        t = theme.T
        status, col = "", t["muted"]
        if d.needs_ocr:
            status, col = "OCR needed", t["warn"]
        elif d.flagged:
            status, col = f"⚑ {d.flagged}", t["warn"]
        elif d.text_status == "reviewed":
            status, col = "✓", t["ok"]
        if d.is_stale and not status:
            status = "•"
        it = QTreeWidgetItem([d.title, status])
        it.setData(0, ROLE, ("doc", d.id))
        it.setIcon(0, theme.icon(kind_icon(d), t["muted"]))
        it.setForeground(1, QColor(col))
        it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        if d.is_stale:
            f = it.font(0); f.setItalic(True); it.setFont(0, f)
        tip = f"{Path(d.path).name}\n{d.kind} · {d.text_status}" + ("\n(not indexed yet)" if d.is_stale else "")
        it.setToolTip(0, tip); it.setToolTip(1, tip)
        return it

    # ================================================================== selection helpers
    def selected_doc_ids(self) -> list[int]:
        ids = []
        for it in self.tree.selectedItems():
            key = it.data(0, ROLE)
            if not key:
                continue
            if key[0] == "doc":
                ids.append(key[1])
            elif key[0] == "category":
                ids += [d.id for d in self.lib.documents(key[1], key[2])]
            elif key[0] == "folder":
                ids += [d.id for d in self.lib.documents(key[1], key[2], where="(group_path=? OR group_path LIKE ?)",
                                                         params=(key[3], key[3] + "/%"))]
            elif key[0] == "system":
                ids += [d.id for d in self.lib.documents(key[1])]
        return list(dict.fromkeys(ids))

    def current_system(self):
        it = self.tree.currentItem()
        while it is not None:
            key = it.data(0, ROLE)
            if key and key[0] == "system":
                return self.lib.system(key[1])
            if key and key[0] in ("category", "folder"):
                return self.lib.system(key[1])
            if key and key[0] == "doc":
                d = self.lib.document(key[1])
                return self.lib.system(d.system_id) if d else None
            it = it.parent()
        return None

    def _selection_changed(self):
        it = self.tree.currentItem()
        key = it.data(0, ROLE) if it else None
        if key and key[0] == "doc":
            self.doc_panel.show_doc(self.lib.document(key[1]))
            if self.tabs.currentWidget() is self.home:
                self.tabs.setCurrentWidget(self.doc_panel)
        elif key:
            # system / category / folder (e.g. an emulator source tree): no single document -> no preview
            self.doc_panel.show_doc(None)

    def _double_clicked(self, it, _col):
        key = it.data(0, ROLE)
        if key and key[0] == "doc":
            self.open_editor(key[1])
        elif key and key[0] == "system":
            self.edit_system()

    def _context_menu(self, pos):
        it = self.tree.itemAt(pos)
        key = it.data(0, ROLE) if it else None
        m = QMenu(self)
        if not key:
            m.addAction(self.a_add_system)
        elif key[0] == "system":
            m.addAction("Add files…", self.add_files_dialog)
            m.addAction("Add folder…", self.add_folder_dialog)
            m.addAction("Edit system…", self.edit_system)
            m.addSeparator()
            m.addAction("OCR everything that needs it", lambda: self.ocr_docs(
                [d.id for d in self.lib.documents(key[1], where="needs_ocr=1")]))
            m.addAction("Show folder", lambda: open_path(self.lib.system_dir(self.lib.system(key[1]))))
            m.addSeparator()
            m.addAction("Remove system…", self.remove_system)
        else:
            if key[0] == "doc":
                m.addAction("Open / proofread", lambda: self.open_editor(key[1]))
            m.addAction("Run OCR", lambda: self.ocr_docs(self.selected_doc_ids()))
            m.addAction("Force OCR (all pages)", lambda: self.ocr_docs(self.selected_doc_ids(), force=True))
            m.addAction("Index now", lambda: self.build_index(False, self.selected_doc_ids()))
            if key[0] in ("category", "folder"):
                m.addAction("Add files here…", self.add_files_dialog)
            if key[0] == "doc":
                m.addAction("Show in folder", lambda: self.doc_action("show_folder", key[1]))
            m.addSeparator()
            m.addAction("Remove…", self.remove_selected_docs)
        m.exec(self.tree.viewport().mapToGlobal(pos))

    # ================================================================== systems
    def add_system(self):
        dlg = SystemDialog(self)
        if not dlg.exec():
            return
        v = dlg.values()
        if self.lib.find_system(v["name"]):
            QMessageBox.warning(self, "Exists", f"A system called '{v['name']}' already exists.")
            return
        s = self.lib.add_system(**v)
        self.log(f"Added system {s.name}")
        self.refresh_all()
        # files kept from an earlier removal (or copied from another PC)?
        sdir = self.lib.system_dir(s)
        if any(p.is_file() and p.name != ingest.MANIFEST for p in sdir.rglob("*")):
            if QMessageBox.question(self, "Existing files found",
                                    f"The folder for {s.name} already contains files (kept from an earlier removal, or "
                                    f"copied from another PC):\n{sdir}\n\nImport them now? Existing OCR text and "
                                    "corrections will be reused.") == QMessageBox.Yes:
                self._run_import(s, sdir)

    def edit_system(self):
        s = self.current_system()
        if not s:
            return
        dlg = SystemDialog(self, s)
        if dlg.exec():
            self.lib.update_system(s.id, **dlg.values())
            self.refresh_all()
            self.schedule_index()

    def remove_system(self):
        s = self.current_system()
        if not s:
            QMessageBox.information(self, "Remove system", "Select a system in the library tree first.")
            return
        n = len(self.lib.documents(s.id))
        dlg = RemoveSystemDialog(self, s, n, self.lib.system_dir(s))
        if not dlg.exec():
            return
        for did in [d.id for d in self.lib.documents(s.id)]:
            self._close_editor(did, force=True)
        msg = self.lib.delete_system(s.id, delete_files=dlg.delete_files)
        self.log(msg)
        self.doc_panel.show_doc(None)
        self.refresh_all()

    def import_system_folder_dialog(self):
        d = QFileDialog.getExistingDirectory(self, "Import a RetroRAG system folder (e.g. copied from another PC)")
        if not d:
            return
        folder = Path(d)
        man = ingest.read_manifest(folder).get("system", {})
        name = man.get("name") or folder.name.replace("-", " ").title()
        name, ok = QInputDialog.getText(self, "Import system folder", "System name:", text=name)
        if not ok or not name.strip():
            return
        s = self.lib.find_system(name) or self.lib.add_system(name.strip(), **{k: v for k, v in man.items() if k != "name"})
        self._run_import(s, folder)

    def _run_import(self, s, folder: Path):
        def job(ctx):
            return ingest.import_system_folder(self.lib, s, folder, progress=ctx.progress, cancel=ctx.cancelled, log=ctx.log)
        self.jobs.submit(f"Import {s.name}", job, on_done=lambda ids: (self.refresh_all(), self.schedule_index()))

    def consolidate(self):
        if QMessageBox.question(self, "Make portable", "Copy every linked-in-place file into the library folder?") \
                == QMessageBox.Yes:
            self.jobs.submit("Copy linked files", lambda ctx: ingest.consolidate_linked(self.lib, ctx.log),
                             on_done=lambda n: self.refresh_all())

    # ================================================================== adding
    def _ensure_system(self):
        if not self.lib.systems():
            QMessageBox.information(self, "Add a system first", "Add a retro system (e.g. 'Commodore 64') first.")
            self.add_system()
        return bool(self.lib.systems())

    def add_files_dialog(self):
        if not self._ensure_system():
            return
        files, _ = QFileDialog.getOpenFileNames(self, "Add files")
        if files:
            self.add_paths([Path(f) for f in files], self.tree.currentItem())

    def add_folder_dialog(self):
        if not self._ensure_system():
            return
        d = QFileDialog.getExistingDirectory(self, "Add folder (e.g. emulator or firmware source tree)")
        if d:
            self.add_paths([Path(d)], self.tree.currentItem())

    def add_paths(self, paths: list[Path], item: QTreeWidgetItem | None = None):
        if not paths or not self._ensure_system():
            return
        key = item.data(0, ROLE) if item else None
        system = self.current_system() if item is None else None
        category = None
        if key:
            if key[0] in ("system", "category", "folder"):
                system = self.lib.system(key[1])
            if key[0] in ("category", "folder"):
                category = key[2]
            if key[0] == "doc":
                d = self.lib.document(key[1])
                system, category = self.lib.system(d.system_id), d.category
        folders = [p for p in paths if p.is_dir()]
        files = [p for p in paths if p.is_file()]
        if files:
            dlg = AddDialog(self, self.lib.systems(), system, files, folder=False)
            if category:
                dlg.category.setCurrentText(category)
            if dlg.exec():
                v = dlg.values()
                s = self.lib.system(v["system_id"])
                self.jobs.submit(f"Add {len(files)} file(s) to {s.name}", lambda ctx: ingest.add_files(
                    self.lib, s, v["category"], files, copy=v["copy"], progress=ctx.progress, cancel=ctx.cancelled,
                    log=ctx.log), on_done=self._after_add)
        for folder in folders:
            dlg = AddDialog(self, self.lib.systems(), system, [folder], folder=True)
            if category:
                dlg.category.setCurrentText(category)
            if dlg.exec():
                v = dlg.values()
                s = self.lib.system(v["system_id"])
                self.jobs.submit(f"Add folder {folder.name}", lambda ctx, folder=folder, v=v, s=s: ingest.add_folder(
                    self.lib, s, v["category"], folder, copy=v["copy"], kinds=v["kinds"], progress=ctx.progress,
                    cancel=ctx.cancelled, log=ctx.log), on_done=self._after_add)

    def _after_add(self, ids):
        self.log(f"Added {len(ids or [])} document(s).")
        self.refresh_all()
        if ids:
            pending = [i for i in ids if (d := self.lib.document(i)) and d.needs_ocr]
            if pending:
                self.log(f"{len(pending)} of them need OCR - select them and click 'Run OCR' (or right-click the system).")
            self.schedule_index()

    # ================================================================== OCR
    def _tesseract_ok(self) -> bool:
        from ..ocr import tesseract_info
        self._tess = tesseract_info(self.lib.settings.tesseract_path or self.app_settings.tesseract_path)
        if not self._tess["ok"]:
            QMessageBox.warning(self, "Tesseract needed", self._tess["message"])
            return False
        return True

    def ocr_selected_or_pending(self):
        ids = [i for i in self.selected_doc_ids() if (d := self.lib.document(i)) and d.kind in ("pdf", "image")]
        if not ids:
            ids = [d.id for d in self.lib.documents(where="needs_ocr=1")]
            if not ids:
                QMessageBox.information(self, "OCR", "Nothing needs OCR. Select documents to OCR them anyway.")
                return
            if QMessageBox.question(self, "OCR", f"OCR all {len(ids)} document(s) that need it?") != QMessageBox.Yes:
                return
        self.ocr_docs(ids)

    def ocr_docs(self, ids: list[int], force: bool = False, pages: list[int] | None = None, psm: int | None = None,
                 on_done=None, cleanup: str | None = None):
        from ..ocr import has_manual_edits, ocr_document
        if not self._tesseract_ok():
            return
        docs = [d for d in (self.lib.document(i) for i in ids) if d and d.kind in ("pdf", "image")]
        if not docs:
            return
        edited = [d for d in docs if has_manual_edits(self.lib, d) and pages is None]
        if edited:
            names = "\n".join(d.title for d in edited[:10])
            r = QMessageBox.question(self, "Overwrite corrections?",
                                     f"These documents have been edited/proofread since OCR:\n{names}\n\n"
                                     "Re-running OCR replaces their text. Skip them?",
                                     QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                return
            if r == QMessageBox.Yes:
                docs = [d for d in docs if d not in edited]
        for d in docs:
            self._close_editor(d.id) if pages is None else None
        tess = self.app_settings.tesseract_path

        def job(ctx):
            out = []
            for d in docs:
                if ctx.cancelled():
                    break
                out.append(ocr_document(self.lib, self.lib.document(d.id), pages=pages, force=force, psm=psm, cleanup=cleanup,
                                        progress=ctx.progress, cancel=ctx.cancelled, log=ctx.log, app_tesseract=tess))
            return out

        def done(res):
            self.refresh_all()
            self.schedule_index()
            if on_done:
                on_done(res)
        self.jobs.submit(f"OCR {len(docs)} document(s)" if not pages else f"Re-OCR page {pages[0]}", job, on_done=done)

    # ================================================================== index
    def schedule_index(self):
        if self.app_settings.auto_index:
            self._index_timer.start()

    def _auto_index_now(self):
        if self.jobs.busy:
            self._index_timer.start(3000)
            return
        if self.lib.stats()["stale"]:
            self.build_index(False)

    def build_index(self, rebuild: bool, doc_ids: list[int] | None = None):
        from ..indexer import build_index

        def job(ctx):
            return build_index(self.lib, doc_ids, rebuild_all=rebuild, progress=ctx.progress, cancel=ctx.cancelled,
                               log=ctx.log)

        def done(res):
            self.refresh_all()
            if res and res.get("error") and not getattr(self, "_warned_embed", False):
                self._warned_embed = True
                QMessageBox.warning(self, "Embeddings unavailable", res["error"] +
                                    "\n\nKeyword search works meanwhile. Change the embedding provider in Settings, "
                                    "then press F5.")
        self.jobs.submit("Rebuild index" if rebuild else "Build index", job, on_done=done)

    # ================================================================== links
    def suggest_links_library(self):
        from ..relations import suggest

        def job(ctx):
            return suggest(self.lib, None, limit=300, log=ctx.log)

        def done(sugs):
            from .links import SuggestionsDialog
            dlg = SuggestionsDialog(self, self.lib, sugs or [], "Suggested links across the library")
            dlg.exec()
            if dlg.changed:
                self.doc_panel.refresh_current()
                self.refresh_stats()
                self.schedule_index()
        self.jobs.submit("Suggest links", job, on_done=done)

    def select_doc(self, doc_id: int):
        """Show a document in the Document tab and highlight it in the tree."""
        d = self.lib.document(doc_id)
        if not d:
            return
        if self.filter.text() or self.filter_kind.currentText() != "All":
            self.filter.clear(); self.filter_kind.setCurrentIndex(0)
        for it in self._iter_items():
            k = it.data(0, ROLE)
            if k and k[0] == "system" and k[1] == d.system_id:
                it.setExpanded(True)
        for it in self._iter_items():
            k = it.data(0, ROLE)
            if k and k[0] == "category" and k[1] == d.system_id and k[2] == d.category:
                it.setExpanded(True)
        target = None
        for it in self._iter_items():
            k = it.data(0, ROLE)
            if k and k[0] == "folder" and k[1] == d.system_id and k[2] == d.category and d.group_path and \
                    (d.group_path == k[3] or d.group_path.startswith(k[3] + "/")):
                it.setExpanded(True)
        for it in self._iter_items():
            k = it.data(0, ROLE)
            if k and k[0] == "doc" and k[1] == doc_id:
                target = it
                break
        if target:
            self.tree.setCurrentItem(target)
            self.tree.scrollToItem(target)
        self.doc_panel.show_doc(d)
        self.tabs.setCurrentWidget(self.doc_panel)

    # ================================================================== documents
    def doc_action(self, action: str, doc_id: int):
        d = self.lib.document(doc_id)
        if not d:
            return
        if action == "open_editor":
            self.open_editor(doc_id)
        elif action == "ocr":
            self.ocr_docs([doc_id])
        elif action == "ocr_force":
            self.ocr_docs([doc_id], force=True)
        elif action == "reextract":
            if d.text_status in ("ocr", "reviewed") and QMessageBox.question(
                    self, "Re-extract", "This replaces the OCR/corrected text with the file's own text layer. Continue?") \
                    != QMessageBox.Yes:
                return
            self._close_editor(doc_id)
            ingest.extract(self.lib, d, log=self.log)
            self.refresh_all(); self.doc_panel.show_doc(self.lib.document(doc_id)); self.schedule_index()
        elif action == "index":
            self.build_index(False, [doc_id])
        elif action == "open_original":
            open_path(self.lib.abspath(d.path))
        elif action == "show_folder":
            reveal(self.lib.abspath(d.path))
        elif action == "remove":
            self.remove_docs([doc_id])

    def remove_selected_docs(self):
        ids = self.selected_doc_ids()
        if ids:
            self.remove_docs(ids)

    def remove_docs(self, ids: list[int]):
        box = QMessageBox(self)
        box.setWindowTitle("Remove documents")
        box.setText(f"Remove {len(ids)} document(s) from the library?")
        keep = box.addButton("Remove, keep files", QMessageBox.AcceptRole)
        delete = box.addButton("Remove and delete files", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() not in (keep, delete):
            return
        for i in ids:
            self._close_editor(i, force=True)
            self.lib.delete_document(i, delete_files=box.clickedButton() is delete)
        self.log(f"Removed {len(ids)} document(s).")
        self.doc_panel.show_doc(None)
        self.refresh_all()

    def open_editor(self, doc_id: int, page: int = 0):
        d = self.lib.document(doc_id)
        if not d:
            return
        if doc_id in self.editors:
            ed = self.editors[doc_id]
        else:
            ed = ProofEditor(self.lib, d, self.app_settings.editor_font_size)
            ed.saved.connect(lambda i: (self.refresh_tree(), self.schedule_index(), self.refresh_stats()))
            ed.request_reocr.connect(self._reocr_page)
            self.editors[doc_id] = ed
            self.tabs.addTab(ed, (d.title[:28] + "…") if len(d.title) > 28 else d.title)
        self.tabs.setCurrentWidget(ed)
        if page:
            QTimer.singleShot(50, lambda: ed.goto_page(page))

    def _reocr_page(self, doc_id: int, page: int, psm: int, cleanup: str):
        ed = self.editors.get(doc_id)
        self.ocr_docs([doc_id], pages=[page], psm=psm, cleanup=cleanup, on_done=lambda _r: ed and ed.reload_after_ocr())

    def _close_editor(self, doc_id: int, force: bool = False) -> bool:
        ed = self.editors.get(doc_id)
        if not ed:
            return True
        if ed.is_dirty() and not force:
            r = QMessageBox.question(self, "Unsaved changes", f"Save changes to '{ed.doc.title}'?",
                                     QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                return False
            if r == QMessageBox.Save:
                ed.save()
        self.tabs.removeTab(self.tabs.indexOf(ed))
        del self.editors[doc_id]
        ed.deleteLater()
        return True

    def _close_tab(self, index: int):
        w = self.tabs.widget(index)
        if isinstance(w, ProofEditor):
            self._close_editor(w.doc.id)

    # ================================================================== servers / AI
    def toggle_servers(self):
        from ..api_server import start_servers
        if self.servers:
            for s in self.servers:
                s.stop()
            self.servers = []
            self._update_server_action()
            self.server_label.setText("Servers: stopped")
            self.log("Servers stopped.")
            return
        st = self.lib.settings
        try:
            self.servers = start_servers(self.lib.root, st.api_host, st.api_port, st.mcp_port, st.api_token)
        except Exception as e:
            QMessageBox.warning(self, "Servers", f"Could not start servers: {e}")
            return
        QTimer.singleShot(1200, self._check_servers)
        self._update_server_action()
        host = "127.0.0.1" if st.api_host == "0.0.0.0" else st.api_host
        self.server_label.setText(f"REST :{st.api_port} · MCP :{st.mcp_port}" + (" (LAN)" if st.api_host == "0.0.0.0" else ""))
        self.log(f"REST API  http://{host}:{st.api_port}/docs\nMCP HTTP  http://{host}:{st.mcp_port}/mcp")

    def _check_servers(self):
        dead = [s for s in self.servers if not s.running]
        if dead:
            ports = ", ".join(str(s.port) for s in dead)
            self.log(f"Server on port {ports} failed to start (port already in use?). Change ports in Settings → Servers.")
            self.server_label.setText(f"Servers: error on {ports}")

    def show_connect(self):
        ConnectDialog(self, self.lib, bool(self.servers)).exec()

    def show_settings(self):
        dlg = SettingsDialog(self, self.app_settings, self.lib)
        if not dlg.exec():
            return
        chunk_changed, new_lib = dlg.apply()
        self._tess = None
        del self._tess
        self.app_settings.save()
        self.set_theme(self.app_settings.theme)
        if new_lib and Path(new_lib).resolve() != self.lib.root:
            self.switch_library(Path(new_lib))
            return
        self.refresh_all()
        if chunk_changed and QMessageBox.question(self, "Re-index", "Chunk settings changed. Rebuild the index now?") \
                == QMessageBox.Yes:
            self.build_index(True)

    def open_library(self):
        d = QFileDialog.getExistingDirectory(self, "Open or create a library folder", str(self.lib.root.parent))
        if d:
            self.switch_library(Path(d))

    def switch_library(self, path: Path):
        for did in list(self.editors):
            if not self._close_editor(did):
                return
        if self.servers:
            self.toggle_servers()
        self.lib.close()
        self.lib = Library(path)
        self.app_settings.library_path = store_library_path(path)
        self.app_settings.save()
        self.setWindowTitle(f"{APP_NAME} - {self.lib.root}")
        self.doc_panel.show_doc(None)
        self.refresh_all()
        self.log(f"Opened library {path}")

    # ================================================================== export
    def export_markdown(self):
        d = QFileDialog.getExistingDirectory(self, "Export markdown corpus to folder")
        if d:
            from ..export import export_markdown
            self.jobs.submit("Export markdown", lambda ctx: export_markdown(self.lib, Path(d), log=ctx.log),
                             on_done=lambda n: open_path(d))

    def export_jsonl(self, emb: bool):
        f, _ = QFileDialog.getSaveFileName(self, "Export chunks", "retrorag_chunks.jsonl", "JSON Lines (*.jsonl)")
        if f:
            from ..export import export_jsonl
            self.jobs.submit("Export JSONL", lambda ctx: export_jsonl(self.lib, Path(f), emb, log=ctx.log))

    # ================================================================== lifecycle
    def _first_run_checks(self):
        if not self.lib.systems():
            self.log("Welcome! Start by adding a retro system (➕ System), then add documents.")

    def closeEvent(self, e):
        for did in list(self.editors):
            if not self._close_editor(did):
                e.ignore()
                return
        if self.jobs.busy and QMessageBox.question(self, "Busy", "A job is still running. Quit anyway?") != QMessageBox.Yes:
            e.ignore()
            return
        self.jobs.shutdown()
        for s in self.servers:
            s.stop(2)
        self.app_settings.doc_split = self.doc_panel.split.sizes()
        self.app_settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode()
        self.app_settings.window_state = bytes(self.saveState().toBase64()).decode()
        self.app_settings.save()
        e.accept()
