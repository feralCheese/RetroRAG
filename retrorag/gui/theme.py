"""Modern light/dark theme (Fluent-style) and icons drawn from the Windows icon font.

  apply_theme(app, "system" | "light" | "dark" | "retro", phosphor=..., scanlines=..., font_file=...)
  icon("search")            -> QIcon in the current text colour
  T["accent"]               -> current colour tokens (for HTML in QTextBrowsers)
"""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QIcon, QPainter, QPainterPath, QPalette, QPixmap,
                           QRadialGradient)
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow, QWidget

LIGHT = dict(bg="#f3f4f7", surface="#ffffff", surface2="#f6f7f9", border="#e3e6ec", border_strong="#cdd2db",
             text="#1d2330", muted="#6b7385", accent="#4f6bed", accent_hover="#3d59da", accent_soft="#e7ebfd",
             on_accent="#ffffff", warn="#b45309", ok="#15803d", danger="#d63c3c", scrim="#e9ecf2")
DARK = dict(bg="#1a1c22", surface="#22252d", surface2="#2a2e37", border="#343944", border_strong="#474e5c",
            text="#e7e9ef", muted="#9aa1b2", accent="#8098ff", accent_hover="#97abff", accent_soft="#2e3654",
            on_accent="#0f1220", warn="#f5a524", ok="#4ade80", danger="#f87171", scrim="#15171c")

_TYPE = dict(radius_note="", bold="600", mono="Consolas", heading="16pt", h1="20pt", h2="13pt", big="22pt")
for _t in (LIGHT, DARK):
    _t.update(_TYPE)

# Retro: monochrome CRT phosphors. 'text' is the phosphor colour; everything else is a shade of it.
PHOSPHORS = {
    "green": dict(bg="#030803", surface="#061009", surface2="#0a1a0e", border="#1d5229", border_strong="#2d8343",
                  text="#5cff7a", muted="#2fa24c", accent="#a6ffb6", accent_hover="#d0ffd9", accent_soft="#0f3a19",
                  on_accent="#021004", warn="#d9ff7a", ok="#5cff7a", danger="#f0fff3", scrim="#020502"),
    "amber": dict(bg="#0a0600", surface="#120b00", surface2="#1c1200", border="#5a3c00", border_strong="#8a5c00",
                  text="#ffb000", muted="#a86d00", accent="#ffd27a", accent_hover="#ffe7b8", accent_soft="#3a2600",
                  on_accent="#140c00", warn="#fff1a0", ok="#ffb000", danger="#fff7e6", scrim="#060300"),
    "white": dict(bg="#050607", surface="#0b0d0e", surface2="#141719", border="#3a4044", border_strong="#5a6266",
                  text="#e6edf0", muted="#8a9499", accent="#ffffff", accent_hover="#ffffff", accent_soft="#2a2f33",
                  on_accent="#050607", warn="#ffffff", ok="#e6edf0", danger="#ffffff", scrim="#020303"),
}
PHOSPHOR_NAMES = {"green": "Green phosphor (PET)", "amber": "Amber phosphor", "white": "White phosphor"}
RETRO_FONT_FILE = Path(__file__).resolve().parent / "fonts" / "RetroRAG-Phosphor.ttf"
RETRO_PX = 16          # pixel fonts are drawn at a multiple of their 8-dot em, so every dot is a crisp 2x2 block

T: dict = dict(LIGHT)          # current tokens
_dark = False
_retro = False
_retro_family: str | None = None
_loaded_fonts: dict[str, str] = {}
_scanlines = None              # ScanlineManager when active

