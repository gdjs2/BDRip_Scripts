"""Editable encoder options using the same validation as the CRF CLI."""

from __future__ import annotations

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from bdrip.crf.config import load_config, validate_config

PRESETS = (
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
    "placebo",
)


class EncoderOptions(QWidget):
    def __init__(self, codec: str, settings: dict):
        super().__init__()
        self.codec = codec
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.profile = QLabel()
        form.addRow("Profile / pixel format", self.profile)
        self.preset = QComboBox()
        self.preset.addItems(PRESETS)
        form.addRow("Preset", self.preset)
        self.level = QLineEdit()
        self.level.setPlaceholderText("auto (or a level such as 4.1)")
        form.addRow("Level", self.level)
        self.tune = QLineEdit()
        self.tune.setPlaceholderText("Blank for none; for example grain")
        form.addRow("Tune", self.tune)
        layout.addLayout(form)
        layout.addWidget(
            QLabel(
                f"{codec} parameters — key=value entries separated by colons or newlines"
            )
        )
        self.params = QPlainTextEdit()
        self.params.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        self.params.setMinimumHeight(130)
        layout.addWidget(self.params, 1)
        layout.addWidget(QLabel("Additional FFmpeg encoder options"))
        self.options = QTableWidget(0, 2)
        self.options.setHorizontalHeaderLabels(["Option", "Value"])
        self.options.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.options.setMaximumHeight(140)
        layout.addWidget(self.options)
        buttons = QHBoxLayout()
        add = QPushButton("Add option")
        add.clicked.connect(self.add_option)
        remove = QPushButton("Remove selected option")
        remove.clicked.connect(self.remove_option)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.load(settings)

    def load(self, settings: dict) -> None:
        self.profile.setText(f"{settings['profile']} / {settings['pixel_format']}")
        self.preset.setCurrentText(settings["preset"])
        self.level.setText(settings["level"] or "auto")
        self.tune.setText(settings["tune"] or "")
        self.params.setPlainText(
            ":".join(f"{key}={value}" for key, value in settings["params"].items())
        )
        self.options.setRowCount(0)
        for key, value in settings["options"].items():
            self.add_option(key, value)

    def add_option(self, key: str = "", value: str = "") -> None:
        # clicked(bool) should create an empty row, not an option named False.
        key = key if isinstance(key, str) else ""
        row = self.options.rowCount()
        self.options.insertRow(row)
        self.options.setItem(row, 0, QTableWidgetItem(key))
        self.options.setItem(row, 1, QTableWidgetItem(str(value)))
        self.options.setCurrentCell(row, 0)

    def remove_option(self) -> None:
        rows = {item.row() for item in self.options.selectedIndexes()}
        for row in sorted(rows, reverse=True):
            self.options.removeRow(row)

    def values(self) -> dict:
        options = {}
        for row in range(self.options.rowCount()):
            key, value = [
                self.options.item(row, col).text().strip()
                if self.options.item(row, col)
                else ""
                for col in (0, 1)
            ]
            if not key and not value:
                continue
            if not key or not value:
                raise ValueError(
                    f"{self.codec}: each additional option needs a name and a value"
                )
            if key in options:
                raise ValueError(f"{self.codec}: duplicate additional option {key!r}")
            options[key] = value
        level = self.level.text().strip()
        return {
            "preset": self.preset.currentText(),
            "level": None if not level or level.lower() == "auto" else level,
            "tune": self.tune.text().strip() or None,
            "params": ":".join(
                line.strip().strip(":")
                for line in self.params.toPlainText().splitlines()
                if line.strip()
            ),
            "options": options,
        }


class EncoderSettingsDialog(QDialog):
    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Encoder options for new tasks")
        self.resize(820, 660)
        self.config = validate_config(config)
        layout = QVBoxLayout(self)
        label = QLabel(
            "Changes apply to videos added after you save. Each queued task keeps its captured settings."
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        self.tabs = QTabWidget()
        self.editors = {}
        for codec, settings in self.config["codecs"].items():
            editor = EncoderOptions(codec, settings)
            self.editors[codec] = editor
            self.tabs.addTab(editor, codec)
        layout.addWidget(self.tabs, 1)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #c0392b;")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.RestoreDefaults
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(
            self.restore_defaults
        )
        layout.addWidget(buttons)

    def restore_defaults(self) -> None:
        for codec, settings in load_config()["codecs"].items():
            self.editors[codec].load(settings)
        self.error.clear()

    def accept(self) -> None:
        config = validate_config(self.config)
        try:
            for codec, editor in self.editors.items():
                config["codecs"][codec].update(editor.values())
            config = validate_config(config)
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        self.config = config
        super().accept()
