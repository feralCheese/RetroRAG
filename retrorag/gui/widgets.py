"""Small reusable widgets: a spinner and a busy banner."""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

from . import theme


class Spinner(QWidget):
    """Rotating arc. start()/stop(); hidden while stopped."""

    def __init__(self, size: int = 18, parent=None):
        super().__init__(parent)
        self._angle = 0
        self._timer = QTimer(self, interval=40, timeout=self._tick)
        self.setFixedSize(QSize(size, size))
        self.hide()

    def start(self):
        self.show()
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self.hide()

    def _tick(self):
        self._angle = (self._angle + 24) % 360
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = 2.5 if self.width() < 24 else 3.5
        r = QRectF(w, w, self.width() - 2 * w, self.height() - 2 * w)
        track = QColor(theme.T["border"])
        p.setPen(QPen(track, w))
        p.drawEllipse(r)
        p.setPen(QPen(QColor(theme.T["accent"]), w, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(r, -self._angle * 16, 100 * 16)
        p.end()


class BusyBanner(QFrame):
    """'Working…' strip with a spinner, a message and Cancel."""
    cancel = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("busyBanner")
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 6, 6, 6)
        self.spinner = Spinner(18)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.btn = QPushButton("Cancel")
        self.btn.clicked.connect(self.cancel.emit)
        h.addWidget(self.spinner)
        h.addWidget(self.label, 1)
        h.addWidget(self.btn)
        self.hide()

    def show_busy(self, text: str):
        self.label.setText(text)
        self.spinner.start()
        self.show()

    def clear(self):
        self.spinner.stop()
        self.hide()