# Segoe Fluent Icons / Segoe MDL2 Assets code points
GLYPHS = {
    "add": 0xE710, "doc": 0xE8A5, "folder": 0xE8B7, "open_folder": 0xE838, "sync": 0xE895, "refresh": 0xE72C,
    "play": 0xE768, "stop": 0xE71A, "settings": 0xE713, "search": 0xE721, "home": 0xE80F, "save": 0xE74E,
    "edit": 0xE70F, "delete": 0xE74D, "prev": 0xE76B, "next": 0xE76C, "zoom_in": 0xE8A3, "zoom_out": 0xE71F,
    "check": 0xE73E, "flag": 0xE7C1, "link": 0xE71B, "export": 0xEDE1, "system": 0xE7F4, "code": 0xE943,
    "image": 0xEB9F, "pdf": 0xEA90, "chip": 0xE950, "floppy": 0xE74E, "scan": 0xE8FE, "view": 0xE890,
    "info": 0xE946, "warning": 0xE7BA, "accept": 0xE8FB, "fit": 0xE9A6, "library": 0xE8F1, "server": 0xE968,
    "filter": 0xE71C, "list": 0xEA37, "text": 0xE8D2,
}
_icon_font: str | None = None
_cache: dict = {}


def is_dark() -> bool:
    return _dark


def is_retro() -> bool:
    return _retro


def load_font_file(path: str | Path) -> str | None:
    """Register a .ttf/.otf with Qt; returns its family name."""
    key = str(path)
    if key in _loaded_fonts:
        return _loaded_fonts[key]
    fid = QFontDatabase.addApplicationFont(key)
    fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
    if fams:
        _loaded_fonts[key] = fams[0]
        return fams[0]
    return None


def retro_font(px: int = RETRO_PX) -> QFont:
    f = QFont(_retro_family or "Consolas")
    f.setPixelSize(px)
    f.setStyleStrategy(QFont.NoAntialias)
    f.setHintingPreference(QFont.PreferNoHinting)
    f.setFixedPitch(True)
    return f


def mono_font(point_size: int = 10) -> QFont:
    """Monospace font for text editors/logs in the current theme."""
    if _retro:
        return retro_font(RETRO_PX)
    f = QFont("Consolas", point_size)
    f.setStyleHint(QFont.Monospace)
    return f


def phosphorize(img):
    """Render a scan as it would look on the monitor: light paper -> dark screen, ink -> glowing phosphor."""
    from PIL import ImageOps
    g = ImageOps.invert(img.convert("L"))
    g = ImageOps.autocontrast(g, cutoff=1)
    return ImageOps.colorize(g, black=T["scrim"], white=T["text"], mid=T["muted"]).convert("RGB")


class ScanlineOverlay(QWidget):
    """Transparent layer over a window: CRT scan lines + a soft vignette. Ignores the mouse."""

    def __init__(self, host: QWidget):
        super().__init__(host)
        self.setObjectName("scanlines")
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.NoFocus)
        tile = QPixmap(4, 3)
        tile.fill(Qt.transparent)
        p = QPainter(tile)
        p.fillRect(0, 2, 4, 1, QColor(0, 0, 0, 105))
        p.end()
        self._tile = tile
        host.installEventFilter(self)
        self.setGeometry(host.rect())
        self.raise_()
        self.show()

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t in (QEvent.Resize, QEvent.Show, QEvent.LayoutRequest):
            self.setGeometry(obj.rect())
            self.raise_()
        elif t == QEvent.ChildAdded:
            QTimer.singleShot(0, self.raise_)
        return False

    def paintEvent(self, e):
        p = QPainter(self)
        p.drawTiledPixmap(self.rect(), self._tile)
        r = self.rect()
        g = QRadialGradient(QPointF(r.center()), max(r.width(), r.height()) * 0.75)
        g.setColorAt(0.0, QColor(0, 0, 0, 0))
        g.setColorAt(0.7, QColor(0, 0, 0, 0))
        g.setColorAt(1.0, QColor(0, 0, 0, 110))
        p.fillRect(e.rect(), g)
        p.end()


