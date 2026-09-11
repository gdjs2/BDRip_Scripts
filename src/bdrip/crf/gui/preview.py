"""Interactive model previews, with saved-image viewing for older sweep results."""

from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from bdrip.common.files import read_json
from bdrip.crf.model import METHOD


class ImagePreview(QWidget):
    def __init__(self):
        super().__init__()
        self.original = QPixmap()
        self.zoom: float | None = None
        self.scale = 1.0
        self.setMinimumSize(300, 210)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        toolbar = QHBoxLayout()
        self.buttons = []
        for label, callback in (
            ("Fit", lambda: self.set_zoom(None)),
            ("100%", lambda: self.set_zoom(1.0)),
            ("−", lambda: self.set_zoom(self.scale / 1.25)),
            ("+", lambda: self.set_zoom(self.scale * 1.25)),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
            self.buttons.append(button)
        self.zoom_label = QLabel()
        toolbar.addWidget(self.zoom_label)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        self.scroll = QScrollArea()
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas = QLabel()
        self.canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas.setWordWrap(True)
        self.scroll.setWidget(self.canvas)
        layout.addWidget(self.scroll, 1)
        self.scroll.viewport().installEventFilter(self)
        self._render()

    def show_file(self, path: Path | None) -> None:
        self.original = QPixmap()
        if path:
            try:
                self.original.loadFromData(path.read_bytes())
            except OSError:
                pass
        else:
            self.zoom = None
        self._render()

    def set_zoom(self, factor: float | None) -> None:
        self.zoom = None if factor is None else min(3.0, max(0.1, factor))
        self._render()

    def _render(self) -> None:
        available = self.scroll.viewport().size()
        for button in self.buttons:
            button.setEnabled(not self.original.isNull())
        if self.original.isNull():
            self.canvas.clear()
            self.canvas.setText(
                "Measured points appear as encoding finishes. Curves appear after CRF 13 and 20 are complete."
            )
            self.canvas.resize(available)
            self.zoom_label.clear()
            return
        fit = min(
            available.width() / self.original.width(),
            available.height() / self.original.height(),
        )
        self.scale = max(0.01, min(1.0, fit)) if self.zoom is None else self.zoom
        size = self.original.size() * self.scale
        self.canvas.setPixmap(
            self.original.scaled(
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.canvas.resize(size)
        self.zoom_label.setText(
            f"{'Fit · ' if self.zoom is None else ''}{self.scale:.0%}"
        )

    def eventFilter(self, watched, event):
        if watched is self.scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._render()
        return super().eventFilter(watched, event)


class FigurePreview(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack)
        self.image = ImagePreview()
        self.stack.addWidget(self.image)
        self.chart = None

    def set_report(self, report: dict) -> None:
        if report.get("method") == METHOD:
            if self.chart is None:
                # Load the Qt plotting backend only when a model is available.
                from bdrip.crf.gui.plot import InteractivePlot

                self.chart = InteractivePlot()
                self.stack.addWidget(self.chart)
            self.chart.set_report(report)
            self.stack.setCurrentWidget(self.chart)
        else:
            if self.chart:
                self.chart.reset()
            self.stack.setCurrentWidget(self.image)

    def show_file(self, path: Path | None) -> None:
        if path is None:
            self.set_report({})
        self.image.show_file(path)


class FigureDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.resize(1050, 780)
        self.path: Path | None = None
        self.stamp = None
        self.report_stamp = None
        layout = QVBoxLayout(self)
        self.preview = FigurePreview()
        layout.addWidget(self.preview)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)

    def set_file(self, path: Path, title: str) -> None:
        if path != self.path:
            self.preview.show_file(None)
            self.stamp = None
            self.report_stamp = None
        self.path = path
        self.setWindowTitle(f"Figure preview · {title}")
        self.refresh()

    def refresh(self) -> None:
        if self.path:
            report_path = self.path.with_name("results.json")
            try:
                stat = report_path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                stamp = None
            if stamp != self.report_stamp:
                self.preview.set_report(read_json(report_path))
                self.report_stamp = stamp
            try:
                stat = self.path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                stamp = None
            if stamp != self.stamp:
                self.preview.show_file(self.path)
                self.stamp = stamp

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()
        self.timer.start()

    def hideEvent(self, event) -> None:
        self.timer.stop()
        super().hideEvent(event)
