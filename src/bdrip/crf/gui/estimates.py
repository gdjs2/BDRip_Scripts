"""Inspect predictions from saved two-point models without running more encodes."""

from PySide6.QtWidgets import (
    QAbstractItemView,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from bdrip.crf.model import METHOD, fit_models, predict_bitrate


class ModelEstimates(QWidget):
    def __init__(self):
        super().__init__()
        self.report = {}
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Video bitrate (Mbps)"))
        self.bitrate = QDoubleSpinBox()
        self.bitrate.setDecimals(6)
        self.bitrate.setRange(0.000001, 100000)
        self.bitrate.setSingleStep(0.1)
        self.bitrate.setValue(8)
        controls.addWidget(self.bitrate)
        controls.addStretch()
        layout.addLayout(controls)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(
            ["Encoder", "Estimated B-frame QP", "Approximate CRF"]
        )
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMaximumHeight(130)
        layout.addWidget(self.table)
        self.equations = QLabel()
        self.equations.setWordWrap(True)
        layout.addWidget(self.equations)
        layout.addStretch()
        self.bitrate.valueChanged.connect(self.refresh)
        self.refresh()

    def set_report(self, report: dict) -> None:
        self.report = report
        self.refresh()

    def refresh(self) -> None:
        self.table.setRowCount(0)
        self.equations.clear()
        if self.report and self.report.get("method") != METHOD:
            self.message.setText(
                "Previous sweep result. Its original figure and measurements are preserved."
            )
            return
        bitrate = self.bitrate.value()
        equations = []
        extrapolated = []
        for codec, analysis in self.report.get("codecs", {}).items():
            models = analysis.get("models") or fit_models(analysis["rows"])
            estimate = predict_bitrate(models, bitrate)
            if estimate["extrapolated"]:
                extrapolated.append(codec)
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                codec,
                *(
                    f"{estimate[field]:.3f}" if estimate[field] is not None else "N/A"
                    for field in ("average_qp", "crf")
                ),
            ]
            for column, text in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(text))
            qp, rate = models.get("qp"), models.get("log_bitrate")
            if qp and rate and estimate["average_qp"] is not None:
                beta = qp["b"] / rate["e"]
                alpha = qp["a"] - beta * rate["d"]
                equations.append(
                    f"{codec}: QP(R) = {alpha:.6g} {beta:+.6g} ln R (R in Mbps)"
                )
            else:
                equations.append(f"{codec}: {estimate['status']}")
        self.message.setText(
            "Estimates from the measured endpoints. CRF is approximate; no additional encoding is performed."
            + (
                f"\nExtrapolation outside the measured bitrate range: {', '.join(extrapolated)}."
                if extrapolated
                else ""
            )
        )
        self.equations.setText("\n".join(equations))