class ScanlineManager(QObject):
    """Adds a ScanlineOverlay to every main window and dialog while the retro theme (with scan lines) is on."""

    def __init__(self, app: QApplication):
        super().__init__(app)
        self.app = app
        app.installEventFilter(self)
        for w in app.topLevelWidgets():
            self._attach(w)

    def _attach(self, w):
        if isinstance(w, (QMainWindow, QDialog)) and not w.findChild(ScanlineOverlay, "scanlines",
                                                                       Qt.FindDirectChildrenOnly):
            ScanlineOverlay(w)

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Show and isinstance(obj, QWidget) and obj.isWindow():
            self._attach(obj)
        return False

    def remove(self):
        self.app.removeEventFilter(self)
        for w in self.app.topLevelWidgets():
            for ov in w.findChildren(ScanlineOverlay, "scanlines", Qt.FindDirectChildrenOnly):
                ov.hide()
                ov.deleteLater()
        self.deleteLater()


def _find_icon_font() -> str | None:
    fams = set(QFontDatabase.families())
    for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets"):
        if f in fams:
            return f
    return None


def icon(name: str, color: str | None = None) -> QIcon:
    """Glyph icon; empty QIcon if the Windows icon font is unavailable (buttons then just show text)."""
    global _icon_font
    if _icon_font is None:
        _icon_font = _find_icon_font() or ""
    if not _icon_font or name not in GLYPHS:
        return QIcon()
    color = color or T["text"]
    key = (name, color, _dark)
    if key in _cache:
        return _cache[key]
    ic = QIcon()
    for size in (16, 20, 24, 32, 48):
        for mode, col in ((QIcon.Normal, color), (QIcon.Disabled, T["border_strong"])):
            pm = QPixmap(size, size)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.TextAntialiasing)
            f = QFont(_icon_font)
            f.setPixelSize(int(size * 0.8))
            p.setFont(f)
            p.setPen(QColor(col))
            p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, chr(GLYPHS[name]))
            p.end()
            ic.addPixmap(pm, mode)
    _cache[key] = ic
    return ic


def app_icon() -> QIcon:
    """Rounded accent tile with a chip glyph."""
    ic = QIcon()
    font = _icon_font or _find_icon_font()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
        p.fillPath(path, QColor("#4f6bed"))
        p.setPen(QColor("white"))
        if font:
            f = QFont(font); f.setPixelSize(int(size * 0.62)); p.setFont(f)
            p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, chr(GLYPHS["chip"]))
        else:
            f = QFont("Segoe UI", 1, QFont.Bold); f.setPixelSize(int(size * 0.6)); p.setFont(f)
            p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, "R")
        p.end()
        ic.addPixmap(pm)
    return ic


# ---------------------------------------------------------------------- small SVG assets for QSS
def _assets() -> dict[str, str]:
    from ..config import app_dir
    d = app_dir() / "theme"
    d.mkdir(parents=True, exist_ok=True)
    m, a, oa = T["muted"], T["accent"], T["on_accent"]
    svgs = {
        "down": f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10"><path d="M1.5 3.5 5 7l3.5-3.5" fill="none" stroke="{m}" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
        "up": f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10"><path d="M1.5 6.5 5 3l3.5 3.5" fill="none" stroke="{m}" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
        "check": f'<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12"><path d="M2.5 6.2 5 8.6l4.6-5" fill="none" stroke="{oa}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
        "dot": f'<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12"><circle cx="6" cy="6" r="2.6" fill="{oa}"/></svg>',
        "close": f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10"><path d="M2 2l6 6M8 2 2 8" stroke="{m}" stroke-width="1.3" stroke-linecap="round"/></svg>',
        "close_hover": f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10"><path d="M2 2l6 6M8 2 2 8" stroke="{a}" stroke-width="1.6" stroke-linecap="round"/></svg>',
    }
    out = {}
    tag = "dark" if _dark else "light"
    for k, svg in svgs.items():
        f = d / f"{k}_{tag}.svg"
        f.write_text(svg, "utf-8")
        out[k] = f.as_posix()
    return out


