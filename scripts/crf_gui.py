"""Desktop queue for two-point CRF models. Run with: uv run --extra gui scripts/crf_gui.py"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

try:
    from PySide6.QtCore import Qt, QTimer, QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import (
        QApplication, QComboBox, QDialog, QFileDialog, QGroupBox, QHBoxLayout,
        QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
        QProgressBar, QPushButton, QSplitter, QTabWidget,
        QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
    )
except ImportError as exc:
    raise SystemExit("The desktop GUI needs the optional gui dependencies. Run:\n"
                     "  uv run --extra gui scripts/crf_gui.py") from exc

if __package__:
    from .crf_config import load_config, validate_config
    from .crf_gui_options import EncoderSettingsDialog
    from .crf_gui_preview import FigureDialog, FigurePreview
    from .crf_gui_models import ModelEstimates
    from .crf_progress import read_json, write_json
    from .crf_queue import Task, TaskQueue
else:
    from crf_config import load_config, validate_config
    from crf_gui_options import EncoderSettingsDialog
    from crf_gui_preview import FigureDialog, FigurePreview
    from crf_gui_models import ModelEstimates
    from crf_progress import read_json, write_json
    from crf_queue import Task, TaskQueue


def elapsed(task: Task) -> str:
    seconds = max(0, int((task.finished or time.time()) - task.started)) if task.started else 0
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def tail(path: Path, limit: int = 64 * 1024) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            truncated = handle.tell() > limit
            handle.seek(max(0, handle.tell() - limit))
            text = handle.read(limit).decode("utf-8", errors="replace")
        return ("[Showing the end of this log; the full file is saved.]\n" if truncated else "") + text
    except OSError:
        return "The log will appear when this task starts."


class Window(QMainWindow):
    def __init__(self, workspace: Path, config_path: Path | None = None):
        super().__init__()
        self.setWindowTitle("CRF Video Queue")
        self.resize(1150, 850)
        self.config = load_config(config_path)
        self.queue = TaskQueue(workspace, self)
        self.closing = False
        self.items: dict[str, QTreeWidgetItem] = {}
        self.selected_id = None
        self.figure_stamp = self.log_stamp = self.report_stamp = None
        self.last_log_scan = 0.0
        self.figure_dialog: FigureDialog | None = None

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(18, 14, 18, 14)
        title = QLabel("CRF Video Queue")
        title.setStyleSheet("font-size: 22px; font-weight: 600;")
        layout.addWidget(title)
        layout.addWidget(QLabel("Centered 60-second sample · CRF 13 and 20 · Linear QP and exponential bitrate models"))

        settings = QGroupBox("Settings for new tasks")
        form = QVBoxLayout(settings)
        config_row = QHBoxLayout()
        self.config_label = QLabel(str(config_path) if config_path else "Default encoder settings")
        self.config_label.setWordWrap(True)
        config_row.addWidget(self.config_label, 1)
        config_row.addWidget(self.button("Encoder options…", self.edit_encoders))
        config_row.addWidget(self.button("Load config…", self.load_settings))
        config_row.addWidget(self.button("Save config…", self.save_settings))
        form.addLayout(config_row)
        values = QHBoxLayout()
        values.addWidget(QLabel("One clip from the middle · Whole video if shorter than 60 seconds"))
        values.addStretch()
        values.addWidget(QLabel("Codec"))
        self.codec = QComboBox()
        self.codec.addItems(["both", "x264", "x265"])
        values.addWidget(self.codec)
        values.addWidget(QLabel("Crop"))
        self.crop = QLineEdit()
        self.crop.setToolTip("auto, none, or exact width:height:x:y")
        values.addWidget(self.crop, 1)
        form.addLayout(values)
        self.encoder_label = QLabel()
        self.encoder_label.setWordWrap(True)
        form.addWidget(self.encoder_label)
        layout.addWidget(settings)
        self.fill_settings()

        controls = QHBoxLayout()
        controls.addWidget(self.button("Add videos…", self.add_videos))
        self.start_button = self.button("Start queue", self.queue.start)
        self.pause_button = self.button("Pause after current", self.queue.pause)
        controls.addWidget(self.start_button)
        controls.addWidget(self.pause_button)
        controls.addStretch()
        self.cancel_button = self.button("Cancel task", lambda: self.task_action(self.queue.cancel))
        self.retry_button = self.button("Retry task", lambda: self.task_action(self.queue.retry))
        self.remove_button = self.button("Remove task", lambda: self.task_action(self.queue.remove))
        self.remove_button.setToolTip("Remove from the queue list; keep all saved figures and logs")
        for button in (self.cancel_button, self.retry_button, self.remove_button):
            controls.addWidget(button)
        layout.addLayout(controls)

        self.overview = QLabel()
        layout.addWidget(self.overview)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Video", "Codec", "State", "Progress", "Elapsed"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setMinimumHeight(110)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            self.tree.header().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemSelectionChanged.connect(self.refresh_details)
        splitter.addWidget(self.tree)

        details = QWidget()
        detail_layout = QVBoxLayout(details)
        detail_layout.setContentsMargins(0, 8, 0, 0)
        self.detail_title = QLabel("Select a task to view its results and logs.")
        self.detail_title.setWordWrap(True)
        self.detail_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_layout.addWidget(self.detail_title)
        self.progress_label = QLabel()
        self.progress_label.setWordWrap(True)
        detail_layout.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        detail_layout.addWidget(self.progress_bar)

        self.tabs = QTabWidget()
        self.figure = FigurePreview()
        self.tabs.addTab(self.figure, "QP and bitrate curves")
        self.models = ModelEstimates()
        self.tabs.addTab(self.models, "Estimates")
        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        self.log_selector = QComboBox()
        self.log_selector.currentIndexChanged.connect(self.refresh_log)
        log_layout.addWidget(self.log_selector)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        log_layout.addWidget(self.log)
        self.tabs.addTab(log_panel, "Logs")
        self.saved_settings = QPlainTextEdit()
        self.saved_settings.setReadOnly(True)
        self.tabs.addTab(self.saved_settings, "Task settings")
        detail_layout.addWidget(self.tabs, 1)
        links = QHBoxLayout()
        self.folder_button = self.button("Open results folder", lambda: self.open_result())
        self.figure_button = self.button("Larger preview…", self.preview_figure)
        self.csv_button = self.button("Open CSV table", lambda: self.open_result("summary.csv"))
        for button in (self.folder_button, self.figure_button, self.csv_button):
            links.addWidget(button)
        links.addStretch()
        detail_layout.addLayout(links)
        splitter.addWidget(details)
        splitter.setSizes([110, 490])
        layout.addWidget(splitter, 1)
        self.statusBar().showMessage(f"Queue folder: {self.queue.workspace}")
        self.queue.changed.connect(self.refresh)
        self.queue.problem.connect(self.show_error)
        self.refresh()

    @staticmethod
    def button(text, callback) -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(callback)
        return button

    def fill_settings(self) -> None:
        self.crop.setText(self.config["video"]["crop"] or "none")
        self.update_encoder_summary()

    def update_encoder_summary(self) -> None:
        self.encoder_label.setText("  ·  ".join(
            f"{name}: {settings['profile']} @ {settings['level'] or 'auto'}, {settings['preset']}"
            for name, settings in self.config["codecs"].items()))

    def current_config(self) -> dict:
        config = validate_config(self.config)
        config["video"]["crop"] = None if self.crop.text().strip().lower() == "none" else self.crop.text().strip()
        return validate_config(config)

    def edit_encoders(self) -> None:
        dialog = EncoderSettingsDialog(self.config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.config["codecs"] = dialog.config["codecs"]
            self.config_label.setText("Custom encoder settings")
            self.update_encoder_summary()
        dialog.deleteLater()

    def save_settings(self) -> None:
        try:
            config = self.current_config()
            path, _ = QFileDialog.getSaveFileName(self, "Save video and encoder configuration",
                                                 "crf_search.json", "JSON files (*.json)")
            if path:
                write_json(Path(path), config)
                self.config = config
                self.config_label.setText(path)
        except (ValueError, OSError) as exc:
            self.show_error(str(exc))

    def load_settings(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load encoder configuration", "", "JSON files (*.json)")
        if path:
            try:
                self.config = load_config(Path(path))
                self.config_label.setText(path)
                self.fill_settings()
            except ValueError as exc:
                self.show_error(str(exc))

    def add_videos(self) -> None:
        try:
            config = self.current_config()
            paths, _ = QFileDialog.getOpenFileNames(
                self, "Add videos to the queue", "", "Videos (*.mkv *.mp4 *.m2ts *.mts *.mov *.avi *.webm *.ts *.m4v);;All files (*)")
            if paths:
                added = self.queue.add(paths, config, self.codec.currentText())
                self.tree.setCurrentItem(self.items[added[0].id])
        except (ValueError, OSError) as exc:
            self.show_error(str(exc))

    def selected_task(self) -> Task | None:
        selected = self.tree.selectedItems()
        task_id = selected[0].data(0, Qt.ItemDataRole.UserRole) if selected else None
        return next((task for task in self.queue.tasks if task.id == task_id), None)

    def task_action(self, action) -> None:
        task = self.selected_task()
        if task:
            try:
                result = action(task)
                if isinstance(result, Task):
                    self.tree.setCurrentItem(self.items[result.id])
            except (OSError, ValueError) as exc:
                self.show_error(str(exc))

    def refresh(self) -> None:
        ids = {task.id for task in self.queue.tasks}
        for task_id in list(self.items):
            if task_id not in ids:
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(self.items.pop(task_id)))
        for index, task in enumerate(self.queue.tasks):
            if task.id not in self.items:
                item = QTreeWidgetItem(self.tree)
                item.setData(0, Qt.ItemDataRole.UserRole, task.id)
                item.setToolTip(0, task.video)
                self.items[task.id] = item
            item = self.items[task.id]
            for column, value in enumerate([Path(task.video).name, task.codec, task.state.title(),
                                            f"{task.percent}%", elapsed(task)]):
                item.setText(column, value)
            # Keep visual order consistent when a retried task moves to the end.
            old_index = self.tree.indexOfTopLevelItem(item)
            if old_index != index:
                selected = item.isSelected()
                self.tree.takeTopLevelItem(old_index)
                self.tree.insertTopLevelItem(index, item)
                item.setSelected(selected)
        queued = sum(task.state == "queued" for task in self.queue.tasks)
        completed = sum(task.state == "completed" for task in self.queue.tasks)
        active = self.queue.active
        activity = f"Running: {Path(active.video).name} · {elapsed(active)}" if active else "Queue paused" if queued else "Queue idle"
        self.overview.setText(f"{activity}   |   {queued} queued · {completed} completed")
        self.start_button.setEnabled(bool(queued) and not self.queue.enabled)
        self.pause_button.setEnabled(self.queue.enabled)
        if not self.tree.selectedItems() and self.queue.tasks:
            self.tree.setCurrentItem(self.items[(active or self.queue.tasks[0]).id])
        self.refresh_details()
        if self.closing and not self.queue.active:
            QTimer.singleShot(0, self.close)

    def refresh_details(self) -> None:
        task = self.selected_task()
        changed = (task.id if task else None) != self.selected_id
        if changed:
            self.selected_id = task.id if task else None
            self.figure_stamp = self.log_stamp = self.report_stamp = None
            self.last_log_scan = 0.0
            self.figure.show_file(None)
            self.models.set_report({})
            self.saved_settings.setPlainText(json.dumps(task.config, indent=2) if task else "")
            self.log_selector.blockSignals(True)
            self.log_selector.clear()
            self.log_selector.addItem("Task log", "task.log")
            self.log_selector.blockSignals(False)
        self.cancel_button.setEnabled(bool(task and task.state in {"queued", "running"}))
        self.retry_button.setEnabled(bool(task and task.state in {"failed", "cancelled", "interrupted"}))
        self.remove_button.setEnabled(bool(task and task is not self.queue.active))
        self.folder_button.setEnabled(bool(task))
        self.figure_button.setEnabled(bool(task and any((self.queue.output(task) / name).exists()
                                                       for name in ("results.json", "qp-bitrate.png"))))
        self.csv_button.setEnabled(bool(task and (self.queue.output(task) / "summary.csv").exists()))
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(task.percent if task else 0)
        if not task:
            self.detail_title.setText("Select a task to view its results and logs.")
            self.progress_label.clear()
            self.log.clear()
            return
        self.detail_title.setText(task.video)
        progress = task.progress
        message = task.error or progress.get("message", task.state.title())
        if task.state == "cancelling":
            message = "Cancelling the active sample and saving completed results…"
        if task.state == "running" and not progress.get("total"):
            self.progress_bar.setRange(0, 0)
        counts = f" · {progress.get('completed', 0)}/{progress['total']} encodes complete" if progress.get("total") else ""
        frames = f" · {progress['frames']} frames submitted" if progress.get("frames") and progress.get("stage") == "encoding" else ""
        flushing = " · flushing encoder" if frames and progress.get("flushing") else ""
        self.progress_label.setText(f"{message}{counts}{frames}{flushing} · {elapsed(task)}")
        report_path = self.queue.output(task) / "results.json"
        try:
            stamp = report_path.stat().st_mtime_ns
            if stamp != self.report_stamp:
                report = read_json(report_path)
                self.models.set_report(report)
                self.figure.set_report(report)
                self.report_stamp = stamp
        except OSError:
            pass
        figure_path = self.queue.output(task) / "qp-bitrate.png"
        try:
            stamp = figure_path.stat().st_mtime_ns
            if stamp != self.figure_stamp:
                self.figure.show_file(figure_path)
                self.figure_stamp = stamp
        except OSError:
            pass
        if time.monotonic() - self.last_log_scan >= 2:
            existing = {self.log_selector.itemData(i) for i in range(self.log_selector.count())}
            for path in sorted((self.queue.output(task) / "logs").glob("*.log")):
                relative = str(path.relative_to(self.queue.output(task)))
                if relative not in existing:
                    self.log_selector.addItem(path.name, relative)
            self.last_log_scan = time.monotonic()
        self.refresh_log()

    def refresh_log(self) -> None:
        task = self.selected_task()
        if not task:
            return
        path = self.queue.output(task) / (self.log_selector.currentData() or "task.log")
        try:
            stamp = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        except OSError:
            stamp = (str(path), None)
        if stamp != self.log_stamp:
            scrollbar = self.log.verticalScrollBar()
            following = scrollbar.value() >= scrollbar.maximum()
            position = scrollbar.value()
            self.log.setPlainText(tail(path))
            scrollbar.setValue(scrollbar.maximum() if following else position)
            self.log_stamp = stamp

    def open_result(self, name: str = "") -> None:
        task = self.selected_task()
        if task:
            path = self.queue.output(task) / name
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                self.show_error(f"Could not open {path}")

    def preview_figure(self) -> None:
        task = self.selected_task()
        if task:
            if self.figure_dialog is None:
                self.figure_dialog = FigureDialog(self)
            self.figure_dialog.set_file(self.queue.output(task) / "qp-bitrate.png", Path(task.video).name)
            self.figure_dialog.show()
            self.figure_dialog.raise_()

    def show_error(self, message: str) -> None:
        QMessageBox.warning(self, "CRF Video Queue", message)

    def closeEvent(self, event) -> None:
        if self.queue.active:
            event.ignore()
            if not self.closing:
                self.queue.pause()
                try:
                    self.queue.cancel(self.queue.active)
                except OSError as exc:
                    self.show_error(f"Cannot cancel the active task: {exc}")
                    return
                self.closing = True
                self.centralWidget().setEnabled(False)
                self.statusBar().showMessage("Stopping the active task before closing; queued tasks and results will be kept.")
            return
        if self.figure_dialog:
            self.figure_dialog.close()
        self.queue.close()
        event.accept()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd() / "crf-tasks",
                        help="Folder for the saved queue and each task's figures, reports, and logs")
    parser.add_argument("--config", type=Path, help="Initial video and encoder configuration")
    args = parser.parse_args(argv)
    app = QApplication([sys.argv[0]])
    app.setApplicationName("CRF Video Queue")
    try:
        window = Window(args.workspace, args.config)
    except (OSError, ValueError) as exc:
        QMessageBox.critical(None, "Cannot open queue", str(exc))
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
