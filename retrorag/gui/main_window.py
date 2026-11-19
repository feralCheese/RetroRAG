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
from .dialogs import AddDialog, ConnectDialog, MoveDialog, RemoveSystemDialog, SettingsDialog, SystemDialog
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
        self.setDragEnabled(True)
        self.setDragDropMode(QTreeWidget.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
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
        if e.mimeData().hasUrls() or e.source() is self:
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls() or e.source() is self:
            e.acceptProposedAction()

    def dropEvent(self, e):
        if e.source() is self:          # moving documents (or a system) within the library
            self.win.drop_move(self.itemAt(e.position().toPoint()))
            e.acceptProposedAction()
            return
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
        self.clear_filter_btn = QToolButton(text="\u2715")
        self.clear_filter_btn.setObjectName("clearFilter")
        self.clear_filter_btn.setToolTip("Clear the filter - show every document")
        self.clear_filter_btn.clicked.connect(self.clear_filter)
        self.clear_filter_btn.hide()
        fh.addWidget(self.filter, 1); fh.addWidget(self.filter_kind); fh.addWidget(self.clear_filter_btn)
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
        self.doc_panel.docs_added.connect(self._after_image_add)
        self.doc_panel.create_image.connect(lambda sid: self.create_image_dialog(sid))
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
        self.a_add_note = A("Add note", lambda: self.edit_note(None), "Ctrl+Shift+M",
                            "Write down your own findings about the selected system or peripheral", "edit")
        self.a_add_link = A("Add link", lambda: self.add_link(), "Ctrl+Shift+L",
                            "Save a web page, docs site or GitHub repository for the selected system - and copy it "
                            "into the library if you like", "link")
        self.a_ocr = A("Run OCR", self.ocr_selected_or_pending, "Ctrl+R",
                       "OCR the selected documents (or every document that needs it)", "scan")
        self.a_index = A("Build index", lambda: self.build_index(False), "F5", "Chunk + embed new/changed documents", "sync")
        self.a_servers = A("Start servers", self.toggle_servers, None, "Start REST API + MCP HTTP servers", "play")
        self.a_connect = A("Connect AI", self.show_connect, None, "How to connect Claude, opencode, Unsloth Studio, LM Studio…", "link")
        self.a_settings = A("Settings", self.show_settings, "Ctrl+,", None, "settings")

        tb = QToolBar("Main"); tb.setObjectName("main_toolbar"); tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        self.addToolBar(tb)
        for a in (self.a_add_system, self.a_add_files, self.a_add_folder, self.a_add_note, self.a_add_link, None,
                  self.a_ocr, self.a_index, None, self.a_servers, self.a_connect):
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
        a = m.addAction("Open disk / tape image…", self.open_image_file)
        a.setShortcut(QKeySequence("Ctrl+Shift+O"))
        a.setToolTip("Look inside any disk or tape image file and extract from it, without adding it to the library")
        m.addAction("Copy linked files into library (make portable)", self.consolidate)
        from ..platform_util import file_manager_name
        m.addAction(f"Show library in {file_manager_name()}", lambda: open_path(self.lib.root))
        m.addSeparator(); m.addAction(self.a_settings)
        m.addSeparator(); m.addAction("Exit", self.close)
        m = mb.addMenu("&System")
        m.addAction(self.a_add_system)
        m.addAction("Add peripheral / expansion to selected system…", self.add_peripheral)
        m.addAction("Edit selected system…", self.edit_system)
        m.addAction("Remove selected system…", self.remove_system)
        m.addSeparator()
        m.addAction(self.a_add_note)
        m.addAction(self.a_add_link)
        m.addAction("Links…", lambda: self.show_links())
        m = mb.addMenu("&Documents")
        for a in (self.a_add_files, self.a_add_folder, self.a_ocr):
            m.addAction(a)
        m.addAction("Force OCR on selected (all pages)", lambda: self.ocr_docs(self.selected_doc_ids(), force=True))
        m.addAction("Remove selected documents…", self.remove_selected_docs)
        m.addSeparator()
        a = m.addAction("Create disk / tape image…", lambda: self.create_image_dialog())
        a.setShortcut(QKeySequence("Ctrl+Shift+D"))
        a.setIcon(theme.icon("floppy"))
        self._action_icons.append((a, "floppy"))
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
        from .widgets import Spinner
        self.status_spinner = Spinner(16)
        self.status_busy = QLabel()
        self.status_busy.hide()
        self.statusBar().insertPermanentWidget(0, self.status_spinner)
        self.statusBar().insertPermanentWidget(1, self.status_busy)
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
        self.jobs.job_started.connect(self._on_job_busy)
        self.jobs.job_ended.connect(self._on_job_idle)
        self.doc_panel.cancel_job.connect(self.jobs.cancel)
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
        job = self.jobs.current
        if job is not None:
            text = f"{job.verb} - {msg}" + (f"  ({i}/{n})" if n else "") if msg and msg != "done" else f"{job.verb}…"
            self._show_busy(job, text)

    # ------------------------------------------------------------------ busy feedback
    def _show_busy(self, job, text: str):
        self.status_spinner.start()
        self.status_busy.setText(text[:90])
        self.status_busy.show()
        self.doc_panel.set_busy(set(job.doc_ids), text)
        for did, ed in self.editors.items():
            ed.set_busy(text if did in job.doc_ids else None)

    def _on_job_busy(self, job):
        self._show_busy(job, f"{job.verb}…")
        if job.doc_ids:
            self.refresh_tree()

    def _on_job_idle(self, job):
        if self.jobs._q.empty():
            self.status_spinner.stop()
            self.status_busy.hide()
        self.doc_panel.set_busy(set())
        for ed in self.editors.values():
            ed.set_busy(None)
        if job.doc_ids:
            self.refresh_tree()

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
        self.doc_panel.refresh_systems()
        self.doc_panel.preview.lib = self.lib
        self.doc_panel.refresh_current()

    def refresh_stats(self):
        st = self.lib.stats()
        self.stats_label.setText(f"{st['systems']} systems · {st['documents']} docs · {st['chunks']} chunks · "
                                 f"{st['needs_ocr']} need OCR · {st['stale']} out of date" +
                                 (f" · {st['links_due']} link(s) to re-check" if st["links_due"] else ""))
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

        n_tiles = 7 if st["links"] else 6
        width = f"{100 // n_tiles}%"

        def tile(n, label, col=None, goto=None, action="fix"):
            num = (f"<span style='font-size:{t['big']};font-weight:{t['bold']};color:{col or t['text']}'>{n}</span>")
            lab = f"<span style='color:{t['muted']}'>{label}</span>"
            if goto and n:   # clickable: jump to the first document behind this number
                return (f"<td width='{width}' style='background:{t['surface']};padding:14px 16px'>"
                        f"<a href='goto:{goto}' style='text-decoration:none'>{num}<br>{lab}<br>"
                        f"<span style='color:{t['accent']}'>{action} ›</span></a></td>")
            return f"<td width='{width}' style='background:{t['surface']};padding:14px 16px'>{num}<br>{lab}</td>"
        tiles = [tile(st["systems"], "systems"), tile(st["documents"], "documents"),
                 tile(st["chunks"], f"chunks · {st['embedded']} embedded"),
                 tile(st["needs_ocr"], "need OCR", t["warn"] if st["needs_ocr"] else None, "needs_ocr"),
                 tile(st["flagged_docs"], "with flagged words", t["warn"] if st["flagged_docs"] else None,
                      "flagged"),
                 tile(st["stale"], "out of date", t["warn"] if st["stale"] else None, "stale")]
        if st["links"]:
            gone = f"<br>{st['links_gone']} gone" if st["links_gone"] else ""
            tiles.append(tile(st["links_due"], f"links to re-check{gone}", t["warn"] if st["links_due"] else None,
                              "links", "re-check"))
        tiles = "".join(tiles)

        def step(n, title, body):
            return (f"<tr><td valign='top' style='padding:6px 12px 6px 0'><span style='background:{t['accent_soft']};"
                    f"color:{t['accent']};font-weight:{t['bold']}'>&nbsp;{n}&nbsp;</span></td>"
                    f"<td style='padding:6px 0'><b>{title}</b><br><span style='color:{t['muted']}'>{body}</span></td></tr>")
        steps = "".join([
            step(1, "New system", "Add a machine, e.g. <i>Commodore 64</i> with aliases <i>C64, CBM 64</i>. "
                 "Peripherals and expansions (a 1541 drive, a Tube second processor, an Amiga accelerator) are "
                 "systems too: right-click a machine - <b>Add peripheral / expansion</b>."),
            step(2, "Add files or folders", "Or drag &amp; drop onto a system or category: manuals, programming "
                 "references, schematics, pinouts, datasheets (PDF, GIF, JPG, PNG, TIFF, TXT, HTML), emulator source "
                 "trees, ROMs/firmware, disk &amp; tape images (D64, ADF, TAP, TZX, DSK…). Zips are unpacked."),
            step(3, "Run OCR", "Words Tesseract is unsure about are written as <code>{?w0rd|42?}</code>."),
            step(4, "Proofread", "Double-click a document. <b>F3</b> selects the next flagged word - type the fix - "
                 "<b>Ctrl+Enter</b> accepts it and moves on. The scan highlights the line &amp; word you're on."),
            step(5, "Build index (F5)", "Chunks and embeds everything new or changed. Try queries in <b>Search test</b>."),
            step(6, "Connect AI", "Claude Desktop / Claude Code / opencode (MCP), Unsloth Studio &amp; other MCP clients (HTTP), "
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
<p style='color:{t['muted']};margin-top:14px'><b>Add note</b> (Ctrl+Shift+M) keeps your own findings about a
system or peripheral in <i>My Notes</i>; the AI is told they're yours. <b>Add link</b> (Ctrl+Shift+L) saves a web
page, docs site or GitHub repository - and can copy it into the library, so local AIs can search it and it survives
the site going away. RetroRAG reminds you here when copies are due for a re-check.</p>
<p style='color:{t['muted']};margin-top:6px'>Tip: for schematics and pinouts, re-OCR a page in the editor with
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
        if s == "goto:links":
            self.show_links(due_only=True)
        elif s.startswith("goto:"):
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

    def clear_filter(self):
        self.filter.blockSignals(True); self.filter.clear(); self.filter.blockSignals(False)
        self.filter_kind.setCurrentIndex(0)      # triggers refresh_tree
        self.refresh_tree()

    def _update_filter_ui(self):
        on = bool(self.filter.text().strip()) or self.filter_kind.currentText() != "All"
        self.clear_filter_btn.setVisible(on)
        name = "filterActive" if self.filter_kind.currentText() != "All" else ""
        if self.filter_kind.objectName() != name:
            self.filter_kind.setObjectName(name)
            self.filter_kind.style().unpolish(self.filter_kind); self.filter_kind.style().polish(self.filter_kind)

    def select_system(self, system_id: int):
        """Show a system in the tree: open its families, select it, scroll to it."""
        for it in self._iter_items():
            if it.data(0, ROLE) == ("system", system_id):
                p = it.parent()
                while p is not None:
                    p.setExpanded(True)
                    p = p.parent()
                self.tree.setCurrentItem(it)
                self.tree.scrollToItem(it)
                return

    def refresh_tree(self):
        self._update_filter_ui()
        expanded = self._expanded_keys()
        sel = self._selected_key()
        self.tree.clear()
        flt = self.filter.text().strip().lower()
        mode = self.filter_kind.currentText()
        filtering = bool(flt) or mode != "All"
        c = self.lib.conn()
        systems = self.lib.systems()
        ids = {x.id for x in systems}
        by_id = {x.id: x for x in systems}
        hmap = {p: [h for h in hs if h in ids] for p, hs in self.lib.host_map().items()}
        periph_of: dict = {}
        for x in systems:
            for h in hmap.get(x.id, []):
                periph_of.setdefault(h, []).append(x)
        links_of: dict = {}
        for l in self.lib.links():
            links_of.setdefault(l.system_id, []).append(l)
        kids: dict = {}
        for x in systems:
            par = x.parent_id if x.parent_id in ids else None
            if par is None and hmap.get(x.id):
                continue           # a peripheral / expansion: shown under the machines it works with
            kids.setdefault(par, []).append(x)
        from ..config import NOTES_CATEGORY

        def build(s, parent_item, role: str = "top", path: frozenset = frozenset()) -> bool:
            if s.id in path:       # "works with" loops (A for B, B for A): show each once per branch
                return False
            path = path | {s.id}
            counts = {r[0]: r[1] for r in c.execute(
                "SELECT category, COUNT(*) FROM documents WHERE system_id=? GROUP BY category", (s.id,))}
            variant = role == "variant"
            is_periph = bool(hmap.get(s.id)) or role == "peripheral"
            si = QTreeWidgetItem([s.name, str(sum(counts.values()))])
            si.setData(0, ROLE, ("system", s.id))
            si.setIcon(0, theme.icon("chip" if is_periph else "system",
                                     theme.T["muted"] if role != "top" else theme.T["accent"]))
            si.setForeground(1, QColor(theme.T["muted"]))
            f = si.font(0); f.setBold(role == "top"); si.setFont(0, f)
            info = ", ".join(x for x in (s.kind if is_periph else "", s.manufacturer, s.year, s.cpu, s.aliases) if x)
            if kids.get(s.id):
                info += ("\n" if info else "") + "Documents here apply to all its variants"
            if variant:
                info += ("\n" if info else "") + f"Variant of {parent_item.text(0)}"
            if hmap.get(s.id):
                info += ("\n" if info else "") + "Peripheral / expansion for " + \
                        ", ".join(by_id[h].name for h in hmap[s.id])
            si.setToolTip(0, info)
            (parent_item.addChild(si) if parent_item else self.tree.addTopLevelItem(si))
            cats = [cat for cat in CATEGORIES if cat in counts] + sorted(set(counts) - set(CATEGORIES))
            for cat in cats:
                ci = QTreeWidgetItem([cat, str(counts[cat])])
                ci.setData(0, ROLE, ("category", s.id, cat))
                ci.setIcon(0, theme.icon("edit" if cat == NOTES_CATEGORY else "folder", theme.T["accent"]))
                ci.setForeground(1, QColor(theme.T["muted"]))
                si.addChild(ci)
                if filtering:
                    self._populate_category(ci, flt, mode)
                    if ci.childCount() == 0:
                        si.removeChild(ci)
                    else:
                        ci.setExpanded(True)
                else:
                    ci.addChild(QTreeWidgetItem(["…"]))   # lazy placeholder
            links = links_of.get(s.id, [])
            if links and mode == "All":
                shown = [l for l in links if not flt or flt in l.label.lower() or flt in l.url.lower()]
                if shown:
                    self._links_node(si, s.id, shown, filtering)
            for ch in kids.get(s.id, []):
                build(ch, si, "variant", path)
            periph = [p for p in periph_of.get(s.id, []) if p.id not in path]
            if periph:
                pn = QTreeWidgetItem(["Peripherals & expansions", str(len(periph))])
                pn.setData(0, ROLE, ("peripherals", s.id))
                pn.setIcon(0, theme.icon("chip", theme.T["accent"]))
                pn.setForeground(1, QColor(theme.T["muted"]))
                pn.setToolTip(0, f"Disk drives, second processors, expansion boards... that work with {s.name}.\n"
                                 f"Searching {s.name} includes their documents.")
                si.addChild(pn)
                for p in periph:
                    build(p, pn, "peripheral", path)
                if filtering and pn.childCount():
                    pn.setExpanded(True)
            # systems always stay visible (a new, empty system mustn't vanish under a filter); with a filter on,
            # open the ones that have matching documents
            if (filtering and si.childCount()) or ("system", s.id) in expanded:
                si.setExpanded(True)
            return True

        for top in kids.get(None, []):
            build(top, None)
        # restore expansions
        for item in self._iter_items():
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
        working = self.jobs.current is not None and d.id in self.jobs.current.doc_ids
        if working:
            status, col = "working…", t["accent"]
        elif d.id in self.jobs.pending_doc_ids:
            status, col = "queued", t["muted"]
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

    def _links_node(self, si: QTreeWidgetItem, system_id: int, links: list, filtering: bool):
        from .web_ui import link_tooltip, status_text
        t = theme.T
        due = sum(1 for l in links if l.is_due())
        ln = QTreeWidgetItem(["Links", f"{due} due" if due else str(len(links))])
        ln.setData(0, ROLE, ("links", system_id))
        ln.setIcon(0, theme.icon("link", t["accent"]))
        ln.setForeground(1, QColor(t["warn"] if due else t["muted"]))
        ln.setToolTip(0, "Web pages, docs sites and GitHub repositories saved for this system. Copied ones are in "
                         "the categories above too.")
        si.addChild(ln)
        for l in links:
            st, col = status_text(l)
            it = QTreeWidgetItem([l.label, st])
            it.setData(0, ROLE, ("link", l.id))
            it.setIcon(0, theme.icon("link" if l.copies else "view", t["muted"]))
            it.setForeground(1, QColor(t.get(col, t["muted"])))
            it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            if l.status == "gone":
                f = it.font(0); f.setStrikeOut(True); it.setFont(0, f)
            tip = link_tooltip(self.lib, l)
            it.setToolTip(0, tip); it.setToolTip(1, tip)
            ln.addChild(it)
        if filtering:
            ln.setExpanded(True)

    # ================================================================== peripherals, notes, links
    def add_peripheral(self, host_id: int | None = None):
        if host_id is None:
            s = self.current_system()
            if not s:
                QMessageBox.information(self, "Add peripheral / expansion",
                                        "Select the machine it works with in the library tree first.")
                return
            host_id = s.id
        self.add_system(host_id=host_id)

    def attach_existing(self, host_id: int):
        from .dialogs import system_choices
        host = self.lib.system(host_id)
        have = {p.id for p in self.lib.peripherals(host_id)}
        choices = [(label, sid) for label, sid in system_choices(self.lib, {host_id}) if sid not in have]
        if not choices:
            QMessageBox.information(self, "Attach", "There are no other systems to attach.")
            return
        label, ok = QInputDialog.getItem(self, "Attach a peripheral / expansion",
                                         f"Which system works with {host.name}?",
                                         [c[0].strip().lstrip("└ ") for c in choices], 0, False)
        if not ok:
            return
        sid = choices[[c[0].strip().lstrip("└ ") for c in choices].index(label)][1]
        self.lib.add_host(sid, host_id)
        self.log(f"{self.lib.system(sid).name} now works with {host.name}.")
        self.refresh_all()
        self.schedule_index()

    def detach_peripheral(self, system_id: int, host_id: int):
        s, h = self.lib.system(system_id), self.lib.system(host_id)
        if QMessageBox.question(self, "Detach", f"Stop listing {s.name} as a peripheral / expansion for {h.name}?\n\n"
                                "Nothing is deleted.") != QMessageBox.Yes:
            return
        self.lib.remove_host(system_id, host_id)
        self.refresh_all()
        self.schedule_index()

    def edit_note(self, doc_id: int | None = None, system_id: int | None = None):
        from .notes_ui import NoteDialog
        if doc_id is None and system_id is None:
            s = self.current_system()
            system_id = s.id if s else None
        if doc_id is None and not self.lib.systems():
            QMessageBox.information(self, "Add a system first", "Notes belong to a system or peripheral - add one "
                                    "first.")
            return
        if doc_id is not None and doc_id in self.editors:
            self._close_editor(doc_id)
        dlg = NoteDialog(self, self.lib, system_id, doc_id)
        dlg.saved.connect(lambda i: (self.log(f"Saved note '{self.lib.document(i).title}'"), self.refresh_all(),
                                     self.select_doc(i), self.schedule_index()))
        dlg.deleted.connect(lambda i: (self.doc_panel.show_doc(None), self.refresh_all()))
        dlg.exec()

    def add_link(self, system_id: int | None = None):
        from .web_ui import save_link_dialog
        if system_id is None:
            s = self.current_system()
            system_id = s.id if s else None
        if not self.lib.systems():
            QMessageBox.information(self, "Add a system first", "Links belong to a system or peripheral - add one "
                                    "first.")
            return
        lid = save_link_dialog(self, self.lib, None, system_id)
        if lid:
            self.refresh_all()
            self.run_links([lid])

    def edit_link(self, link_id: int):
        from .web_ui import save_link_dialog
        l = self.lib.link(link_id)
        if not l:
            return
        lid = save_link_dialog(self, self.lib, l)
        self.refresh_all()
        if lid:
            self.run_links([lid])

    def remove_link(self, link_id: int):
        from .web_ui import remove_link_dialog
        l = self.lib.link(link_id)
        if not l:
            return
        ids = self.lib.link_doc_ids(link_id)
        for i in ids:
            if not self._close_editor(i):
                return
        r = remove_link_dialog(self, self.lib, l)
        if r and r[1]:
            self.doc_panel.show_doc(None)
        self.refresh_all()

    def open_link(self, link_id: int):
        l = self.lib.link(link_id)
        if l:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl(l.url))

    def show_links(self, system_id: int | None = None, due_only: bool = False):
        from .web_ui import LinksWindow
        win = getattr(self, "links_win", None)
        if win is not None:
            win.close()
            win.deleteLater()
        win = LinksWindow(self, self.lib, system_id, due_only)
        win.run_links.connect(self.run_links)
        win.changed.connect(self.refresh_all)
        win.docs_deleted.connect(lambda ids: ([self._close_editor(i, force=True) for i in ids],
                                              self.doc_panel.show_doc(None), self.refresh_all()))
        self.links_win = win
        win.show()
        win.raise_()

    def run_links(self, link_ids: list[int]):
        links = [l for l in (self.lib.link(i) for i in link_ids) if l]
        if not links:
            return
        lib_root, ids = str(self.lib.root), [l.id for l in links]
        copies = any(l.copies for l in links)
        name = (f"Copy {links[0].label[:40]}" if len(links) == 1 and links[0].copies else
                f"Check {links[0].label[:40]}" if len(links) == 1 else f"Re-check {len(links)} links")

        def job(ctx):
            return ctx.run_process("retrorag.proc:links_job", library=lib_root, link_ids=ids)

        def done(res):
            res = res or []
            bad = [r for r in res if r.get("status") != "ok"]
            self.refresh_all()
            if getattr(self, "links_win", None) is not None:
                try:
                    self.links_win.refresh()
                except RuntimeError:
                    self.links_win = None
            if any(r.get("added") or r.get("updated") for r in res):
                self.schedule_index()
            if len(res) == 1:
                r = res[0]
                self.statusBar().showMessage(f"{r['title']}: {r['message']}", 12000)
            else:
                self.statusBar().showMessage(f"Re-checked {len(res)} link(s)" +
                                             (f" - {len(bad)} need a look (see Links)" if bad else ""), 12000)
            if len(res) == 1 and bad and bad[0]["status"] in ("error", "refused") and copies:
                QMessageBox.information(self, "Link", f"{bad[0]['title']}\n\n{bad[0]['message']}")
        self.jobs.submit(name, job, on_done=done, verb="Copying from the web" if copies else "Checking links")
        if not self.jobs.busy:
            self.statusBar().showMessage("Copying runs in the background - you can keep working.", 6000)

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
        return self.system_of(self.tree.currentItem())

    def system_of(self, it):
        while it is not None:
            key = it.data(0, ROLE)
            if key and key[0] in ("system", "category", "folder", "links", "peripherals"):
                return self.lib.system(key[1])
            if key and key[0] == "link":
                l = self.lib.link(key[1])
                return self.lib.system(l.system_id) if l else None
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
        elif key and key[0] == "link":
            self.edit_link(key[1])

    def _context_menu(self, pos):
        it = self.tree.itemAt(pos)
        key = it.data(0, ROLE) if it else None
        m = QMenu(self)
        if not key:
            m.addAction(self.a_add_system)
        elif key[0] == "peripherals":
            m.addAction("Add peripheral / expansion…", lambda: self.add_peripheral(key[1]))
            m.addAction("Attach an existing system…", lambda: self.attach_existing(key[1]))
        elif key[0] == "links":
            m.addAction("Add link…", lambda: self.add_link(key[1]))
            ids = [l.id for l in self.lib.links(key[1])]
            m.addAction("Re-copy / check them all", lambda: self.run_links(ids))
            m.addAction("Open the Links window…", lambda: self.show_links(key[1]))
        elif key[0] == "link":
            l = self.lib.link(key[1])
            m.addAction("Open in browser", lambda: self.open_link(key[1]))
            m.addAction("Re-copy now" if l and l.copies else "Check now", lambda: self.run_links([key[1]]))
            m.addAction("Edit link…", lambda: self.edit_link(key[1]))
            m.addAction("Open the Links window…", lambda: self.show_links(l.system_id if l else None))
            m.addSeparator()
            m.addAction("Remove link…", lambda: self.remove_link(key[1]))
        elif key[0] == "system":
            m.addAction("Add files…", self.add_files_dialog)
            m.addAction("Add folder…", self.add_folder_dialog)
            m.addAction("Add note…", lambda: self.edit_note(None, key[1]))
            m.addAction("Add link…", lambda: self.add_link(key[1]))
            m.addSeparator()
            m.addAction("Edit system…", self.edit_system)
            m.addAction("Add variant…", lambda: self.add_system(parent_id=key[1]))
            m.addAction("Add peripheral / expansion…", lambda: self.add_peripheral(key[1]))
            par = it.parent()
            pkey = par.data(0, ROLE) if par is not None else None
            if pkey and pkey[0] == "peripherals":
                host = self.lib.system(pkey[1])
                m.addAction(f"Detach from {host.name}…", lambda: self.detach_peripheral(key[1], pkey[1]))
            m.addAction("Move all its documents to…", self.move_selected_dialog)
            m.addAction("Create disk / tape image…", lambda: self.create_image_dialog(key[1]))
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
            from ..config import NOTES_CATEGORY
            if key[0] == "category" and key[2] == NOTES_CATEGORY:
                m.addAction("Add note…", lambda: self.edit_note(None, key[1]))
            if key[0] == "doc":
                m.addAction("Show in folder", lambda: self.doc_action("show_folder", key[1]))
                d = self.lib.document(key[1])
                m.addAction("Put on a new disk / tape image…", lambda: self.create_image_dialog(
                    d.system_id, [i for i in self.selected_doc_ids()] or [key[1]]))
            m.addSeparator()
            m.addAction("Move to…", self.move_selected_dialog)
            m.addAction("Remove…", self.remove_selected_docs)
        m.exec(self.tree.viewport().mapToGlobal(pos))

    # ================================================================== systems
    def add_system(self, parent_id: int | None = None, host_id: int | None = None):
        dlg = SystemDialog(self, None, self.lib, parent_id, host_id=host_id)
        if not dlg.exec():
            return
        v = dlg.values()
        if self.lib.find_system(v["name"]):
            QMessageBox.warning(self, "Exists", f"A system called '{v['name']}' already exists.")
            return
        s = self.lib.add_system(**v)
        self.log(f"Added system {s.name}")
        self.refresh_all()
        self.select_system(s.id)
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
        dlg = SystemDialog(self, s, self.lib)
        if dlg.exec():
            try:
                self.lib.update_system(s.id, **dlg.values())
            except ValueError as e:
                QMessageBox.warning(self, "Edit system", str(e))
            self.refresh_all()
            self.select_system(s.id)
            self.schedule_index()

    def remove_system(self):
        s = self.current_system()
        if not s:
            QMessageBox.information(self, "Remove system", "Select a system in the library tree first.")
            return
        n = len(self.lib.documents(s.id))
        par = self.lib.system(s.parent_id) if s.parent_id else None
        dlg = RemoveSystemDialog(self, s, n, self.lib.system_dir(s), [c.name for c in self.lib.children(s.id)],
                                 par.name if par else None)
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
        fam = self.lib.find_system(man.get("variant_of", "")) if man.get("variant_of") else None
        s = self.lib.find_system(name) or self.lib.add_system(
            name.strip(), parent_id=fam.id if fam else None,
            **{k: v for k, v in man.items() if k not in ("name", "variant_of")})
        self._run_import(s, folder)

    def _run_import(self, s, folder: Path):
        lib_root = str(self.lib.root)

        def job(ctx):
            return ctx.run_process("retrorag.proc:import_folder_job", library=lib_root, system_id=s.id, folder=str(folder))
        self.jobs.submit(f"Import {s.name}", job, on_done=lambda ids: (self.refresh_all(), self.schedule_index()),
                         verb=f"Importing into {s.name}")

    def consolidate(self):
        if QMessageBox.question(self, "Make portable", "Copy every linked-in-place file into the library folder?") \
                == QMessageBox.Yes:
            self.jobs.submit("Copy linked files", lambda ctx: ingest.consolidate_linked(self.lib, ctx.log),
                             on_done=lambda n: self.refresh_all())

    # ================================================================== moving documents
    def move_selected_dialog(self):
        ids = self.selected_doc_ids()
        if not ids:
            QMessageBox.information(self, "Move documents", "Select documents (or a category / folder) first.")
            return
        cur = self.current_system()
        dlg = MoveDialog(self, self.lib, len(ids), cur.id if cur else None)
        if dlg.exec():
            sid, cat, group = dlg.values()
            self.move_docs(ids, sid, cat, group)

    def drop_move(self, target):
        """Documents dragged onto a system / category in the tree (or a system onto another system)."""
        key = target.data(0, ROLE) if target else None
        if not key:
            return
        dragged = [it.data(0, ROLE) for it in self.tree.selectedItems() if it.data(0, ROLE)]
        if len(dragged) == 1 and dragged[0][0] == "system" and key[0] == "peripherals" and dragged[0][1] != key[1]:
            child, host = self.lib.system(dragged[0][1]), self.lib.system(key[1])
            if QMessageBox.question(self, "Peripheral / expansion",
                                    f"List '{child.name}' as a peripheral / expansion for '{host.name}'?\n\n"
                                    f"Searching {host.name} will then include {child.name}'s documents.") \
                    == QMessageBox.Yes:
                self.lib.add_host(child.id, host.id)
                self.refresh_all()
                self.schedule_index()
            return
        if len(dragged) == 1 and dragged[0][0] == "system" and key[0] == "system" and dragged[0][1] != key[1]:
            child, fam = self.lib.system(dragged[0][1]), self.lib.system(key[1])
            box = QMessageBox(self)
            box.setWindowTitle("Systems")
            box.setText(f"What is '{child.name}' to '{fam.name}'?")
            box.setInformativeText(f"A variant shares {fam.name}'s documents (e.g. ZX Spectrum 128K of ZX Spectrum).\n"
                                   f"A peripheral / expansion is its own hardware that works with {fam.name} (e.g. a "
                                   f"disk drive): searching {fam.name} includes its documents.")
            b_var = box.addButton("A variant", QMessageBox.AcceptRole)
            b_per = box.addButton("A peripheral / expansion", QMessageBox.AcceptRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() is b_var:
                try:
                    self.lib.set_parent(child.id, fam.id)
                except ValueError as e:
                    QMessageBox.warning(self, "Make variant", str(e))
            elif box.clickedButton() is b_per:
                self.lib.add_host(child.id, fam.id)
            else:
                return
            self.refresh_all()
            self.schedule_index()
            return
        if key[0] in ("peripherals", "links", "link"):
            return
        # where it lands: a category = its top level, a folder = that folder, a document = beside it,
        # a system = same category and folder as now
        if key[0] == "doc":
            d = self.lib.document(key[1])
            sid, cat, group = d.system_id, d.category, d.group_path
        elif key[0] == "folder":
            sid, cat, group = key[1], key[2], key[3]
        elif key[0] == "category":
            sid, cat, group = key[1], key[2], ""
        else:
            sid, cat, group = key[1], None, None

        def changes(d) -> bool:
            return (d.system_id != sid or (cat is not None and d.category != cat)
                    or (group is not None and d.group_path != group))
        ids = [i for i in self.selected_doc_ids() if (d := self.lib.document(i)) and changes(d)]
        if not ids:
            return
        where = " (top level)" if group == "" else (f" / {group.split('/')[-1]}" if group else "")
        target_name = self.lib.system(sid).name + (f" / {cat}" if cat else "") + where
        if QMessageBox.question(self, "Move documents", f"Move {len(ids)} document(s) to {target_name}?\n\n"
                                "Files move on disk too, with their OCR text, corrections and links.") == QMessageBox.Yes:
            self.move_docs(ids, sid, cat, group)

    def move_docs(self, ids: list[int], system_id: int, category: str | None, group_path: str | None = None):
        for i in ids:
            if not self._close_editor(i):
                return
        sname = self.lib.system(system_id).name

        def job(ctx):
            return self.lib.move_documents(ids, system_id, category, log=ctx.log, group_path=group_path)

        def done(n):
            self.refresh_all()
            self.statusBar().showMessage(f"Moved {n} document(s) to {sname}" + (f" / {category}" if category else ""), 6000)
            self.schedule_index()
        self.jobs.submit(f"Move {len(ids)} document(s)", job, on_done=done, doc_ids=ids, verb=f"Moving to {sname}")

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
            if key[0] in ("links", "link", "peripherals"):
                system = self.system_of(item)
        folders = [p for p in paths if p.is_dir()]
        files = [p for p in paths if p.is_file()]
        if files:
            dlg = AddDialog(self, self.lib.systems(), system, files, folder=False)
            if category:                    # dropped onto a category: that's where the user wants them
                dlg.set_all(category)
            if dlg.exec():
                v = dlg.values()
                s = self.lib.system(v["system_id"])
                lib_root = str(self.lib.root)
                self.jobs.submit(f"Add {len(files)} file(s) to {s.name}", lambda ctx: ctx.run_process(
                    "retrorag.proc:add_files_job", library=lib_root, system_id=s.id, category=None,
                    categories=v["categories"], paths=[str(f) for f in files], copy=v["copy"]),
                    on_done=self._after_add, verb=f"Adding {len(files)} file(s)")
        for folder in folders:
            dlg = AddDialog(self, self.lib.systems(), system, [folder], folder=True)
            if category:
                dlg.set_all(category)
            if dlg.exec():
                v = dlg.values()
                s = self.lib.system(v["system_id"])
                lib_root = str(self.lib.root)
                self.jobs.submit(f"Add folder {folder.name}", lambda ctx, folder=folder, v=v, s=s: ctx.run_process(
                    "retrorag.proc:add_folder_job", library=lib_root, system_id=s.id, category=v["category"],
                    folder=str(folder), copy=v["copy"], kinds=sorted(v["kinds"]) if v["kinds"] is not None else None),
                    on_done=self._after_add, verb=f"Adding folder {folder.name}")

    def open_image_file(self, path: str | None = None):
        from .media_ui import ImageBrowserDialog, image_file_filter, image_from_file
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Open disk / tape image", "", image_file_filter())
        if not path:
            return
        got = image_from_file(self, Path(path))
        if not got:
            return
        img, title, img_path = got
        dlg = ImageBrowserDialog(self, self.lib, img, title, img_path)
        dlg.add_to_library.connect(lambda p: self.add_paths([Path(p)]))
        dlg.exec()

    def create_image_dialog(self, system_id: int | None = None, doc_ids: list[int] | None = None):
        from .media_ui import CreateImageDialog
        if system_id is None:
            s = self.current_system()
            system_id = s.id if s else None
        dlg = CreateImageDialog(self, self.lib, system_id, doc_ids)
        if dlg.exec() and dlg.created_doc:
            d = self.lib.document(dlg.created_doc)
            self.log(f"Created disk / tape image '{d.title}'")
            self.refresh_all()
            self.select_doc(d.id)
            self.schedule_index()

    def _after_image_add(self, ids):
        self.log(f"Added {len(ids)} file(s) from the disk / tape image to the library.")
        self.refresh_all()
        self.schedule_index()

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
        from ..ocr import has_manual_edits
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
        lib_root, doc_ids = str(self.lib.root), [d.id for d in docs]

        def job(ctx):
            # separate process: PDF rendering and image clean-up would otherwise freeze the window
            return ctx.run_process("retrorag.proc:ocr_job", library=lib_root, doc_ids=doc_ids, pages=pages,
                                   force=force, psm=psm, cleanup=cleanup, app_tesseract=tess)

        def done(res):
            self.refresh_all()
            self.schedule_index()
            if on_done:
                on_done(res)
        self.jobs.submit(f"OCR {len(docs)} document(s)" if not pages else f"Re-OCR page {pages[0]}", job, on_done=done,
                         doc_ids=doc_ids, verb="Running OCR" if not pages else f"Re-running OCR on page {pages[0]}")
        if not self.jobs.busy:
            self.statusBar().showMessage("OCR started - it runs in the background; you can keep working.", 6000)

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
            lib_root = str(self.lib.root)
            self.jobs.submit(f"Re-extract {d.title}", lambda ctx: ctx.run_process(
                "retrorag.proc:extract_job", library=lib_root, doc_id=doc_id),
                on_done=lambda _r: (self.refresh_all(), self.schedule_index()), doc_ids=[doc_id],
                verb="Re-extracting text")
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
        from ..notes import is_note
        if is_note(d):
            self.edit_note(doc_id)
            return
        if doc_id in self.editors:
            ed = self.editors[doc_id]
        else:
            ed = ProofEditor(self.lib, d, self.app_settings.editor_font_size)
            ed.saved.connect(lambda i: (self.refresh_tree(), self.schedule_index(), self.refresh_stats(),
                                        self.statusBar().showMessage(f"Saved {self.lib.document(i).title}", 4000)))
            ed.dirty_changed.connect(lambda dirty, ed=ed: self._mark_tab(ed, dirty))
            ed.cancel_job.connect(self.jobs.cancel)
            ed.request_reocr.connect(self._reocr_page)
            self.editors[doc_id] = ed
            self.tabs.addTab(ed, (d.title[:28] + "…") if len(d.title) > 28 else d.title)
        self.tabs.setCurrentWidget(ed)
        if page:
            QTimer.singleShot(50, lambda: ed.goto_page(page))

    def _mark_tab(self, ed, dirty: bool):
        i = self.tabs.indexOf(ed)
        if i >= 0:
            title = (ed.doc.title[:28] + "…") if len(ed.doc.title) > 28 else ed.doc.title
            self.tabs.setTabText(i, ("\u25cf " if dirty else "") + title)
            self.tabs.setTabToolTip(i, "Unsaved changes - Ctrl+S to save" if dirty else ed.doc.title)

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
        everywhere = st.api_host in ("0.0.0.0", "")
        self.server_label.setText(f"REST :{st.api_port} · MCP :{st.mcp_port}" + (" (LAN)" if everywhere else ""))
        if everywhere:
            from ..platform_util import lan_address
            lan = lan_address()
            self.log(f"Servers listening on all network interfaces ({st.api_host or '0.0.0.0'}):")
            if lan:
                self.log(f"  from other computers:  REST http://{lan}:{st.api_port}/docs   "
                         f"MCP http://{lan}:{st.mcp_port}/mcp")
            self.log(f"  on this PC:            REST http://127.0.0.1:{st.api_port}/docs   "
                     f"MCP http://127.0.0.1:{st.mcp_port}/mcp")
        else:
            self.log(f"REST API  http://{st.api_host}:{st.api_port}/docs\nMCP HTTP  http://{st.api_host}:{st.mcp_port}/mcp")

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