def stylesheet() -> str:
    a = _assets()
    t = T
    return f"""
* {{ outline: 0; }}
QWidget {{ color: {t['text']}; }}
QMainWindow, QDialog {{ background: {t['bg']}; }}
QMainWindow::separator {{ background: {t['bg']}; width: 6px; height: 6px; }}
QLabel#heading {{ font-size: 16pt; font-weight: 600; }}
QLabel#muted, QLabel#subtle {{ color: {t['muted']}; }}

QMenuBar {{ background: {t['surface']}; border-bottom: 1px solid {t['border']}; padding: 2px 4px; }}
QMenuBar::item {{ padding: 5px 10px; border-radius: 6px; background: transparent; }}
QMenuBar::item:selected {{ background: {t['surface2']}; }}
QMenu {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 8px; padding: 5px; }}
QMenu::item {{ padding: 6px 26px 6px 12px; border-radius: 5px; }}
QMenu::item:selected {{ background: {t['accent_soft']}; }}
QMenu::item:disabled {{ color: {t['muted']}; }}
QMenu::separator {{ height: 1px; background: {t['border']}; margin: 5px 8px; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 6px; }}

QToolBar {{ background: {t['surface']}; border: none; border-bottom: 1px solid {t['border']}; padding: 5px 8px; spacing: 3px; }}
QToolBar::separator {{ background: {t['border']}; width: 1px; margin: 6px 6px; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 5px 9px; }}
QToolButton:hover {{ background: {t['surface2']}; border-color: {t['border']}; }}
QToolButton:pressed, QToolButton:checked {{ background: {t['accent_soft']}; border-color: {t['accent_soft']}; }}
QToolButton:disabled {{ color: {t['muted']}; }}
QToolButton::menu-indicator {{ image: url({a['down']}); subcontrol-position: right center; right: 4px; }}
QToolButton[popupMode="2"] {{ padding-right: 18px; }}

QToolButton#segLeft, QToolButton#segRight {{ border: 1px solid {t['border_strong']}; padding: 3px 14px; background: {t['surface']}; border-radius: 0; }}
QToolButton#segLeft {{ border-top-left-radius: 6px; border-bottom-left-radius: 6px; }}
QToolButton#segRight {{ border-top-right-radius: 6px; border-bottom-right-radius: 6px; border-left: none; }}
QToolButton#segLeft:checked, QToolButton#segRight:checked {{ background: {t['accent']}; color: {t['on_accent']}; border-color: {t['accent']}; }}

QPushButton {{ background: {t['surface']}; border: 1px solid {t['border_strong']}; border-radius: 6px; padding: 6px 14px; min-height: 16px; }}
QPushButton:hover {{ background: {t['surface2']}; border-color: {t['accent']}; }}
QPushButton:pressed {{ background: {t['accent_soft']}; }}
QPushButton:disabled {{ color: {t['muted']}; border-color: {t['border']}; background: {t['surface2']}; }}
QPushButton:default, QPushButton#primary {{ background: {t['accent']}; color: {t['on_accent']}; border-color: {t['accent']}; font-weight: 600; }}
QPushButton:default:hover, QPushButton#primary:hover {{ background: {t['accent_hover']}; border-color: {t['accent_hover']}; }}
QPushButton:default:disabled, QPushButton#primary:disabled {{ background: {t['surface2']}; color: {t['muted']}; border-color: {t['border']}; }}
QPushButton#flat {{ border-color: transparent; background: transparent; text-align: left; padding: 6px 10px; }}
QPushButton#flat:hover {{ background: {t['surface2']}; border-color: {t['border']}; }}

QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit, QTextBrowser {{
    background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 6px; padding: 4px 7px;
    selection-background-color: {t['accent']}; selection-color: {t['on_accent']}; }}
QLineEdit, QComboBox, QSpinBox {{ min-height: 20px; }}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{ border-color: {t['border_strong']}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border: 1px solid {t['accent']}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{ color: {t['muted']}; background: {t['surface2']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: url({a['down']}); width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 6px; padding: 4px;
    selection-background-color: {t['accent_soft']}; selection-color: {t['text']}; }}
QSpinBox::up-button, QSpinBox::down-button {{ border: none; width: 18px; background: transparent; }}
QSpinBox::up-arrow {{ image: url({a['up']}); width: 9px; height: 9px; }}
QSpinBox::down-arrow {{ image: url({a['down']}); width: 9px; height: 9px; }}

QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px; border: 1px solid {t['border_strong']}; background: {t['surface']}; }}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {t['accent']}; }}
QCheckBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; image: url({a['check']}); }}
QRadioButton::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; image: url({a['dot']}); }}

QTabWidget::pane {{ border: none; border-top: 1px solid {t['border']}; background: {t['bg']}; }}
QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{ background: transparent; color: {t['muted']}; padding: 8px 14px; border: none; border-bottom: 2px solid transparent; margin-right: 2px; }}
QTabBar::tab:hover {{ color: {t['text']}; }}
QTabBar::tab:selected {{ color: {t['text']}; border-bottom: 2px solid {t['accent']}; font-weight: 600; }}
QTabBar::close-button {{ image: url({a['close']}); subcontrol-position: right; margin-left: 4px; width: 12px; height: 12px; }}
QTabBar::close-button:hover {{ image: url({a['close_hover']}); }}

QTreeView, QListView, QTableView {{ background: {t['surface']}; border: none; alternate-background-color: {t['surface2']};
    selection-background-color: {t['accent_soft']}; selection-color: {t['text']}; }}
QTreeView::item {{ padding: 4px 2px; border: none; }}
QTreeView::item:hover {{ background: {t['surface2']}; }}
QTreeView::item:selected {{ background: {t['accent_soft']}; color: {t['text']}; }}
QTreeView::branch:selected {{ background: {t['accent_soft']}; }}
QTreeView::branch:hover {{ background: {t['surface2']}; }}
QHeaderView::section {{ background: {t['surface']}; border: none; border-bottom: 1px solid {t['border']}; padding: 5px 8px; color: {t['muted']}; }}

QGroupBox {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 10px; margin-top: 22px; padding: 14px 12px 12px 12px; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 4px; top: 2px; padding: 0 4px; color: {t['muted']}; }}

QDockWidget {{ titlebar-close-icon: none; }}
QDockWidget::title {{ background: {t['bg']}; padding: 7px 10px 4px 10px; color: {t['muted']}; text-align: left; }}
QWidget#dockBody {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 10px; }}

QStatusBar {{ background: {t['surface']}; border-top: 1px solid {t['border']}; color: {t['muted']}; }}
QStatusBar QLabel {{ color: {t['muted']}; padding: 0 10px; }}
QStatusBar::item {{ border: none; }}

QProgressBar {{ background: {t['surface2']}; border: none; border-radius: 4px; max-height: 8px; min-height: 8px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {t['accent']}; border-radius: 4px; }}

QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t['border_strong']}; border-radius: 3px; min-height: 30px; margin: 0 2px; }}
QScrollBar::handle:horizontal {{ background: {t['border_strong']}; border-radius: 3px; min-width: 30px; margin: 2px 0; }}
QScrollBar::handle:hover {{ background: {t['muted']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 6px; }}
QSplitter::handle:vertical {{ height: 6px; }}
QSplitter::handle:hover {{ background: {t['accent_soft']}; }}

QGraphicsView {{ background: {t['scrim']}; border: 1px solid {t['border']}; border-radius: 8px; }}
QPlainTextEdit#log {{ background: {t['surface2']}; border: none; color: {t['muted']}; font-family: Consolas; }}
QToolTip {{ background: {t['surface']}; color: {t['text']}; border: 1px solid {t['border']}; padding: 5px 8px; border-radius: 6px; }}
QMessageBox QLabel {{ min-width: 280px; }}
"""


