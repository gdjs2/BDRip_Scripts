"""Desktop queue for two-point CRF models. Run with: uv run --extra gui bdrip gui"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    from PySide6.QtCore import Qt, QTimer, QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import (
        QApplication,
        QComboBox,
        QDialog,
        QDoubleSpinBox,
        QFileDialog,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMenu,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QSizePolicy,
        QSpinBox,
        QSplitter,
        QTabWidget,
        QToolButton,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:
    raise SystemExit(
        "The desktop GUI needs the optional gui dependencies. Run:\n"
        "  uv run --extra gui bdrip gui"
    ) from exc

from bdrip.common.files import read_json, write_json
from bdrip.crf.config import load_config, validate_config
from bdrip.crf.gui.estimates import ModelEstimates
from bdrip.crf.gui.options import EncoderSettingsDialog
from bdrip.crf.gui.preview import FigureDialog, FigurePreview
from bdrip.crf.gui.queue import Task, TaskQueue
from bdrip.crf.gui.style import STYLE


def elapsed(task: Task) -> str:
    seconds = (
        max(0, int((task.finished or time.time()) - task.started))
        if task.started
        else 0
    )
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
        return (
            "[Showing the end of this log; the full file is saved.]\n"
            if truncated
            else ""
        ) + text
    except OSError:
        return "The log will appear when this task starts."


class Window(QMainWindow):
    def __init__(
        self,
        workspace: Path,
        config_path: Path | None = None,
        *,
        output_root: Path | None = None,
    ):
        super().__init__()
        self.setWindowTitle("CRF Studio")
        self.resize(1080, 760)
        self.setMinimumSize(880, 620)
        self.setStyleSheet(STYLE)
        self.config = load_config(config_path)
        self.queue = TaskQueue(workspace, self, output_root=output_root)
        self.closing = False
        self.items: dict[str, QTreeWidgetItem] = {}
        self.selected_id = None
        self.figure_stamp = self.log_stamp = self.report_stamp = None
        self.last_log_scan = 0.0
        self.figure_dialog: FigureDialog | None = None

        central = QWidget()
        central.setObjectName("workspace")
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 10, 12, 8)
        layout.setSpacing(10)
        header = QHBoxLayout()
        title = QLabel("CRF Studio")
        title.setObjectName("heading")
        header.addWidget(title)
        subtitle = QLabel("Bitrate & B-frame QP")
        subtitle.setObjectName("muted")
        header.addWidget(subtitle)
        header.addStretch()
        layout.addLayout(header)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(12)
        sidebar = QWidget()
        sidebar.setMinimumWidth(255)
        sidebar.setMaximumWidth(400)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(10)
        settings = QGroupBox("New task settings")
        form = QVBoxLayout(settings)
        form.setSpacing(8)
        config_row = QHBoxLayout()
        self.config_label = QLabel(
            config_path.name if config_path else "Default configuration"
        )
        self.config_label.setToolTip(str(config_path) if config_path else "")
        self.config_label.setObjectName("muted")
        self.config_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        config_row.addWidget(self.config_label, 1)
        config_button = QToolButton()
        config_button.setText("Config")
        config_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        config_menu = QMenu(config_button)
        config_menu.addAction("Load config…", self.load_settings)
        config_menu.addAction("Save config…", self.save_settings)
        config_button.setMenu(config_menu)
        config_row.addWidget(config_button)
        form.addLayout(config_row)
        values = QGridLayout()
        values.setHorizontalSpacing(10)
        values.addWidget(QLabel("Samples"), 0, 0)
        values.addWidget(QLabel("Seconds each"), 0, 1)
        self.sample_count = QSpinBox()
        self.sample_count.setRange(1, 10000)
        self.sample_count.setToolTip(
            "Clips spread across the video; short videos use fewer clips"
        )
        values.addWidget(self.sample_count, 1, 0)
        self.sample_seconds = QDoubleSpinBox()
        self.sample_seconds.setDecimals(3)
        self.sample_seconds.setRange(0.001, 86400)
        values.addWidget(self.sample_seconds, 1, 1)
        values.addWidget(QLabel("Codec"), 2, 0)
        values.addWidget(QLabel("Crop"), 2, 1)
        self.codec = QComboBox()
        self.codec.addItems(["both", "x264", "x265"])
        values.addWidget(self.codec, 3, 0)
        self.crop = QLineEdit()
        self.crop.setToolTip("auto, none, or exact width:height:x:y")
        values.addWidget(self.crop, 3, 1)
        values.setColumnStretch(0, 1)
        values.setColumnStretch(1, 1)
        form.addLayout(values)
        form.addWidget(self.button("Encoder options…", self.edit_encoders))
        self.encoder_label = QLabel()
        self.encoder_label.setObjectName("muted")
        self.encoder_label.setWordWrap(True)
        form.addWidget(self.encoder_label)
        side.addWidget(settings)
        self.fill_settings()

        queue_header = QHBoxLayout()
        queue_header.addWidget(QLabel("Task queue"))
        queue_header.addStretch()
        add = self.button("Add videos…", self.add_videos)
        add.setObjectName("primary")
        queue_header.addWidget(add)
        side.addLayout(queue_header)
        controls = QHBoxLayout()
        self.start_button = self.button("Start queue", self.queue.start)
        self.start_button.setObjectName("primary")
        self.pause_button = self.button("Pause", self.queue.pause)
        self.pause_button.setToolTip("Finish the current video, then pause the queue")
        controls.addWidget(self.start_button, 1)
        controls.addWidget(self.pause_button, 1)
        side.addLayout(controls)
        self.overview = QLabel()
        self.overview.setObjectName("muted")
        side.addWidget(self.overview)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Video / codec", "Status"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.tree.setMinimumHeight(95)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.tree.itemSelectionChanged.connect(self.refresh_details)
        side.addWidget(self.tree, 1)
        actions = QHBoxLayout()
        self.cancel_button = self.button(
            "Cancel", lambda: self.task_action(self.queue.cancel)
        )
        self.retry_button = self.button(
            "Retry", lambda: self.task_action(self.queue.retry)
        )
        self.remove_button = self.button(
            "Remove", lambda: self.task_action(self.queue.remove)
        )
        self.remove_button.setToolTip(
            "Remove from the queue; keep saved figures and logs"
        )
        for button in (self.cancel_button, self.retry_button, self.remove_button):
            actions.addWidget(button, 1)
        side.addLayout(actions)
        self.splitter.addWidget(sidebar)

        details = QWidget()
        details.setMinimumWidth(540)
        detail_layout = QVBoxLayout(details)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(8)
        self.detail_title = QLabel("Select a video to view its results")
        self.detail_title.setObjectName("detailTitle")
        self.detail_title.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self.detail_title.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        detail_layout.addWidget(self.detail_title)
        self.progress_label = QLabel()
        self.progress_label.setObjectName("muted")
        self.progress_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        detail_layout.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        detail_layout.addWidget(self.progress_bar)
        self.tabs = QTabWidget()
        self.figure = FigurePreview()
        self.tabs.addTab(self.figure, "Bitrate / QP")
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
        self.folder_button = self.button(
            "Open results folder", lambda: self.open_result()
        )
        self.figure_button = self.button("Larger preview…", self.preview_figure)
        self.csv_button = self.button(
            "Open CSV table", lambda: self.open_result("summary.csv")
        )
        for button in (self.folder_button, self.figure_button, self.csv_button):
            links.addWidget(button)
        links.addStretch()
        detail_layout.addLayout(links)
        self.splitter.addWidget(details)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([280, 764])
        layout.addWidget(self.splitter, 1)
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
        self.sample_count.setMaximum(max(10000, self.config["sampling"]["count"]))
        self.sample_count.setValue(self.config["sampling"]["count"])
        self.sample_seconds.setMaximum(max(86400, self.config["sampling"]["seconds"]))
        self.sample_seconds.setValue(self.config["sampling"]["seconds"])
        self.update_encoder_summary()

    def update_encoder_summary(self) -> None:
        self.encoder_label.setText(
            "\n".join(
                f"{name}: {settings['profile']} @ {settings['level'] or 'auto'}, {settings['preset']}"
                for name, settings in self.config["codecs"].items()
            )
        )

    def current_config(self) -> dict:
        config = validate_config(self.config)
        config["sampling"].update(
            count=self.sample_count.value(), seconds=self.sample_seconds.value()
        )
        config["video"]["crop"] = (
            None
            if self.crop.text().strip().lower() == "none"
            else self.crop.text().strip()
        )
        return validate_config(config)

    def edit_encoders(self) -> None:
        dialog = EncoderSettingsDialog(self.config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.config["codecs"] = dialog.config["codecs"]
            self.config_label.setText("Custom encoder settings")
            self.config_label.setToolTip("")
            self.update_encoder_summary()
        dialog.deleteLater()

    def save_settings(self) -> None:
        try:
            config = self.current_config()
            path, _ = QFileDialog.getSaveFileName(
                self,
                "Save video and encoder configuration",
                "crf_search.json",
                "JSON files (*.json)",
            )
            if path:
                write_json(Path(path), config)
                self.config = config
                self.config_label.setText(Path(path).name)
                self.config_label.setToolTip(path)
        except (ValueError, OSError) as exc:
            self.show_error(str(exc))

    def load_settings(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load encoder configuration", "", "JSON files (*.json)"
        )
        if path:
            try:
                self.config = load_config(Path(path))
                self.config_label.setText(Path(path).name)
                self.config_label.setToolTip(path)
                self.fill_settings()
            except ValueError as exc:
                self.show_error(str(exc))

    def add_videos(self) -> None:
        try:
            config = self.current_config()
            paths, _ = QFileDialog.getOpenFileNames(
                self,
                "Add videos to the queue",
                "",
                "Videos (*.mkv *.mp4 *.m2ts *.mts *.mov *.avi *.webm *.ts *.m4v);;All files (*)",
            )
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
                self.tree.takeTopLevelItem(
                    self.tree.indexOfTopLevelItem(self.items.pop(task_id))
                )
        for index, task in enumerate(self.queue.tasks):
            if task.id not in self.items:
                item = QTreeWidgetItem(self.tree)
                item.setData(0, Qt.ItemDataRole.UserRole, task.id)
                item.setToolTip(0, task.video)
                self.items[task.id] = item
            item = self.items[task.id]
            for column, value in enumerate(
                [
                    f"{Path(task.video).name}\n{task.codec.upper()} · {elapsed(task)}",
                    f"{task.state.title()}\n{task.percent}%",
                ]
            ):
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
        activity = (
            f"Running: {Path(active.video).name} · {elapsed(active)}"
            if active
            else "Queue paused"
            if queued
            else "Queue idle"
        )
        self.overview.setText(f"{queued} queued · {completed} completed")
        self.overview.setToolTip(activity)
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
            self.saved_settings.setPlainText(
                json.dumps(task.config, indent=2) if task else ""
            )
            self.log_selector.blockSignals(True)
            self.log_selector.clear()
            self.log_selector.addItem("Task log", "task.log")
            self.log_selector.blockSignals(False)
        self.cancel_button.setEnabled(
            bool(task and task.state in {"queued", "running"})
        )
        self.retry_button.setEnabled(
            bool(task and task.state in {"failed", "cancelled", "interrupted"})
        )
        self.remove_button.setEnabled(bool(task and task is not self.queue.active))
        self.folder_button.setEnabled(bool(task))
        self.folder_button.setToolTip(str(self.queue.output(task)) if task else "")
        self.figure_button.setEnabled(
            bool(
                task
                and any(
                    (self.queue.output(task) / name).exists()
                    for name in ("results.json", "qp-bitrate.png")
                )
            )
        )
        self.csv_button.setEnabled(
            bool(task and (self.queue.output(task) / "summary.csv").exists())
        )
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(task.percent if task else 0)
        if not task:
            self.detail_title.setText("Select a video to view its results")
            self.detail_title.setToolTip("")
            self.progress_label.clear()
            self.progress_label.setToolTip("")
            self.log.clear()
            return
        self.detail_title.setText(Path(task.video).name)
        self.detail_title.setToolTip(task.video)
        progress = task.progress
        message = task.error or progress.get("message", task.state.title())
        if task.state == "cancelling":
            message = "Cancelling the active sample and saving completed results…"
        if task.state == "running" and not progress.get("total"):
            self.progress_bar.setRange(0, 0)
        counts = (
            f" · {progress.get('completed', 0)}/{progress['total']} encodes complete"
            if progress.get("total")
            else ""
        )
        frames = (
            f" · {progress['frames']} frames submitted"
            if progress.get("frames") and progress.get("stage") == "encoding"
            else ""
        )
        flushing = " · flushing encoder" if frames and progress.get("flushing") else ""
        self.progress_label.setText(
            f"{message}{counts}{frames}{flushing} · {elapsed(task)}"
        )
        self.progress_label.setToolTip(self.progress_label.text())
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
            existing = {
                self.log_selector.itemData(i) for i in range(self.log_selector.count())
            }
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
            self.figure_dialog.set_file(
                self.queue.output(task) / "qp-bitrate.png", Path(task.video).name
            )
            self.figure_dialog.show()
            self.figure_dialog.raise_()

    def show_error(self, message: str) -> None:
        QMessageBox.warning(self, "CRF Studio", message)

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
                self.statusBar().showMessage(
                    "Stopping the active task before closing; queued tasks and results will be kept."
                )
            return
        if self.figure_dialog:
            self.figure_dialog.close()
        self.queue.close()
        event.accept()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd() / "crf-tasks",
        help="Folder for saved queue state (task results default to beside each video)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Parent folder for new task results (default: crf-tasks beside each video)",
    )
    parser.add_argument(
        "--config", type=Path, help="Initial video and encoder configuration"
    )
    args = parser.parse_args(argv)
    app = QApplication([sys.argv[0]])
    app.setApplicationName("CRF Studio")
    try:
        window = Window(args.workspace, args.config, output_root=args.output_dir)
    except (OSError, ValueError) as exc:
        QMessageBox.critical(None, "Cannot open queue", str(exc))
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
