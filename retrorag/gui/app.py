from __future__ import annotations

import sys


def run(library: str | None = None) -> int:
    from PySide6.QtWidgets import QApplication

    from ..config import APP_NAME, AppSettings
    from . import theme
    from .main_window import MainWindow

    try:   # own taskbar icon/group instead of python.exe's
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RetroRAG.App")
    except Exception:
        pass
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    s = AppSettings.load()
    theme.apply_theme(app, s.theme, s.retro_phosphor, s.retro_scanlines, s.retro_font_file)
    app.setWindowIcon(theme.app_icon())
    w = MainWindow(library)
    w.show()
    return app.exec()