def _retro_qss(qss: str) -> str:
    """Square corners, no bold (pixel fonts have one weight), pixel-font monospace, phosphor glow on focus."""
    qss = re.sub(r"border-(?:top-|bottom-)?(?:left-|right-)?radius: \d+px", "border-radius: 0px", qss)
    qss = qss.replace("font-weight: 600", "font-weight: 400")
    qss = qss.replace("font-family: Consolas", f"font-family: '{T['mono']}'")
    qss = qss.replace("QLabel#heading {{ font-size: 16pt;".replace("{{", "{"), f"QLabel#heading {{ font-size: {T['heading']};")
    t = T
    return qss + f"""
QMenu::item:selected, QTreeView::item:selected, QComboBox QAbstractItemView {{ color: {t['accent']}; }}
QTreeView::item:selected {{ background: {t['accent_soft']}; color: {t['accent']}; }}
QTabBar::tab:selected {{ color: {t['accent']}; }}
QPushButton:default, QPushButton#primary {{ background: {t['text']}; border-color: {t['text']}; color: {t['on_accent']}; }}
QToolButton#segLeft:checked, QToolButton#segRight:checked {{ background: {t['text']}; border-color: {t['text']}; }}
QLineEdit, QComboBox, QSpinBox {{ min-height: 22px; }}
QPlainTextEdit, QTextBrowser {{ selection-background-color: {t['text']}; selection-color: {t['bg']}; }}
"""


