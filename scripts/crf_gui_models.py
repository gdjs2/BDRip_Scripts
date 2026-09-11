"""Inspect predictions from saved two-point models without running more encodes."""

from PySide6.QtWidgets import (
    QAbstractItemView, QDoubleSpinBox, QHBoxLayout, QHeaderView, QLabel,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

if __package__:
    from .crf_model import METHOD, predict
else:
    from crf_model import METHOD, predict


class ModelEstimates(QWidget):
    def __init__(self):
        super().__init__()
        self.report = {}
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Estimate at CRF"))
        self.crf = QDoubleSpinBox()
        self.crf.setRange(0, 51)
        self.crf.setDecimals(1)
        self.crf.setSingleStep(0.1)
        self.crf.setValue(16.5)
        controls.addWidget(self.crf)
        controls.addStretch()
        layout.addLayout(controls)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Encoder", "Estimated B-frame QP", "Estimated video Mbps"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMaximumHeight(130)
        layout.addWidget(self.table)
        self.equations = QLabel()
        self.equations.setWordWrap(True)
        layout.addWidget(self.equations)
        layout.addStretch()
        self.crf.valueChanged.connect(self.refresh)
        self.refresh()

    def set_report(self, report: dict) -> None:
        self.report = report
        self.refresh()

    def refresh(self) -> None:
        self.table.setRowCount(0)
        self.equations.clear()
        if self.report and self.report.get("method") != METHOD:
            self.message.setText("Previous sweep result. Its original figure and measurements are preserved.")
            return
        crf = self.crf.value()
        self.message.setText("Estimates from CRF 13 and 20; no additional encoding is performed."
                             if 13 <= crf <= 20 else "Extrapolation beyond the measured CRF 13–20 interval.")
        equations = []
        for codec, analysis in self.report.get("codecs", {}).items():
            models = analysis.get("models", {})
            estimate = predict(models, crf)
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [codec, *(f"{estimate[field]:.3f}" if estimate[field] is not None else "N/A"
                              for field in ("average_qp", "average_bitrate_mbps"))]
            for column, text in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(text))
            qp, bitrate = models.get("qp"), models.get("log_bitrate")
            equations.append(f"{codec}: QP(c) = {qp['a']:.6g} {qp['b']:+.6g}c" if qp else
                             f"{codec}: {models.get('qp_status', 'Waiting for both measurements')}")
            if bitrate:
                equations.append(f"{codec}: ln R(c) = {bitrate['d']:.6g} {bitrate['e']:+.6g}c (R in Mbps)")
        self.equations.setText("\n".join(equations))