def _palette() -> QPalette:
    t = T
    p = QPalette()
    c = QColor
    for role, col in ((QPalette.Window, t["bg"]), (QPalette.WindowText, t["text"]), (QPalette.Base, t["surface"]),
                      (QPalette.AlternateBase, t["surface2"]), (QPalette.Text, t["text"]), (QPalette.Button, t["surface"]),
                      (QPalette.ButtonText, t["text"]), (QPalette.Highlight, t["accent"]),
                      (QPalette.HighlightedText, t["on_accent"]), (QPalette.ToolTipBase, t["surface"]),
                      (QPalette.ToolTipText, t["text"]), (QPalette.PlaceholderText, t["muted"]),
                      (QPalette.Link, t["accent"]), (QPalette.LinkVisited, t["accent"]), (QPalette.Mid, t["border"]),
                      (QPalette.Dark, t["border_strong"]), (QPalette.Light, t["surface"])):
        p.setColor(role, c(col))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, c(t["muted"]))
    return p


def system_is_dark(app: QApplication) -> bool:
    try:
        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except Exception:
        return False


def apply_theme(app: QApplication, mode: str = "system", phosphor: str = "green", scanlines: bool = True,
                font_file: str = "") -> bool:
    """Apply the theme; returns True if dark. mode 'retro' = monochrome CRT look with a pixel font."""
    global _dark, _retro, _retro_family, _scanlines
    _retro = mode == "retro"
    dark = True if _retro else (system_is_dark(app) if mode == "system" else mode == "dark")
    _dark = dark
    T.clear()
    if _retro:
        _retro_family = None
        if font_file and Path(font_file).is_file():
            _retro_family = load_font_file(font_file)
        if not _retro_family:
            _retro_family = load_font_file(RETRO_FONT_FILE)
        T.update(PHOSPHORS.get(phosphor, PHOSPHORS["green"]))
        T.update(_TYPE)
        T.update(mono=_retro_family or "Consolas", bold="400", heading=f"{RETRO_PX * 3 // 2}px",
                 h1=f"{RETRO_PX * 2}px", h2=f"{RETRO_PX * 3 // 2}px", big=f"{RETRO_PX * 2}px")
    else:
        T.update(DARK if dark else LIGHT)
    try:   # dark title bars / native dialogs follow along (Qt 6.8+)
        app.styleHints().setColorScheme(Qt.ColorScheme.Unknown if mode == "system" else
                                        (Qt.ColorScheme.Dark if dark else Qt.ColorScheme.Light))
    except Exception:
        pass
    app.setStyle("Fusion")
    app.setPalette(_palette())
    if _retro:
        app.setFont(retro_font())
    else:
        app.setFont(QFont("Segoe UI Variable Text" if "Segoe UI Variable Text" in QFontDatabase.families()
                          else "Segoe UI", 10))
    qss = stylesheet()
    app.setStyleSheet(_retro_qss(qss) if _retro else qss)
    _cache.clear()
    if _retro and scanlines:
        if _scanlines is None:
            _scanlines = ScanlineManager(app)
    elif _scanlines is not None:
        _scanlines.remove()
        _scanlines = None
    return dark
